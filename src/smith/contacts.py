"""People the client wants to remember (FR-36, user decision 2026-10-25): Google Contacts, read-only, every contact.

The client forgets details about people, so they write them where they already keep people: Google 주소록. A contact's
birthday, 중요한 날 (events) and 메모 (biography: "아이 둘 민준·서연, 커피 안 마심, 음력 9/4") are what Smith remembers.
There is no label switch (the client chose the whole address book on 2026-10-25); Smith syncs once a day through the
People API (`contacts.readonly`: `people/me/connections` with a `personFields` mask), asking only for names, nicknames,
birthdays, events, biographies, organizations and memberships (for 즐겨찾기 only) — never phone numbers, e-mail or postal
addresses, so those never reach the disk.

The notes then serve three things: birthdays and anniversaries join the household's date reminders (for the people the
`[daily] contact_reminders` policy picks, so a full address book does not flood the mail; a lunar birthday is read from
"음력 M/D" in the note), a person named in today's calendar gets a one-line "이 분은" under the event, and a reply asking
about someone gets that contact's note. Third-party data stays local and reaches a model only for at most
MODEL_PEOPLE people per call. A name counts only as a whole word and a name two contacts share picks nobody, so a
note never rides on a chance match (review 2026-10-25). Smith never writes to the address book.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smith import gmail, people
from smith.payload import mask_identifiers

SCOPE = "https://www.googleapis.com/auth/contacts.readonly"
API = "https://people.googleapis.com/v1"
PERSON_FIELDS = "names,nicknames,birthdays,events,biographies,organizations,memberships"
STARRED = "contactGroups/starred"
REMINDER_POLICIES = ("starred", "noted", "all")
SYNC_EVERY_HOURS = 24
STALE_DAYS = 3            # A copy older than this is reported as stale in the mail's data status.
LOCK_STALE_SECONDS = 900  # A lock older than this is a crashed sync's and may be taken over.
MODEL_PEOPLE = 3          # Contacts one model call may see (FR-36); the rest is rendered by code only.
MAX_NOTE = 600
ABOUT_CHARS = 160
MAX_PAGES = 20   # Pages of 200 = 4,000 people; a longer book is stored as partial, never as complete.
FILE = "contacts.json"
_LUNAR = re.compile(r"음력\s*(\d{1,2})\s*[/월.]\s*(\d{1,2})\s*일?")


class ContactsError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Contact:
    id: str                 # people/c... resource id without the prefix, stable.
    name: str
    nicknames: tuple[str, ...]
    birthday: tuple[int | None, int, int] | None    # (year or None, month, day), solar as Contacts stores it.
    lunar_birthday: tuple[int, int] | None          # From "음력 M/D" in the note.
    events: tuple[tuple[str, int | None, int, int], ...]   # (label, year or None, month, day)
    note: str
    organization: str       # "회사 · 직함" when given.
    starred: bool = False   # In 즐겨찾기.


@dataclass(frozen=True)
class Stored:
    """The local copy and its state: missing, invalid, partial, failed-since and stale are told apart (AGENTS rule 5)."""
    contacts: list[Contact] = field(default_factory=list)
    exists: bool = False
    invalid: bool = False
    synced_at: datetime | None = None
    complete: bool = True
    last_error: str | None = None   # The latest attempt's code when it failed after the last success.


def path(db: Path) -> Path:
    """Next to the ledger (data/contacts.json for data/smith.db), ignored by Git like the ledger."""
    return Path(db).parent / FILE


def has_scope(granted: set[str]) -> bool:
    return SCOPE in granted


def connections_url(page_token: str = "") -> str:
    params = {"personFields": PERSON_FIELDS, "pageSize": "200"}
    if page_token:
        params["pageToken"] = page_token
    return f"{API}/people/me/connections?{urllib.parse.urlencode(params)}"


def fetch(token: str, *, getter: gmail.Getter = gmail.get) -> tuple[list[Contact], bool]:
    """Every connection of the signed-in account with the fields Smith keeps, and whether the list is complete (False
    when a page token is still left after MAX_PAGES). Raises ContactsError."""
    found: list[Contact] = []
    page_token = ""
    for _ in range(MAX_PAGES):
        page = _get(connections_url(page_token), token, getter)
        persons = page.get("connections")
        for person in persons if isinstance(persons, list) else []:
            contact = parse(person) if isinstance(person, dict) else None
            if contact is not None:
                found.append(contact)
        page_token = str(page.get("nextPageToken") or "")
        if not page_token:
            break
    return sorted(found, key=lambda c: c.name), not page_token


def parse(person: dict[str, Any]) -> Contact | None:
    """One People API person as a Contact, or None without a display name. Unknown shapes are skipped, not raised."""
    names = _items(person, "names")
    name = str(names[0].get("displayName", "")).strip() if names else ""
    if not name:
        return None
    note = " ".join(" ".join(str(b.get("value", "")).split()) for b in _items(person, "biographies"))
    note = mask_identifiers(note)[:MAX_NOTE]
    birthday = None
    for item in _items(person, "birthdays"):
        when = _month_day(item.get("date"))
        if when is not None:
            birthday = when
            break
    lunar = None
    match = _LUNAR.search(note)
    if match and 1 <= int(match.group(1)) <= 12 and 1 <= int(match.group(2)) <= 30:
        lunar = (int(match.group(1)), int(match.group(2)))
    events = []
    for item in _items(person, "events"):
        when = _month_day(item.get("date"))
        if when is not None:
            label = str(item.get("formattedType") or item.get("type") or "기념일").strip()
            events.append((label[:30], *when))
    nicknames = tuple(str(n["value"]).strip() for n in _items(person, "nicknames") if str(n.get("value", "")).strip())
    org = ""
    for item in _items(person, "organizations"):
        parts = [str(item.get(k, "")).strip() for k in ("name", "title")]
        if any(parts):
            org = " · ".join(p for p in parts if p)
            break
    starred = any((m.get("contactGroupMembership") or {}).get("contactGroupResourceName") == STARRED
                  for m in _items(person, "memberships"))
    resource = str(person.get("resourceName", "")).replace("people/", "")
    return Contact(resource or name, name, nicknames, birthday, lunar, tuple(events), note, org, starred)


def save(file: Path, found: list[Contact], *, now: datetime, complete: bool = True) -> None:
    """Write atomically (unique temp file, then replace), so a failed or concurrent write never leaves half a file."""
    rows = [{"id": c.id, "name": c.name, "nicknames": list(c.nicknames), "birthday": c.birthday,
             "lunar_birthday": c.lunar_birthday, "events": [list(e) for e in c.events], "note": c.note,
             "organization": c.organization, "starred": c.starred} for c in found]
    _write(file, {"synced_at": now.isoformat(), "checked_at": now.isoformat(), "complete": complete, "count": len(found),
                  "last_error": None, "last_error_at": None, "contacts": rows})


def read(file: Path) -> Stored:
    """The local copy with its state. A file that is not the expected shape is `invalid` (an empty book, reported),
    never an exception: the address book is one optional source of the morning mail."""
    if not file.exists():
        return Stored()
    try:
        data = json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return Stored(exists=True, invalid=True)
    items = data.get("contacts", []) if isinstance(data, dict) else None
    if not isinstance(items, list):
        return Stored(exists=True, invalid=True)
    found = [c for c in (_contact(item) for item in items) if c is not None]
    return Stored(found, True, len(found) < len(items), _when(data.get("synced_at")), data.get("complete", True) is not False,
                  str(data["last_error"]) if data.get("last_error") else None)


def load(file: Path) -> list[Contact]:
    """The stored contacts; an absent or unreadable file is an empty list (its state is in `read`)."""
    return read(file).contacts


def health(stored: Stored, now: datetime) -> str | None:
    """The data-status code the daily mail reports for the address book, or None when the copy is whole and fresh."""
    if not stored.exists:
        return None
    if stored.invalid:
        return "invalid-file"
    if stored.last_error:
        return f"sync-{stored.last_error}"
    if not stored.complete:
        return "partial"
    if stored.synced_at is not None and now - stored.synced_at > timedelta(days=STALE_DAYS):
        return "stale"
    return None


def synced_at(file: Path, key: str = "synced_at") -> datetime | None:
    try:
        return _when(json.loads(file.read_text(encoding="utf-8-sig"))[key]) if file.exists() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def sync_due(file: Path, now: datetime) -> bool:
    """Once a day, counted from the last attempt, so a failing sync is not retried at every scheduler tick."""
    last = synced_at(file, "checked_at") or synced_at(file)
    return last is None or (now - last).total_seconds() >= SYNC_EVERY_HOURS * 3600


def sync(file: Path, token: str, *, now: datetime, getter: gmail.Getter = gmail.get) -> dict[str, Any]:
    """Fetch and store under the address book's own lock; never raises. Returns {"outcome": "synced" | "partial" |
    "error" | "busy", "count", "error_code"}. A failed fetch keeps the previous copy and records the failure on it, so
    yesterday's contacts still serve today's mail and the mail says the copy is behind."""
    with _locked(file) as held:
        if not held:
            return {"outcome": "busy", "count": 0, "error_code": "sync-running"}
        try:
            found, complete = fetch(token, getter=getter)
        except ContactsError as error:
            try:
                raw = json.loads(file.read_text(encoding="utf-8-sig")) if file.exists() else {}
            except (OSError, ValueError):
                raw = {}
            _try_write(file, {**(raw if isinstance(raw, dict) else {}), "checked_at": now.isoformat(),
                              "last_error": error.code, "last_error_at": now.isoformat()})
            return {"outcome": "error", "count": 0, "error_code": error.code}
        try:
            save(file, found, now=now, complete=complete)
        except OSError:
            return {"outcome": "error", "count": 0, "error_code": "write-failed"}
    return {"outcome": "synced" if complete else "partial", "count": len(found),
            "error_code": None if complete else "page-limit"}


def _try_write(file: Path, payload: dict[str, Any]) -> None:
    try:
        _write(file, payload)
    except OSError:
        pass   # The failure being recorded is already in the result; a full disk must not raise past the sync.


def _write(file: Path, payload: dict[str, Any]) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_name(f".{file.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, file)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _locked(file: Path) -> Iterator[bool]:
    """A process lock for the address book copy (manual and scheduled syncs may overlap). Yields False when another
    live sync holds it; a lock older than LOCK_STALE_SECONDS belongs to a crashed run and is taken over."""
    lock = file.with_name(file.name + ".lock")
    file.parent.mkdir(parents=True, exist_ok=True)
    held = _acquire(lock)
    try:
        yield held
    finally:
        if held:
            lock.unlink(missing_ok=True)


def _acquire(lock: Path) -> bool:
    for _ in range(2):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime < LOCK_STALE_SECONDS:
                    return False
                lock.unlink()
            except OSError:
                return False
    return False


def _contact(item: Any) -> Contact | None:
    if not isinstance(item, dict):
        return None
    try:
        birthday, lunar = item.get("birthday"), item.get("lunar_birthday")
        return Contact(str(item["id"]), str(item["name"]), tuple(str(n) for n in item.get("nicknames") or []),
                       (int(birthday[0]) if birthday[0] is not None else None, int(birthday[1]), int(birthday[2]))
                       if birthday else None,
                       (int(lunar[0]), int(lunar[1])) if lunar else None,
                       tuple((str(e[0]), int(e[1]) if e[1] is not None else None, int(e[2]), int(e[3]))
                             for e in item.get("events") or []),
                       str(item.get("note", "")), str(item.get("organization", "")), bool(item.get("starred", False)))
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


# --- what the rest of Smith takes from the contacts --------------------------------------------------------------------

def person_id(contact: Contact) -> str:
    """The id a contact carries in the date book; prefixed so it never collides with the household's own entries.
    Reminder keys built on it reach the writer model, so the resource id (a long hex/digit run that an identifier rule
    could read as a phone or account number) is replaced by a stable base32 digest without digit runs."""
    digest = base64.b32encode(hashlib.sha256(contact.id.encode("utf-8")).digest()[:10]).decode("ascii").lower()
    return f"contact-{digest}"


def for_reminders(found: list[Contact], policy: str) -> list[Contact]:
    """The contacts whose dates join the reminders: "starred" (즐겨찾기), "noted" (starred, or a note or 중요한 날 written:
    the default) or "all". Search, replies and calendar lines use every contact regardless."""
    if policy == "all":
        return list(found)
    if policy == "starred":
        return [c for c in found if c.starred]
    return [c for c in found if c.starred or c.note or c.events]


def as_book(found: list[Contact]) -> people.Book:
    """Contacts as people and dates, so `people.reminders`/`upcoming` treat them like the household's own entries.
    A contact's label is their name (the point of remembering them)."""
    persons, dates = [], []
    for contact in found:
        pid = person_id(contact)
        birth = None
        if contact.lunar_birthday:
            birth = people.Birth("lunar", contact.lunar_birthday[0], contact.lunar_birthday[1],
                                 contact.birthday[0] if contact.birthday else None)
        elif contact.birthday:
            birth = people.Birth("solar", contact.birthday[1], contact.birthday[2], contact.birthday[0])
        persons.append(people.Person(pid, contact.name, "acquaintance", birth))
        for index, (label, year, month, day) in enumerate(contact.events):
            try:
                date(2000, month, day)
            except ValueError:
                continue
            dates.append(people.Dated(f"{pid}-event-{index}", f"{contact.name} {label}", "anniversary", month, day, year,
                                      False, None, pid))
    return people.Book(tuple(persons), tuple(dates))


