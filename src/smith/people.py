"""Family and friends' dates (FR-32): who to remember, when, and what the morning mail may say about it.

The client's people and dates live in `data/people.json`, next to the ledger and ignored by Git, so the daily mail
(FR-31) can say "아버지 생신 D-7" while no birth date ever reaches a model. The file is the client's to edit by hand;
`smith people check` validates it and `smith people list` shows the coming year.

Rules the client chose (2026-10-25):
- A lunar birthday is converted with the Korea Astronomy and Space Science Institute tables (the
  `korean_lunar_calendar` package: offline, years 1000-2050). In a year with an intercalary month (윤달) the plain
  month counts, and a 30th day in a 29-day lunar month falls back to the 29th; both are noted on the occurrence.
- Milestones use Korean age (year - birth year + 1): 환갑 61, 칠순 70, 팔순 80, 구순 90. A baby's 돌 is the first
  birthday; a wedding's 10·20·30주년 are milestones too.
- Reminders fall on D-7 and the day for birthdays, anniversaries and occasions, on D-30 as well for milestones, and on
  each entry's own `remind_days` for expiries (passport, licence, car inspection, lease end, check-up), default 30 and 7.

What a model receives (`for_model`): the label (a relation word, never a full name), the occurrence date, days left,
the milestone, and the age or 주년 only inside the gift window (D-30 and D-7), because the age is what a present fits.
"""
import json
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from korean_lunar_calendar import KoreanLunarCalendar

DATE_KINDS = ("anniversary", "occasion", "expiry")   # "birthday" occurrences derive from people.
MILESTONE_AGES = {61: "환갑", 70: "칠순", 80: "팔순", 90: "구순"}   # Korean age.
ANNIVERSARY_MILESTONE_EVERY = 10
REMIND_DEFAULT = (7, 0)
REMIND_MILESTONE = (30, 7, 0)
REMIND_EXPIRY = (30, 7)
GIFT_WINDOW = (30, 7)
WINDOW_DAYS = 400
ELDERS = ("father", "mother", "father-in-law", "mother-in-law", "grandfather", "grandmother")   # 생신, not 생일.
# Days the household keeps every year (the client's list, 2026-10-25). The file's "occasions" entries add to these,
# replace one by id, or drop one with {"id": ..., "disabled": true}.
DEFAULT_OCCASIONS: tuple[dict[str, Any], ...] = (
    {"id": "valentine", "label": "발렌타인데이", "month": 2, "day": 14},
    {"id": "white-day", "label": "화이트데이", "month": 3, "day": 14},
    {"id": "childrens-day", "label": "어린이날", "month": 5, "day": 5},
    {"id": "parents-day", "label": "어버이날", "month": 5, "day": 8},
    {"id": "christmas", "label": "크리스마스", "month": 12, "day": 25},
    {"id": "seollal", "label": "설날", "month": 1, "day": 1, "lunar": True},
    {"id": "chuseok", "label": "추석", "month": 8, "day": 15, "lunar": True},
)
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_FILE_KEYS = {"people", "dates", "occasions", "_note"}
_PERSON_KEYS = {"id", "label", "relation", "birth"}
_BIRTH_KEYS = {"calendar", "year", "month", "day"}
_DATE_KEYS = {"id", "label", "kind", "month", "day", "year", "date", "lunar", "remind_days", "person_id", "disabled"}
MAX_LABEL = 20


class PeopleError(ValueError):
    """The file is unreadable or breaks a rule; the message names the entry so the client can fix it."""


@dataclass(frozen=True)
class Birth:
    calendar: str   # "solar" | "lunar"
    month: int
    day: int
    year: int | None   # None when the client knows only the day: no age, no milestones.


@dataclass(frozen=True)
class Person:
    id: str
    label: str      # How the mail names them: a relation word ("아버지"), never a full name.
    relation: str
    birth: Birth | None


@dataclass(frozen=True)
class Dated:
    """An anniversary or occasion kept every year, or an expiry that happens once."""
    id: str
    label: str
    kind: str
    month: int
    day: int
    year: int | None   # Anniversary: the first year (for 주년); expiry: the year of its one date.
    lunar: bool
    remind_days: tuple[int, ...] | None
    person_id: str | None


@dataclass(frozen=True)
class Book:
    people: tuple[Person, ...]
    dates: tuple[Dated, ...]


