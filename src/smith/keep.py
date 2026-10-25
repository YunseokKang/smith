"""Google Keep notes as the client's own thoughts (FR-35, user decision 2026-10-25): the household's knowledge store,
synced from the scheduled Takeout archives in Drive, kept next to the ledger, and handed to a model only in small,
task-sized, identifier-masked selections.

Google's Keep API serves Workspace accounts only, so a personal account exports through Takeout: a zip with
`Takeout/Keep/<note>.json` files (title, textContent or listContent, labels, timestamps, isTrashed/isArchived/isPinned).
`import_takeout` reads a zip (or the parts of one export, or an extracted folder), drops trashed and empty notes and
writes `data/keep/notes.json`; `sync` pulls the newest export from Drive once a day (daily design §13).

The client keeps monthly journal notes (title "2026-04") whose dated bullets ("0406 ㆍ...") are the real unit of
thought, so notes are split into `Entry` objects: selection (`thoughts`) and search (`search`) work on entries, an
entry used for a sentence is not offered again for REUSE_DAYS, and only the chosen entries reach a model.
"""
import hashlib
import json
import math
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smith.payload import mask_identifiers

NOTES_FILE = "notes.json"
USED_FILE = "used.json"
SYNC_FILE = "sync.json"
REUSE_DAYS = 60
SYNC_EVERY_HOURS = 24
MIN_CHARS, MAX_CHARS = 20, 1200
MODEL_CHARS = 300
MAX_ZIP_ENTRIES, MAX_ZIP_BYTES, MAX_ENTRY_BYTES = 20_000, 200 * 1024 * 1024, 20 * 1024 * 1024
PICK = 6
THOUGHT_LABELS = ("생각", "thought", "thoughts", "일기", "메모", "idea", "아이디어", "다짐", "원칙")
STUDY_LABELS = ("학습", "근골격", "스트레칭", "운동", "영어", "일본어", "study")
# The sentence wants the client's own reflections, not their lecture notes: first-person and feeling words count for an
# entry, markdown headings, quotes, year-dense history and mostly-English text count against it (tuned on the first
# import, 2026-10-25: journal reflections ranked beside Power English and Edo-period study notes).
_REFLECTIVE = re.compile(r"나는|내가|나의|내 |우리|생각|느낌|마음|삶|인생|하루|오늘|싶다|해야겠|다짐|배웠|깨달|행복|감사|좋았|힘들")
_STUDY = re.compile(r"^#|##|^>|(?:(?:1[0-9]|20)\d{2}년.*){2,}|정의\s*:|요약\s*:|제\s?\d+\s?장")
_JAPANESE_CHINESE = re.compile(r"[぀-ヿ一-鿿]")
_MARK = re.compile(r"^\s*(?:[ㆍ•·▪■□●○✓✔\-\*]\s*|\d{1,2}/\d{1,2}\b|\d{4}\b(?!년))")
_MMDD = re.compile(r"^(\d{2})(\d{2})\b\s*")
_MD = re.compile(r"^(\d{1,2})/(\d{1,2})\b\s*")
_BULLET = re.compile(r"^\s*[ㆍ•·▪■□●○✓✔\-\*]\s*")
_TITLE_PERIOD = re.compile(r"^(\d{4})(?:[-./](\d{1,2}))?")


@dataclass(frozen=True)
class Note:
    id: str
    title: str
    text: str
    labels: tuple[str, ...]
    edited: datetime | None
    archived: bool
    pinned: bool
    kind: str   # "note" (prose) | "list" (checklist)


@dataclass(frozen=True)
class Entry:
    """One thought: a dated bullet of a journal note, or a whole short note."""
    id: str            # "<note id>#<index>"
    note_id: str
    text: str
    on: date | None    # The entry's own date when it carries one, else the running date of the note.
    labels: tuple[str, ...]
    kind: str


@dataclass(frozen=True)
class ImportResult:
    imported: int
    trashed: int
    empty: int
    source: str


def directory(db: Path) -> Path:
    return Path(db).parent / "keep"


# --- import and storage ----------------------------------------------------------------------------------------------