def merge(book: people.Book, found: list[Contact]) -> people.Book:
    extra = as_book(found)
    return people.Book(book.people + extra.people, book.dates + extra.dates)


# What may follow a name and still be about that person: particles, honorifics, titles (up to three in a row, as in
# "김준호님과"). Anything else glued to the name makes it part of another word ("이모" in "이모티콘").
_SUFFIXES = tuple(sorted((
    "님", "씨", "께서", "께", "에게", "한테", "이랑", "랑", "하고", "과", "와", "이", "가", "은", "는", "을", "를", "의", "도",
    "만", "네", "형", "누나", "오빠", "언니", "선배", "후배", "팀장", "부장", "과장", "차장", "대리", "실장", "본부장", "대표",
    "이사", "상무", "전무", "사장", "회장", "교수", "박사", "선생", "원장", "변호사", "의원", "작가", "감독"), key=len, reverse=True))
# A question that asks who someone is from a clue ("한빛상사 다니는 사람 누구였지?") rather than naming them.
_REVERSE = re.compile(r"누구|누가|누군지|어떤\s*(?:사람|분)|어느\s*분")
_ASKING = {"누구", "누가", "누군", "군지", "어떤", "사람", "어느", "였지", "더라", "이었", "었지", "있었", "알려", "려줘"}
REVERSE_MIN_OVERLAP = 2


