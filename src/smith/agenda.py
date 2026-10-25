"""Google Calendar events and Google Tasks for the daily mail (FR-31 ① ⑤): read-only, minimal fields, sanitized.

Verified against Google's docs on 2026-10-25: events come from `GET calendar/v3/calendars/primary/events` with
`singleEvents=true&orderBy=startTime` and a time window; the narrowest scopes are `calendar.events.readonly` and
`tasks.readonly`. Tasks come from `GET tasks/v1/users/@me/lists` and `GET tasks/v1/lists/{id}/tasks?showCompleted=false`;
the Calendar app's '할 일' are Google Tasks, and the client marks them done there (Smith never writes).

What is read is bounded on purpose (FR-30): only the primary calendar, only the day or window asked for, a `fields` mask
that leaves attendees, conference data and attachments out of the response, and tasks without their links. Text that
other people may have written (a description, a task note) is data: HTML, URLs, e-mail addresses, phone numbers, meeting
ids and passcodes are removed and the text is cut short before it can reach a model. A location is kept only when it is a
place name without digits, so a street address or a room number never travels.
"""
import http.client
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from smith import gmail
from smith.payload import mask_identifiers

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks.readonly"
SCOPES = (CALENDAR_SCOPE, TASKS_SCOPE)
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TASKS_URL = "https://tasks.googleapis.com/tasks/v1"
EVENT_FIELDS = "items(id,status,summary,description,location,start,end,eventType,transparency),nextPageToken"
TASK_FIELDS = "items(id,title,notes,due,status),nextPageToken"
MAX_EVENTS = 50          # One page; a day or a two-week window with more than this is noted, not paged further.
MAX_LISTS = 10
MAX_TASKS_PER_LIST = 100
MAX_TEXT = 200
MAX_TITLE = 100
OVERDUE_SHOWN = 5
TIGHT_GAP_MINUTES = 15
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HTML = re.compile(r"<[^>]{1,200}>")
# A meeting id or passcode: a label (Korean or English, long and short forms), an optional separator, then the value —
# one non-space token of any characters (codes carry _ # / . too) and up to three further digit groups (review
# 2026-10-25: "Zoom 비번: A_b#12 접속코드 abc-defg-hij" must not reach a model).
_ACCESS = re.compile(r"(?:회의\s*(?:id|아이디|번호|코드)|meeting\s*(?:id|code|number)|접속\s*(?:코드|번호|정보)|참가\s*코드|"
                     r"passcode|pass\s*code|password|pwd|pin|비밀번호|비번|암호|패스워드)\s*(?:[:：=]|은|는)?\s*"
                     r"\S{3,40}(?:\s+\d{2,6}){0,3}", re.IGNORECASE)