def import_takeout(source: Path | list[Path], folder: Path) -> ImportResult:
    """Read a Takeout zip (or the parts of one export, or an extracted folder) and (re)write `notes.json`.
    Raises OSError/ValueError on a bad source."""
    sources = source if isinstance(source, list) else [source]
    documents = [doc for one in sources for doc in _documents(one)]
    if not documents:
        raise ValueError("no Keep notes found (expected Takeout/Keep/*.json)")
    notes, trashed, empty = [], 0, 0
    for name, raw in documents:
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("isTrashed"):
            trashed += 1
            continue
        note = _note(name, item)
        if note is None:
            empty += 1
            continue
        notes.append(note)
    folder.mkdir(parents=True, exist_ok=True)
    label = str(sources[0] if len(sources) == 1 else sources[0].parent)
    payload = {"imported_at": datetime.now(timezone.utc).isoformat(), "source": label, "notes": [_raw(n) for n in notes]}
    temp = folder / (NOTES_FILE + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, folder / NOTES_FILE)
    return ImportResult(len(notes), trashed, empty, label)


def load(folder: Path) -> list[Note]:
    path = folder / NOTES_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    notes = []
    for item in data.get("notes", []) if isinstance(data, dict) else []:
        try:
            notes.append(Note(str(item["id"]), str(item.get("title", "")), str(item["text"]), tuple(item.get("labels", [])),
                              datetime.fromisoformat(item["edited"]) if item.get("edited") else None,
                              bool(item.get("archived")), bool(item.get("pinned")), str(item.get("kind", "note"))))
        except (KeyError, TypeError, ValueError):
            continue
    return notes


def imported_at(folder: Path) -> datetime | None:
    path = folder / NOTES_FILE
    try:
        return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8-sig"))["imported_at"]) if path.exists() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def recent_count(notes: list[Note], *, days: int = 365) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return sum(1 for n in notes if n.edited is not None and n.edited >= cutoff)


# --- entries ---------------------------------------------------------------------------------------------------------

def entries(notes: list[Note]) -> list[Entry]:
    """Every note split into entries at bullets and date tokens; a date token sets the date of the entries after it."""
    found: list[Entry] = []
    for note in notes:
        year, _ = _title_period(note.title)
        running = None if note.edited is None else note.edited.date()
        for index, chunk in enumerate(_split(note.text)):
            on, body = _dated(chunk, year)
            running = on or running
            body = " ".join(body.split())
            if len(body) < MIN_CHARS:
                continue
            found.append(Entry(f"{note.id}#{index}", note.id, body[:MAX_CHARS], running, note.labels, note.kind))
    return found


def _split(text: str) -> list[str]:
    segments: list[list[str]] = []
    for line in text.splitlines():
        if _MARK.match(line) or not segments:
            segments.append([line])
        else:
            segments[-1].append(line)
    out: list[str] = []
    for lines in segments:
        segment = "\n".join(lines).strip()
        if len(segment) > MAX_CHARS:
            out += [p.strip() for p in re.split(r"\n\s*\n", segment) if p.strip()]
        elif segment:
            out.append(segment)
    return out


def _title_period(title: str) -> tuple[int | None, int | None]:
    match = _TITLE_PERIOD.match(title.strip())
    if not match:
        return None, None
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else None
    return (year if 1990 <= year <= 2100 else None), (month if month and 1 <= month <= 12 else None)


def _dated(chunk: str, year: int | None) -> tuple[date | None, str]:
    body = _BULLET.sub("", chunk.strip(), count=1)
    on = None
    for pattern in (_MMDD, _MD):
        match = pattern.match(body)
        if match and year is not None:
            try:
                on = date(year, int(match.group(1)), int(match.group(2)))
                body = body[match.end():]
            except ValueError:
                pass
            break
    return on, _BULLET.sub("", body.strip(), count=1)


# --- selection for the writer and search for the answers --------------------------------------------------------------