def keys(contact: Contact) -> list[str]:
    """The words that name this contact in a title or a question: the display name and nicknames of two or more
    characters. A given name alone ("준호") is not a key: it is too often an ordinary word."""
    found: list[str] = []
    for key in (contact.name, *contact.nicknames):
        key = key.strip()
        if len(key) >= 2 and key not in found:
            found.append(key)
    return found


def matches(found: list[Contact], text: str) -> tuple[list[Contact], list[str]]:
    """(contacts `text` names unambiguously, keys that name two or more contacts). A key counts only as a whole word
    (start of text or a non-letter before it; the end, a non-letter or a particle/title after it). A key shared by
    several contacts (동명이인, a common nickname) picks nobody: it is returned as ambiguous instead."""
    owners: dict[str, list[Contact]] = {}
    for contact in found:
        for key in keys(contact):
            owners.setdefault(key, []).append(contact)
    hits: list[Contact] = []
    ambiguous: list[str] = []
    for key in sorted(owners, key=len, reverse=True):
        if not _names(text, key):
            continue
        if len(owners[key]) > 1:
            ambiguous.append(key)
        elif owners[key][0] not in hits:
            hits.append(owners[key][0])
    return hits, ambiguous


def mentioned(found: list[Contact], text: str) -> list[Contact]:
    """Contacts `text` names unambiguously, longest key first (see `matches`)."""
    return matches(found, text)[0]