@dataclass(frozen=True)
class Occurrence:
    key: str            # "birthday:father:2027": one reminder set per year, the ledger's deduplication handle.
    on: date
    kind: str           # birthday | anniversary | occasion | expiry
    label: str          # "아버지 생신", "결혼기념일", "여권 만료"
    person_id: str | None
    years: int | None   # Age (만) at a birthday, 주년 at an anniversary; None when the year is unknown.
    milestone: str | None   # "칠순", "돌", "10주년"
    note: str | None        # "음력 9월 4일 · 윤달 있는 해, 평달 기준"
    remind_days: tuple[int, ...]


@dataclass(frozen=True)
class Reminder:
    occurrence: Occurrence
    days_left: int


def path(db: Path) -> Path:
    """Next to the ledger (data/people.json for data/smith.db): private by the same rule as the ledger."""
    return Path(db).parent / "people.json"


def load(file: Path) -> Book:
    """The validated book; an absent file is an empty book, a broken one raises PeopleError.

    Raises:
        PeopleError: unreadable JSON, an unknown key, a duplicate or malformed id, an impossible date, a wrong kind.
    """
    if not file.exists():
        return parse({})
    try:
        raw = json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise PeopleError(f"{file.name}: {error}") from None
    return parse(raw)


def parse(raw: Any) -> Book:
    """Validate the JSON document (see `load`)."""
    if not isinstance(raw, dict):
        raise PeopleError("the file must hold one JSON object")
    _check_keys("file", raw, _FILE_KEYS)
    people = tuple(_person(item) for item in _list(raw, "people"))
    occasions = _occasions(_list(raw, "occasions"))
    dates = tuple(_dated(item) for item in _list(raw, "dates")) + occasions
    seen: set[str] = set()
    for key in [p.id for p in people] + [d.id for d in dates]:
        if key in seen:
            raise PeopleError(f"id used twice: {key}")
        seen.add(key)
    known = {p.id for p in people}
    for item in dates:
        if item.person_id is not None and item.person_id not in known:
            raise PeopleError(f"{item.id}: person_id {item.person_id!r} is not in people")
    return Book(people, dates)


def occurrences(book: Book, start: date, days: int = WINDOW_DAYS) -> list[Occurrence]:
    """Every occurrence from `start` through `start + days`, soonest first."""
    end = start + timedelta(days=days)
    found: list[Occurrence] = []
    for person in book.people:
        if person.birth is not None:
            found.extend(_birthdays(person, start, end))
    for item in book.dates:
        found.extend(_expiry(item, start, end) if item.kind == "expiry" else _yearly(item, start, end))
    return sorted(found, key=lambda o: (o.on, o.kind, o.label))


def reminders(book: Book, today: date) -> list[Reminder]:
    """The occurrences whose reminder falls on `today` (D-30, D-7 or the day, by kind)."""
    found = []
    for occurrence in occurrences(book, today, max(REMIND_MILESTONE + REMIND_EXPIRY) + 1):
        left = (occurrence.on - today).days
        if left in occurrence.remind_days:
            found.append(Reminder(occurrence, left))
    return found


def upcoming(book: Book, today: date, days: int = 14) -> list[Occurrence]:
    """The quiet strip: everything within `days`, reminders or not."""
    return occurrences(book, today, days)


def for_model(found: list[Reminder]) -> list[dict[str, Any]]:
    """The payload form: label, date, days left, milestone; the age or 주년 only inside the gift window."""
    rows = []
    for item in found:
        occurrence = item.occurrence
        row: dict[str, Any] = {"label": occurrence.label, "kind": occurrence.kind, "date": occurrence.on.isoformat(),
                               "days_left": item.days_left, "milestone": occurrence.milestone}
        if item.days_left in GIFT_WINDOW and occurrence.years is not None:
            row["years"] = occurrence.years
        if occurrence.note:
            row["note"] = occurrence.note
        rows.append(row)
    return rows


def lunar_to_solar(year: int, month: int, day: int) -> tuple[date, str | None] | None:
    """The solar date of a lunar day in lunar `year` (plain month; the 29th when the month is short), or None
    outside the tables (before 1000 or after 2050). The note says when a rule was applied."""
    calendar = KoreanLunarCalendar()
    notes = []
    if calendar.setLunarDate(year, month, day, True) or calendar.setLunarDate(year, month, 1, True):
        notes.append("윤달 있는 해, 평달 기준")
    if not calendar.setLunarDate(year, month, day, False):
        if day == 30 and calendar.setLunarDate(year, month, 29, False):
            notes.append("작은달이라 29일 기준")
        else:
            return None
    return date(calendar.solarYear, calendar.solarMonth, calendar.solarDay), " · ".join(notes) or None


