"""Client memory (FR-19): what the client told Smith, kept as Markdown files and retrieved for every advice call.

The client mentions facts the ledger cannot hold (which home a nickname means, how long they lived where,
a subscription they applied for, a spouse's consent, how they like advice). Smith keeps them in
`data/memory/<topic>.md`, next to the ledger and ignored by Git, as plain bullets the client can read,
correct or delete in any editor:

    # 부동산·주거

    - 2026-10-05 · 메일 · 솔방울은 푸른마을아파트를 말한다.

Facts come only from the client: an answered e-mail question (the answer names what it will remember)
or `smith memory add`. Web research never writes memory and the research model never reads it. Once a month the
client sees what Smith remembers and replies with corrections (FR-52, `memory_review`); `revise` then retires or
rewrites the line and keeps the old one in ARCHIVE.

Retrieval is a small RAG step without embeddings, so nothing leaves the machine to index it: the profile
and preference topics always go with a call; the other facts are ranked by shared character bigrams
with the call's text (Korean particles still match: "푸른마을을" shares "푸른마을") and newest first, within a
character budget. When everything fits, everything is sent.

Facts are the client's statements, not ledger facts: prompts say so, and a figure taken from one is
flagged like a figure from the client's question. Identifiers (phone, e-mail, account numbers) and
building or unit numbers are removed when a fact is written and again when it is read.
"""
import math
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from smith.payload import mask_identifiers

TOPICS = {"profile": "가구·인적 사항", "property": "부동산·주거", "plan": "계획·일정", "preference": "선호·원칙",
          "family": "가족·지인", "other": "기타", "counsel": "상담에서 들은 것"}   # family: tastes and facts behind gift ideas (FR-31 ②).
PINNED = ("profile", "preference")   # Always sent: they shape every answer.
# Read only by the thread that wrote them (FR-49, review 2026-11-01): what the client says in a counsel may be about
# anything, the body included, so it never reaches the finance, daily or health calls through the shared memory.
PRIVATE = ("counsel",)
MAX_FACT_CHARS = 300
BUDGET_CHARS = 4000                  # About 25 facts; beyond this only the most relevant are sent.
_ENTRY = re.compile(r"^- (?:(\d{4}-\d{2}-\d{2}) · ([^·]{1,20}) · )?(.+)$")
# A building or unit number ("303동", "1203호") narrows a complex to one home: never kept.
_UNIT = re.compile(r"(?<!\d)\d{1,4}\s*(?:동|호)(?!\s*(?:이상|이하|이내))")
# Lines the client retired or corrected (FR-52) are kept here, below the folder: `load` reads only the top level, so a
# retired line never reaches a call again, and nothing the client once said is lost.
ARCHIVE = Path("archive") / "retired.md"
LOCK_WAIT_SECONDS = 2.0   # A write that meets a live lock waits this long before giving up: appends take milliseconds.


class MemoryBusy(OSError):
    """Another write holds the memory folder's lock (an OSError, so callers that keep going on a failed write still do)."""


@dataclass(frozen=True)
class Fact:
    topic: str
    text: str
    recorded_on: date | None   # None for a line the client wrote by hand without a date.
    source: str
    file: str = ""             # The file's stem ("other" facts may come from a file that is not a topic).


def directory(db: Path) -> Path:
    """Memory lives next to the ledger (data/memory for data/smith.db), so it is private by the same rule."""
    return Path(db).parent / "memory"


def clean(text: str) -> str:
    """One line, identifiers and building or unit numbers removed, at most MAX_FACT_CHARS."""
    text = _UNIT.sub("(동·호 생략)", mask_identifiers(" ".join(str(text).split())))
    return text.replace("((동·호 생략))", "(동·호 생략)")[:MAX_FACT_CHARS].strip()