def search(found: list[Contact], query: str, *, limit: int = MODEL_PEOPLE) -> list[Contact]:
    """Contacts a question is about: the ones it names unambiguously. Only a question that asks who someone is
    ("누구", "어떤 사람") may find people by their note or organisation, and then only with at least
    REVERSE_MIN_OVERLAP word pairs in common beyond the asking words; a note's chance overlap never selects anyone."""
    named = mentioned(found, query)
    if named or not _REVERSE.search(query):
        return named[:limit]
    wanted = _bigrams(query) - _ASKING
    scored = []
    for contact in found:
        hit = len(_bigrams(f"{contact.note} {contact.organization}") & wanted)
        if hit >= REVERSE_MIN_OVERLAP:
            scored.append((hit, contact.name, contact))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, _, c in scored][:limit]


def for_model(found: list[Contact]) -> list[dict[str, Any]]:
    """What a model receives about a person: name, nicknames, organization, the masked note, dates as month-day only."""
    rows = []
    for contact in found:
        birthday = [f"{contact.birthday[1]:02d}-{contact.birthday[2]:02d}"] if contact.birthday else []
        birthday += ["음력 {}/{}".format(*contact.lunar_birthday)] if contact.lunar_birthday else []
        rows.append({"name": contact.name, "nicknames": list(contact.nicknames), "organization": contact.organization or None,
                     "note": contact.note, "birthday": " · ".join(birthday) or None,
                     "events": [f"{label} {month:02d}-{day:02d}" for label, _, month, day in contact.events]})
    return rows