class AgendaError(Exception):
    """A read failed; `code` says how (network-error, login-required, no-scope, api-not-enabled, http-<status>)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Event:
    id: str
    title: str
    start: datetime | date   # A date for an all-day event; a household-local datetime otherwise.
    end: datetime | date     # Exclusive, as Google gives it: an all-day event on one day ends the next date.
    all_day: bool
    location: str | None     # A place name without digits, or None.
    description: str         # Sanitized, at most MAX_TEXT characters.
    event_type: str          # "default", "fromGmail" (a reservation Gmail added), "outOfOffice", ...
    busy: bool               # False when the event is marked "free" (transparency = transparent).


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    due: date | None
    notes: str
    list_title: str


def missing_scopes(granted: set[str]) -> list[str]:
    """The daily-mail scopes the grant lacks; empty when both are present."""
    return [scope for scope in SCOPES if scope not in granted]


def events(token: str, start: datetime, end: datetime, *, tz: str, getter: gmail.Getter = gmail.get) -> list[Event]:
    """Events of the primary calendar overlapping [start, end), in household time: all-day ones first, then by start.

    Raises:
        AgendaError: see the class; `no-scope` when the grant lacks calendar.events.readonly.
    """
    params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
              "orderBy": "startTime", "maxResults": str(MAX_EVENTS), "fields": EVENT_FIELDS, "timeZone": tz}
    page = _get(f"{CALENDAR_URL}?{urllib.parse.urlencode(params)}", token, getter)
    found = []
    for item in page.get("items", []) if isinstance(page.get("items"), list) else []:
        if not isinstance(item, dict) or item.get("status") == "cancelled":
            continue
        event = _event(item, tz)
        if event is not None:
            found.append(event)
    return sorted(found, key=_order)


def day_events(token: str, day: date, *, tz: str, getter: gmail.Getter = gmail.get) -> list[Event]:
    """The events of one household-local calendar day."""
    start = datetime.combine(day, time.min, ZoneInfo(tz))
    return events(token, start, start + timedelta(days=1), tz=tz, getter=getter)


def tasks(token: str, today: date, *, getter: gmail.Getter = gmail.get) -> list[Task]:
    """Open tasks due today or earlier across the client's lists, the nearest due date first.

    Tasks without a due date are not deadlines and are left out. Completed tasks are excluded by the request itself.
    """
    lists = _get(f"{TASKS_URL}/users/@me/lists?maxResults={MAX_LISTS}&fields=items(id,title)", token, getter)
    found = []
    due_max = datetime.combine(today + timedelta(days=1), time.min).isoformat() + "Z"
    for entry in lists.get("items", []) if isinstance(lists.get("items"), list) else []:
        list_id = str(entry.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", list_id):
            continue
        params = {"showCompleted": "false", "showHidden": "false", "dueMax": due_max,
                  "maxResults": str(MAX_TASKS_PER_LIST), "fields": TASK_FIELDS}
        page = _get(f"{TASKS_URL}/lists/{list_id}/tasks?{urllib.parse.urlencode(params)}", token, getter)
        list_title = clean(str(entry.get("title", "")), 40) or "할 일"
        for item in page.get("items", []) if isinstance(page.get("items"), list) else []:
            if not isinstance(item, dict) or item.get("status") == "completed":
                continue
            due = _due(item.get("due"))
            if due is None or due > today:
                continue
            found.append(Task(str(item.get("id", "")), clean(str(item.get("title") or "(제목 없음)"), MAX_TITLE), due,
                              clean(str(item.get("notes", "")), MAX_TEXT), list_title))
    return sorted(found, key=lambda t: (-t.due.toordinal() if t.due else 0, t.title))


def overdue(open_tasks: list[Task], today: date, limit: int = OVERDUE_SHOWN) -> list[Task]:
    """The unfinished tasks whose due date has passed, nearest first, at most `limit` (FR-31 ⑤)."""
    return [t for t in open_tasks if t.due is not None and t.due < today][:limit]


def due_today(open_tasks: list[Task], today: date) -> list[Task]:
    return [t for t in open_tasks if t.due == today]


def overlaps(day: list[Event]) -> list[tuple[Event, Event]]:
    """Pairs of timed, busy events whose times overlap."""
    timed = sorted((e for e in day if not e.all_day and e.busy), key=lambda e: e.start)
    found = []
    for i, first in enumerate(timed):
        for second in timed[i + 1:]:
            if second.start < first.end:   # type: ignore[operator]
                found.append((first, second))
    return found


def tight_gaps(day: list[Event], minutes: int = TIGHT_GAP_MINUTES) -> list[tuple[Event, Event, int]]:
    """Consecutive timed, busy events with less than `minutes` between them (and no overlap)."""
    timed = sorted((e for e in day if not e.all_day and e.busy), key=lambda e: e.start)
    found = []
    for first, second in zip(timed, timed[1:]):
        gap = int((second.start - first.end).total_seconds() // 60)   # type: ignore[operator]
        if 0 <= gap < minutes:
            found.append((first, second, gap))
    return found


def for_model(day: list[Event], open_tasks: list[Task], today: date) -> dict[str, Any]:
    """The payload form: titles, local times, sanitized text; no ids, no attendees, no links."""
    rows = []
    for event in day:
        row: dict[str, Any] = {"title": event.title, "all_day": event.all_day, "from_gmail": event.event_type == "fromGmail"}
        if event.all_day:
            last = event.end - timedelta(days=1)   # type: ignore[operator]
            if last > event.start:   # type: ignore[operator]
                row["until"] = last.isoformat()
        else:
            row["start"], row["end"] = event.start.strftime("%H:%M"), event.end.strftime("%H:%M")   # type: ignore[union-attr]
        if event.location:
            row["location"] = event.location
        if event.description:
            row["description"] = event.description
        rows.append(row)
    return {"events": rows,
            "tasks_overdue": [{"title": t.title, "due": t.due.isoformat(), "days_overdue": (today - t.due).days,
                               "notes": t.notes} for t in overdue(open_tasks, today) if t.due],
            "tasks_today": [{"title": t.title, "notes": t.notes} for t in due_today(open_tasks, today)],
            "overlaps": [[a.title, b.title] for a, b in overlaps(day)],
            "tight_gaps": [[a.title, b.title, gap] for a, b, gap in tight_gaps(day)]}


def clean(text: str, limit: int) -> str:
    """Other people's text made safe for a model: no HTML, links, identifiers or meeting access details."""
    text = _URL.sub(" ", _HTML.sub(" ", text.replace("&nbsp;", " ")))
    text = _ACCESS.sub("[접속 정보 제거]", mask_identifiers(text))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def place(text: Any) -> str | None:
    """A location kept only as a place name: anything with a digit (a street number, a room) or a removed
    identifier is dropped altogether."""
    cleaned = clean(str(text or ""), 60)
    if not cleaned or any(ch.isdigit() for ch in cleaned) or "[식별정보 제거]" in cleaned:
        return None
    return cleaned