def load(folder: Path, *, private: bool = False) -> list[Fact]:
    """Every bullet of every Markdown file in `folder`; a file whose name is not a topic counts as "other". The
    PRIVATE topics are left out unless `private` is set (the counsel thread and the client's own listing)."""
    facts = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        if path.stem in PRIVATE and not private:
            continue
        topic = path.stem if path.stem in TOPICS else "other"
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            match = _ENTRY.match(line.strip()) if not line.startswith((" ", "\t")) else None
            if not match or not clean(match.group(3)):
                continue
            try:
                recorded = date.fromisoformat(match.group(1)) if match.group(1) else None
            except ValueError:
                recorded = None
            facts.append(Fact(topic, clean(match.group(3)), recorded, (match.group(2) or "직접 작성").strip(), path.stem))
    return facts


def text_key(text: str) -> str:
    """What `add` and `revise` compare: the cleaned text without spacing or punctuation (a decimal point kept)."""
    return _key(clean(text))


def find(folder: Path, text: str, *, file: str | None = None) -> Fact | None:
    """The remembered fact that says `text` (spacing and punctuation aside), in `file` when given; counsel included."""
    wanted = text_key(text)
    return next((f for f in load(folder, private=True) if _key(f.text) == wanted and (file is None or f.file == file)), None)


def fact_key(fact: Fact) -> str:
    """A fact's identity across months: its file and its text without spacing or punctuation."""
    return f"{fact.file or fact.topic}:{_key(fact.text)}"


def new_facts(existing: list[Fact], candidates: list[dict[str, str]]) -> list[tuple[str, str]]:
    """(topic, text) of the candidates not already remembered, cleaned; unknown topics become "other"."""
    known = {_key(f.text) for f in existing}
    found: dict[str, tuple[str, str]] = {}
    for item in candidates:
        text = clean(item.get("fact", ""))
        if text and _key(text) not in known:
            found.setdefault(_key(text), (item.get("topic") if item.get("topic") in TOPICS else "other", text))
    return list(found.values())


def add(folder: Path, topic: str, text: str, *, on: date, source: str) -> Fact | None:
    """Append one fact to `<topic>.md`; None when it is empty or already remembered."""
    topic = topic if topic in TOPICS else "other"
    with _locked(folder):
        fresh = new_facts(load(folder, private=True), [{"topic": topic, "fact": text}])
        if not fresh:
            return None
        _, text = fresh[0]
        source = _source(source)
        path = folder / f"{topic}.md"
        header = "" if path.exists() else f"# {TOPICS[topic]}\n\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{header}- {on.isoformat()} · {source} · {text}\n")
    return Fact(topic, text, on, source, topic)


def revise(folder: Path, fact: Fact, *, replacement: str | None, on: date, source: str) -> str:
    """Retire one remembered line the client said is wrong or no longer true, writing `replacement` in its place when
    given. The old line moves to ARCHIVE with the day and what happened, so it never reaches a call again but is not lost.

    Returns "retired", "replaced", "unchanged" (the replacement says the same) or "missing": the line is no longer in its
    file (edited by hand since it was shown), and nothing was written.

    Raises:
        MemoryBusy: another write holds the lock. OSError: the folder could not be written.
    """
    new_text = clean(replacement) if replacement is not None else None
    if replacement is not None and not new_text:
        raise ValueError("empty replacement")
    if new_text is not None and _key(new_text) == _key(fact.text):
        return "unchanged"
    path = folder / f"{fact.file or fact.topic}.md"
    with _locked(folder):
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
        index = next((i for i, line in enumerate(lines) if _line_key(line) == _key(fact.text)), None)
        if index is None:
            return "missing"
        old = lines[index]
        known = {_key(f.text) for f in load(folder, private=True)}
        if new_text is None or _key(new_text) in known:
            del lines[index]   # Retired, or the replacement is already remembered in another line.
        else:
            lines[index] = f"- {on.isoformat()} · {_source(source)} · {new_text}"
        _archive(folder, old, on, "지움" if new_text is None else f"고침 → {new_text}")
        temp = path.with_name(path.name + ".tmp")
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        os.replace(temp, path)
    return "retired" if new_text is None else "replaced"