def about_line(contact: Contact) -> str:
    """The one line under a calendar event naming this person: organization, then the start of the note."""
    parts = [p for p in (contact.organization, contact.note[:ABOUT_CHARS]) if p]
    return " · ".join(parts) if parts else "주소록에 메모가 없습니다"


def about_events(found: list[Contact], titles: list[str], *, per_event: int = 2) -> list[list[tuple[str, str, str]]]:
    """For each event title, the (person id, name, about line) of up to `per_event` contacts it names unambiguously."""
    return [[(person_id(c), c.name, about_line(c)) for c in mentioned(found, title)[:per_event]] for title in titles]


def names_in(text: str, contact: Contact) -> bool:
    """Whether `text` refers to this contact by any key or by the given name alone (for keeping facts about them out of
    the client's memory, where a false positive only drops a fact)."""
    given = [contact.name[1:]] if len(contact.name) in (3, 4) and re.fullmatch(r"[가-힣]+", contact.name) else []
    return any(k and k in text for k in (*keys(contact), *given))


def _names(text: str, key: str) -> bool:
    start = text.find(key)
    while start != -1:
        before = text[start - 1] if start else ""
        if not before.isalnum() and _word_ends(text[start + len(key):]):
            return True
        start = text.find(key, start + 1)
    return False


def _word_ends(rest: str) -> bool:
    for _ in range(4):
        if not rest or not rest[0].isalnum():
            return True
        suffix = next((s for s in _SUFFIXES if rest.startswith(s)), None)
        if suffix is None:
            return False
        rest = rest[len(suffix):]
    return False


def _get(url: str, token: str, getter: gmail.Getter) -> dict[str, Any]:
    try:
        status, body = getter(url, {"Authorization": f"Bearer {token}"})
    except (OSError, http.client.HTTPException):   # Includes timeouts and a body cut short (IncompleteRead).
        raise ContactsError("network-error") from None
    except ValueError:                              # A body that is not JSON.
        raise ContactsError("bad-response") from None
    if status == 200:
        return body if isinstance(body, dict) else {}
    if status == 401:
        raise ContactsError("login-required")
    if status == 403:
        error = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
        # The legacy shape names accessNotConfigured; the google.rpc shape carries SERVICE_DISABLED in details.
        reasons = {str(e.get("reason", "")) for key in ("errors", "details") for e in error.get(key) or [] if isinstance(e, dict)}
        raise ContactsError("api-not-enabled" if reasons & {"accessNotConfigured", "SERVICE_DISABLED"} else "no-scope")
    raise ContactsError(f"http-{status}")


def _items(person: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = person.get(key)
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _month_day(when: Any) -> tuple[int | None, int, int] | None:
    if not isinstance(when, dict):
        return None
    try:
        month, day = int(when.get("month") or 0), int(when.get("day") or 0)
        year = int(when["year"]) if when.get("year") else None
    except (TypeError, ValueError):
        return None
    return (year, month, day) if 1 <= month <= 12 and 1 <= day <= 31 else None


def _bigrams(text: str) -> set[str]:
    grams = set()
    for word in re.findall(r"[0-9A-Za-z가-힣]+", text.lower()):
        grams |= {word[i:i + 2] for i in range(len(word) - 1)} or {word}
    return grams