def _event(item: dict[str, Any], tz: str) -> Event | None:
    start_raw, end_raw = item.get("start") or {}, item.get("end") or {}
    try:
        if "date" in start_raw:
            start: datetime | date = date.fromisoformat(str(start_raw["date"]))
            end: datetime | date = date.fromisoformat(str(end_raw.get("date", start_raw["date"])))
            all_day = True
        elif "dateTime" in start_raw:
            zone = ZoneInfo(tz)
            start = datetime.fromisoformat(str(start_raw["dateTime"])).astimezone(zone)
            end = datetime.fromisoformat(str(end_raw.get("dateTime", start_raw["dateTime"]))).astimezone(zone)
            all_day = False
        else:
            return None
    except (ValueError, TypeError):
        return None
    return Event(str(item.get("id", "")), clean(str(item.get("summary") or "(제목 없음)"), MAX_TITLE), start, end, all_day,
                 place(item.get("location")), clean(str(item.get("description") or ""), MAX_TEXT),
                 str(item.get("eventType") or "default"), item.get("transparency") != "transparent")


def _order(event: Event) -> tuple[int, str]:
    return (0 if event.all_day else 1, event.start.isoformat())


def _due(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def _get(url: str, token: str, getter: gmail.Getter) -> dict[str, Any]:
    try:
        status, payload = getter(url, {"Authorization": f"Bearer {token}"})
    except (OSError, http.client.HTTPException):
        raise AgendaError("network-error") from None
    if status == 200:
        return payload
    if status == 401:
        raise AgendaError("login-required")
    if status == 403:
        reasons = {str(e.get("reason", "")) for e in (payload.get("error") or {}).get("errors", []) if isinstance(e, dict)}
        if "accessNotConfigured" in reasons:
            raise AgendaError("api-not-enabled")
        if "insufficientPermissions" in reasons:
            raise AgendaError("no-scope")
    raise AgendaError(f"http-{status}")