def solar_day(year: int, month: int, day: int) -> tuple[date, str | None]:
    """A yearly solar date in `year`; 29 February becomes the 28th in a common year, noted."""
    try:
        return date(year, month, day), None
    except ValueError:
        if (month, day) == (2, 29):
            return date(year, 2, 28), "평년이라 2월 28일 기준"
        raise


def korean_age(birth_year: int, year: int) -> int:
    return year - birth_year + 1


def save(file: Path, raw: dict[str, Any]) -> Book:
    """Validate `raw` and write it atomically (temp file, then replace), so a bad edit never breaks the file."""
    book = parse(raw)
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_suffix(".json.tmp")
    temp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, file)
    return book


def read_raw(file: Path) -> dict[str, Any]:
    """The document as stored, for an edit that keeps the client's own ordering and notes; validated first."""
    load(file)
    return json.loads(file.read_text(encoding="utf-8-sig")) if file.exists() else {}


def _birthdays(person: Person, start: date, end: date) -> list[Occurrence]:
    birth = person.birth
    assert birth is not None
    word = "생신" if person.relation in ELDERS else "생일"
    found = []
    for year in range(start.year - 1, end.year + 2):
        if birth.calendar == "lunar":
            converted = lunar_to_solar(year, birth.month, birth.day)
            if converted is None:
                continue
            on, note = converted
            note = " · ".join(n for n in (f"음력 {birth.month}월 {birth.day}일", note) if n)
        else:
            on, note = solar_day(year, birth.month, birth.day)
        if not start <= on <= end:
            continue
        years = None if birth.year is None else year - birth.year
        milestone = None
        if birth.year is not None:
            milestone = MILESTONE_AGES.get(korean_age(birth.year, year)) or ("돌" if years == 1 else None)
        found.append(Occurrence(f"birthday:{person.id}:{year}", on, "birthday", f"{person.label} {word}", person.id,
                                years, milestone, note, REMIND_MILESTONE if milestone else REMIND_DEFAULT))
    return found


def _yearly(item: Dated, start: date, end: date) -> list[Occurrence]:
    found = []
    for year in range(start.year - 1, end.year + 2):
        if item.lunar:
            converted = lunar_to_solar(year, item.month, item.day)
            if converted is None:
                continue
            on, note = converted
            note = " · ".join(n for n in (f"음력 {item.month}월 {item.day}일", note) if n)
        else:
            on, note = solar_day(year, item.month, item.day)
        if not start <= on <= end:
            continue
        years = None if item.year is None else year - item.year
        milestone = None
        if item.kind == "anniversary" and years and years % ANNIVERSARY_MILESTONE_EVERY == 0:
            milestone = f"{years}주년"
        remind = item.remind_days or (REMIND_MILESTONE if milestone else REMIND_DEFAULT)
        found.append(Occurrence(f"{item.kind}:{item.id}:{year}", on, item.kind, item.label, item.person_id, years,
                                milestone, note, remind))
    return found


def _expiry(item: Dated, start: date, end: date) -> list[Occurrence]:
    assert item.year is not None
    on = date(item.year, item.month, item.day)
    if not start <= on <= end:
        return []
    return [Occurrence(f"expiry:{item.id}", on, "expiry", item.label, item.person_id, None, None, None,
                       item.remind_days or REMIND_EXPIRY)]


def _list(raw: dict[str, Any], key: str) -> list[Any]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise PeopleError(f"{key} must be a list")
    return value


def _check_keys(where: str, item: Any, allowed: set[str]) -> None:
    if not isinstance(item, dict):
        raise PeopleError(f"{where}: each entry must be an object")
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise PeopleError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _ident(where: str, item: dict[str, Any]) -> str:
    key = item.get("id")
    if not isinstance(key, str) or not _ID.match(key):
        raise PeopleError(f"{where}: id must be lowercase letters, digits and dashes (got {key!r})")
    return key