def thoughts(notes: list[Note], *, day: date, context: str, used: dict[str, date], limit: int = PICK) -> list[Entry]:
    """The entries offered to the writer today: eligible prose, scored by fit with the day, recency and rotation."""
    wanted = _bigrams(context)
    scored = []
    for entry in entries(notes):
        if not _eligible(entry):
            continue
        last = used.get(entry.id)
        if last is not None and (day - last).days < REUSE_DAYS:
            continue
        overlap = len(_bigrams(entry.text) & wanted)
        age_days = None if entry.on is None else max(0, (day - entry.on).days)
        recency = 0.0 if age_days is None else max(0.0, 1.0 - age_days / 1460)
        tagged = (1.0 if any(label.lower() in THOUGHT_LABELS for label in entry.labels) else 0.0) \
            - (0.7 if any(label.lower() in STUDY_LABELS for label in entry.labels) else 0.0)
        reflective = min(1.5, 0.5 * len(_REFLECTIVE.findall(entry.text)))
        letters = sum(ch.isalpha() for ch in entry.text) or 1
        english = sum(ch.isascii() and ch.isalpha() for ch in entry.text) / letters
        foreign = len(_JAPANESE_CHINESE.findall(entry.text)) / letters
        study = -1.0 if (_STUDY.search(entry.text) or english > 0.3 or foreign > 0.05) else 0.0
        seed = int(hashlib.sha256(f"{entry.id}:{day.isoformat()}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        scored.append((overlap * 1.5 + recency + tagged + reflective + study + seed, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored[:limit]]


def search(notes: list[Note], query: str, *, limit: int = 5) -> list[Entry]:
    """The entries most related to `query` by weighted character bigrams (the client-memory method): a 2-character
    Korean word still matches, newest first among equals. List items are searchable too (a wish list is a taste)."""
    wanted = _bigrams(query)
    found = entries(notes)
    if not wanted or not found:
        return []
    grams = {e.id: _bigrams(e.text) for e in found}
    counts: dict[str, int] = {}
    for g in grams.values():
        for gram in g:
            counts[gram] = counts.get(gram, 0) + 1
    total = len(found)
    scored = []
    for entry in found:
        hit = grams[entry.id] & wanted
        if hit:
            scored.append((sum(math.log(1 + total / counts[g]) for g in hit), entry.on or date.min, entry))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [entry for _, _, entry in scored[:limit]]


def for_model(found: list[Entry]) -> list[dict[str, Any]]:
    """The payload form: the client's words with identifiers masked, cut to MODEL_CHARS; the id for rotation."""
    return [{"id": e.id, "text": mask_identifiers(e.text)[:MODEL_CHARS], "date": None if e.on is None else e.on.isoformat()}
            for e in found]


def used(folder: Path) -> dict[str, date]:
    path = folder / USED_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        return {str(k): date.fromisoformat(str(v)) for k, v in raw.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def mark_used(folder: Path, entry_id: str, day: date) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    current = used(folder)
    current[entry_id] = day
    kept = {k: v.isoformat() for k, v in current.items() if (day - v).days <= REUSE_DAYS * 2}
    temp = folder / (USED_FILE + ".tmp")
    temp.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, folder / USED_FILE)


def _eligible(entry: Entry) -> bool:
    if entry.kind != "note" and not any(label.lower() in THOUGHT_LABELS for label in entry.labels):
        return False
    if not MIN_CHARS <= len(entry.text) <= MAX_CHARS:
        return False
    if len(re.findall(r"https?://", entry.text)) >= 2 or re.search(r"(?i)password|비밀번호|계좌|카드번호", entry.text):
        return False
    return True


def _bigrams(text: str) -> set[str]:
    grams = set()
    for word in re.findall(r"[0-9A-Za-z가-힣]+", text.lower()):
        grams |= {word[i:i + 2] for i in range(len(word) - 1)} or {word}
    return grams


# --- Drive sync (daily design §13) -----------------------------------------------------------------------------------

def sync_state(folder: Path) -> dict[str, Any]:
    path = folder / SYNC_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def sync_due(folder: Path, now: datetime) -> bool:
    checked = sync_state(folder).get("last_checked")
    try:
        return checked is None or datetime.fromisoformat(str(checked)) <= now - timedelta(hours=SYNC_EVERY_HOURS)
    except ValueError:
        return True


def sync(folder: Path, token: str, *, now: datetime, lister: Any = None, downloader: Any = None) -> dict[str, Any]:
    """Import the newest Takeout export in Drive when it is newer than the last one imported.

    Returns {"outcome": "imported" | "unchanged" | "no-keep" | "none" | "error", "stamp", "imported", "error_code"};
    Drive errors are returned, not raised, so a scheduled run just logs them.
    """
    from smith import drive

    state = sync_state(folder)
    try:
        archives = (lister or (lambda t: drive.takeout_archives(t, now=now)))(token)
    except drive.DriveError as error:
        return {"outcome": "error", "stamp": None, "imported": 0, "error_code": error.code}
    parts = drive.newest_export(archives)
    state["last_checked"] = now.isoformat()
    if not parts:
        _write_state(folder, state)
        return {"outcome": "none", "stamp": None, "imported": 0, "error_code": None}
    stamp = parts[0].stamp
    if state.get("last_stamp") == stamp:
        _write_state(folder, state)
        return {"outcome": "unchanged", "stamp": stamp, "imported": 0, "error_code": None}
    incoming = folder / "incoming"
    files: list[Path] = []
    try:
        for part in parts:
            target = incoming / part.name
            (downloader or drive.download)(token, part.id, target)
            files.append(target)
        if not any(True for path in files for _ in _documents(path)):
            # An export of other products (no Keep in it): the notes stand, and this stamp is not tried again.
            state["last_stamp"] = stamp
            _write_state(folder, state)
            return {"outcome": "no-keep", "stamp": stamp, "imported": 0, "error_code": None}
        result = import_takeout(files, folder)
    except (drive.DriveError, OSError, ValueError, zipfile.BadZipFile) as error:
        code = getattr(error, "code", None) or type(error).__name__
        _write_state(folder, state)
        return {"outcome": "error", "stamp": stamp, "imported": 0, "error_code": str(code)[:40]}
    finally:
        for path in files:
            path.unlink(missing_ok=True)
    state.update({"last_stamp": stamp, "last_imported_at": now.isoformat(), "last_count": result.imported})
    _write_state(folder, state)
    return {"outcome": "imported", "stamp": stamp, "imported": result.imported, "error_code": None}


def _write_state(folder: Path, state: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / (SYNC_FILE + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, folder / SYNC_FILE)


# --- Takeout document parsing ----------------------------------------------------------------------------------------

def _documents(source: Path):
    if source.is_dir():
        for path in sorted(source.rglob("*.json")):
            if "Keep" in path.parts or path.parent == source:
                yield path.name, path.read_text(encoding="utf-8-sig")
        return
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        # An archive is trusted only within bounds (review 2026-10-25: a zip bomb must not fill the disk).
        if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES or any(i.file_size > MAX_ENTRY_BYTES for i in infos):
            raise ValueError("archive-too-large")
        for info in sorted(infos, key=lambda i: i.filename):
            name = info.filename
            if name.endswith(".json") and "/Keep/" in name.replace("\\", "/"):
                yield name.rsplit("/", 1)[-1], archive.read(name).decode("utf-8-sig", "replace")


def _note(name: str, item: dict[str, Any]) -> Note | None:
    text = str(item.get("textContent") or "").strip()
    kind = "note"
    if not text and isinstance(item.get("listContent"), list):
        kind = "list"
        text = "\n".join(f"- {str(e.get('text', '')).strip()}" for e in item["listContent"]
                         if isinstance(e, dict) and str(e.get("text", "")).strip())
    title = str(item.get("title") or "").strip()
    if not text and not title:
        return None
    labels = tuple(str(l.get("name", "")).strip() for l in item.get("labels", []) if isinstance(l, dict) and l.get("name"))
    edited = _timestamp(item.get("userEditedTimestampUsec") or item.get("createdTimestampUsec"))
    return Note(re.sub(r"\.json$", "", name), title, text, labels, edited, bool(item.get("isArchived")), bool(item.get("isPinned")), kind)


def _timestamp(raw: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(raw) / 1_000_000, tz=timezone.utc) if raw else None
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _raw(note: Note) -> dict[str, Any]:
    return {"id": note.id, "title": note.title, "text": note.text, "labels": list(note.labels),
            "edited": None if note.edited is None else note.edited.isoformat(), "archived": note.archived,
            "pinned": note.pinned, "kind": note.kind}