def _line_key(line: str) -> str | None:
    match = _ENTRY.match(line.strip()) if not line.startswith((" ", "\t")) else None
    return _key(clean(match.group(3))) if match else None


def _archive(folder: Path, line: str, on: date, what: str) -> None:
    """Append the retired line, as it stood in its file, to ARCHIVE (written before the file is replaced)."""
    target = folder / ARCHIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    header = "" if target.exists() else "# 고치거나 지운 기억 (FR-52)\n\n"
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{header}- {on.isoformat()} · {what[:MAX_FACT_CHARS + 10]} · 원래 줄: {line.strip()[2:]}\n")


def _source(source: str) -> str:
    return clean(source).replace("·", " ")[:20] or "직접 작성"


@contextmanager
def _locked(folder: Path) -> Iterator[None]:
    """A process lock for the memory folder: a reply that corrects a line and one that adds a fact would otherwise lose
    each other's write. The operating system holds the lock on an open handle and releases it when the process ends,
    so a crashed run never leaves a lock behind and no age rule can let two writers in at once (Codex review
    2026-11-15). A held lock is waited for up to LOCK_WAIT_SECONDS, so a fact a concurrent answer keeps is not dropped.

    Raises:
        MemoryBusy: the lock stayed held for LOCK_WAIT_SECONDS.
    """
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / ".write.lock", "a+b")
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    try:
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise MemoryBusy("another memory write is in progress")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


def _try_lock(handle: Any) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def retrieve(facts: list[Fact], query: str, *, budget: int = BUDGET_CHARS) -> list[Fact]:
    """The facts to send with one call: pinned topics first, then the most relevant, within `budget`."""
    if sum(len(f.text) for f in facts) <= budget:
        return sorted(facts, key=_recency, reverse=True)
    pinned = sorted((f for f in facts if f.topic in PINNED), key=_recency, reverse=True)
    others = [f for f in facts if f.topic not in PINNED]
    weights = _idf(others)
    wanted = _bigrams(query)
    others.sort(key=lambda f: (sum(weights[g] for g in _bigrams(f.text) & wanted), _recency(f)), reverse=True)
    chosen, used = [], 0
    for fact in pinned + others:
        if used + len(fact.text) <= budget:
            chosen.append(fact)
            used += len(fact.text)
    return chosen


def for_model(facts: list[Fact]) -> list[dict[str, Any]]:
    """The payload form: what the client said, under which topic, when and through which channel."""
    return [{"topic": f.topic, "fact": f.text, "recorded_on": None if f.recorded_on is None else f.recorded_on.isoformat(),
             "source": f.source} for f in facts]


def _key(text: str) -> str:
    """Text without spacing or punctuation, except a decimal point between digits: "1.5억" is not "15억" (Codex review
    2026-11-15)."""
    return re.sub(r"[\s.,·!?~]", "", re.sub(r"(?<=\d)\.(?=\d)", "_", text))


def _recency(fact: Fact) -> date:
    return fact.recorded_on or date.min


def _bigrams(text: str) -> set[str]:
    """Character bigrams of each word: Korean has no spaces between a noun and its particle."""
    grams = set()
    for word in re.findall(r"[0-9A-Za-z가-힣]+", text.lower()):
        grams |= {word[i:i + 2] for i in range(len(word) - 1)} or {word}
    return grams


def _idf(facts: list[Fact]) -> dict[str, float]:
    """Rare bigrams weigh more than common ones ("아파트" appears everywhere, "푸른마을" in one fact)."""
    counts: dict[str, int] = {}
    for fact in facts:
        for gram in _bigrams(fact.text):
            counts[gram] = counts.get(gram, 0) + 1
    total = len(facts) or 1
    return {gram: math.log(1 + total / count) for gram, count in counts.items()}