def _label(where: str, item: dict[str, Any]) -> str:
    label = item.get("label")
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_LABEL:
        raise PeopleError(f"{where}: label must be a short word such as 아버지 (1-{MAX_LABEL} characters)")
    return label.strip()


def _int(where: str, item: dict[str, Any], key: str, low: int, high: int, *, required: bool) -> int | None:
    value = item.get(key)
    if value is None:
        if required:
            raise PeopleError(f"{where}: {key} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PeopleError(f"{where}: {key} must be a whole number from {low} to {high}")
    return value


def _person(item: Any) -> Person:
    _check_keys("people", item, _PERSON_KEYS)
    key = _ident("people", item)
    label = _label(key, item)
    relation = item.get("relation", "")
    if not isinstance(relation, str):
        raise PeopleError(f"{key}: relation must be text")
    birth = None
    if item.get("birth") is not None:
        raw = item["birth"]
        _check_keys(f"{key}.birth", raw, _BIRTH_KEYS)
        calendar = raw.get("calendar", "solar")
        if calendar not in ("solar", "lunar"):
            raise PeopleError(f"{key}.birth: calendar must be solar or lunar")
        month = _int(f"{key}.birth", raw, "month", 1, 12, required=True)
        day = _int(f"{key}.birth", raw, "day", 1, 30 if calendar == "lunar" else 31, required=True)
        year = _int(f"{key}.birth", raw, "year", 1000, 2200, required=False)
        assert month is not None and day is not None
        if calendar == "solar":
            date(2000, month, day)   # A leap year: 29 February is allowed, 31 April is not.
        birth = Birth(calendar, month, day, year)
    return Person(key, label, relation.strip(), birth)


def _dated(item: Any) -> Dated:
    _check_keys("dates", item, _DATE_KEYS)
    key = _ident("dates", item)
    label = _label(key, item)
    kind = item.get("kind")
    if kind not in DATE_KINDS:
        raise PeopleError(f"{key}: kind must be one of {', '.join(DATE_KINDS)}")
    lunar = item.get("lunar", False)
    if not isinstance(lunar, bool):
        raise PeopleError(f"{key}: lunar must be true or false")
    remind = item.get("remind_days")
    if remind is not None:
        if (not isinstance(remind, list) or not remind
                or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 365 for d in remind)):
            raise PeopleError(f"{key}: remind_days must be a list of days before, such as [30, 7]")
        remind = tuple(sorted(set(remind), reverse=True))
    person_id = item.get("person_id")
    if person_id is not None and not isinstance(person_id, str):
        raise PeopleError(f"{key}: person_id must be text")
    if kind == "expiry":
        if lunar or "month" in item or "day" in item or "year" in item:
            raise PeopleError(f"{key}: an expiry takes one date (\"date\": \"YYYY-MM-DD\"), nothing else")
        try:
            when = date.fromisoformat(str(item.get("date")))
        except ValueError:
            raise PeopleError(f"{key}: date must be YYYY-MM-DD") from None
        return Dated(key, label, kind, when.month, when.day, when.year, False, remind, person_id)
    if "date" in item:
        raise PeopleError(f"{key}: a yearly date takes month and day (and year for 주년), not date")
    month = _int(key, item, "month", 1, 12, required=True)
    day = _int(key, item, "day", 1, 30 if lunar else 31, required=True)
    year = _int(key, item, "year", 1000, 2200, required=False)
    assert month is not None and day is not None
    if not lunar:
        date(2000, month, day)
    return Dated(key, label, kind, month, day, year, lunar, remind, person_id)


def _occasions(overrides: list[Any]) -> tuple[Dated, ...]:
    """The default occasions with the file's additions, replacements and `disabled` removals, in default order."""
    by_id = {str(o["id"]): dict(o, kind="occasion") for o in DEFAULT_OCCASIONS}
    for item in overrides:
        _check_keys("occasions", item, _DATE_KEYS)
        key = _ident("occasions", item)
        if item.get("disabled") is True:
            by_id.pop(key, None)
            continue
        merged = dict(by_id.get(key, {}), **{k: v for k, v in item.items() if k != "disabled"})
        merged.setdefault("kind", "occasion")
        if merged["kind"] != "occasion":
            raise PeopleError(f"{key}: an occasion's kind is occasion; put other dates under dates")
        by_id[key] = merged
    return tuple(_dated(item) for item in by_id.values())
