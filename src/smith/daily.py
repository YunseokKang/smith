"""The daily-routine mail's inputs assembled (FR-31): one dict for the renderer, the writer model and the reply answers.

`collect` does the reading (weather, calendar, tasks, holidays, the household's dates) with every source failing on its
own, and `assemble` is the pure step from already-read pieces to the data dict, so tests cover the assembly without a
network. The finance line (⑦) is dates only: the next Bank of Korea rate meeting from the published table and the
statutory tax calendar below; amounts never enter this mail (FR-30).
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smith import agenda, contacts, credentials, datagokr, evidence, gmail, health, history, holidays, keep, people, weather
from smith.config import local_time

WEEKDAYS_KO = "월화수목금토일"
CONTACT_PREFIX = "contact-"   # Person ids of address-book entries in the merged date book (contacts.person_id).
AHEAD_DAYS = 14
# Statutory periods that recur every year (start month, day, end month, day, label). Dates only, no amounts; the mail
# calls them the usual schedule because a year's notice can shift a deadline (FR-17 wording rule).
TAX_CALENDAR = ((1, 15, 2, 28, "연말정산 간소화 자료·서류 제출"), (5, 1, 5, 31, "종합소득세 신고·납부"),
                (6, 16, 6, 30, "자동차세 1기분 납부"), (7, 16, 7, 31, "재산세 1기분 납부"),
                (9, 16, 9, 30, "재산세 2기분 납부"), (12, 1, 12, 15, "종합부동산세 납부"),
                (12, 16, 12, 31, "자동차세 2기분 납부"))


@dataclass(frozen=True)
class Inputs:
    """What `collect` read, or None where a source failed (its code is in `failures`)."""
    weather_report: weather.Report | None
    events: list[agenda.Event] | None
    tasks: list[agenda.Task] | None
    book: people.Book | None
    holiday_today: holidays.Holiday | None
    holidays_ahead: list[holidays.Holiday] | None
    failures: dict[str, str]
    # Named so as not to shadow the modules inside the class body (an annotation is evaluated after its default).
    history_events: list[history.Event] | None = None   # 역사의 오늘 candidates (Korean Wikipedia).
    notes: list[keep.Note] | None = None                 # The client's Keep notes (imported Takeout).
    notes_used: dict[str, date] = field(default_factory=dict)   # Note id -> day it last fed a sentence.
    weather_label: str = ""                              # The district the weather is read for (user request 2026-10-25).
    # Every synced contact (FR-36): the calendar's "이 분은" lines; their dates are already merged into `book`.
    address_book: list[contacts.Contact] = field(default_factory=list)


def collect(db: Path, config: dict[str, Any], *, now: datetime, day: date | None = None,
            getter: datagokr.Getter | None = None, calendar_getter: gmail.Getter = gmail.get) -> Inputs:
    """Read every source for `day` (default: the household's today). Nothing here raises for a source failure."""
    daily = config.get("daily", {})
    tz = config["app"]["timezone"]
    local_now = local_time(now, tz)
    day = day or local_now.date()
    failures: dict[str, str] = {}
    key = credentials.load_api_key("datagokr")
    report = None
    weather_label = str(daily.get("weather", {}).get("label", "") or "")
    if key is None:
        failures["weather"] = failures["holidays"] = "no-datagokr-key"
    elif "weather" not in daily:
        failures["weather"] = "not-configured"
    else:
        spot = dict(daily["weather"])
        spot["warning_keywords"] = tuple(spot["warning_keywords"])
        report = weather.fetch(key, weather.Location(**spot), now=local_now, getter=getter)
        failures.update({f"weather-{k}": v for k, v in report.failures.items()})
    events = tasks = None
    stored = credentials.load_gmail()
    if stored is None:
        failures["calendar"] = failures["tasks"] = "no-gmail-grant"
    else:
        try:
            token, scopes = gmail.access_token(stored)
            if agenda.missing_scopes(scopes):
                failures["calendar"] = failures["tasks"] = "no-scope"
            else:
                for name, read in (("calendar", lambda: agenda.day_events(token, day, tz=tz, getter=calendar_getter)),
                                   ("tasks", lambda: agenda.tasks(token, day, getter=calendar_getter))):
                    try:
                        value = read()
                        if name == "calendar":
                            events = value
                        else:
                            tasks = value
                    except agenda.AgendaError as error:
                        failures[name] = error.code
        except gmail.MailError as error:
            failures["calendar"] = failures["tasks"] = error.code
    book = None
    try:
        book = people.load(people.path(db))
    except people.PeopleError:
        failures["people"] = "invalid-file"
    # Contacts' dates join the household's for the people the policy picks (daily design §14); a broken people.json
    # still leaves the contacts' reminders standing. A broken, partial, failed-since or stale copy is reported, not hidden.
    stored = contacts.read(contacts.path(db))
    problem = contacts.health(stored, now)
    if problem:
        failures["contacts"] = problem
    address_book = stored.contacts
    if address_book:
        chosen = contacts.for_reminders(address_book, str(daily.get("contact_reminders", "noted")))
        book = contacts.merge(book if book is not None else people.parse({}), chosen)
    # Health follow-ups join as dates (FR-39): the exam's name and D-day only, never a finding or a value.
    try:
        book = health.merge_dates(book if book is not None else people.parse({}), health.load(health.path(db)), day)
    except health.HealthError:
        failures["health"] = "invalid-file"
    today_holiday, ahead = None, None
    if key is not None:
        try:
            today_holiday, ahead = holidays.around(key, day, cache=holidays.path(db), now=now, getter=getter,
                                                   days_ahead=AHEAD_DAYS)
        except datagokr.Error as error:
            failures["holidays"] = error.code
    events_today = None
    try:
        events_today = history.events_for(day, cache=history.path(db), now=now)
    except (OSError, ValueError) as error:
        failures["history"] = str(error)[:40]
    notes = keep.load(keep.directory(db))
    return Inputs(report, events, tasks, book, today_holiday, ahead, failures, events_today, notes, keep.used(keep.directory(db)),
                  weather_label, address_book)


def assemble(inputs: Inputs, *, day: date, tz: str, now: datetime, airways_sensitive: bool,
             news: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The data dict: deterministic figures and rule outcomes; a model may later add `prose` to it."""
    report = inputs.weather_report
    advice = None if report is None else weather.advise(report, airways_sensitive=airways_sensitive)
    events = inputs.events or []
    tasks = inputs.tasks or []
    book = inputs.book or people.parse({})
    ahead = inputs.holidays_ahead or []
    return {
        "day": day, "weekday": WEEKDAYS_KO[day.weekday()], "tz": tz, "generated_at": now, "airways_sensitive": airways_sensitive,
        "holiday": None if inputs.holiday_today is None else inputs.holiday_today.name,
        "rest_day": holidays.is_rest_day(day, inputs.holiday_today),
        "weather": None if report is None else {"report": report, "advice": advice, "label": inputs.weather_label},
        "events": events, "overlaps": agenda.overlaps(events), "tight_gaps": agenda.tight_gaps(events),
        "event_about": contacts.about_events(inputs.address_book, [e.title for e in events]),
        "contacts": list(inputs.address_book),   # For the writer's per-person notes; never sent whole.
        "tasks": {"overdue": agenda.overdue(tasks, day), "due_today": agenda.due_today(tasks, day),
                  "overdue_total": sum(1 for t in tasks if t.due is not None and t.due < day)},
        "reminders": people.reminders(book, day),
        "upcoming": people.upcoming(book, day, AHEAD_DAYS),
        "holidays_ahead": ahead, "long_weekends": holidays.long_weekends(ahead, day),
        "finance_dates": finance_dates(day),
        "history_events": list(inputs.history_events or []),
        "history_pick": history.pick(inputs.history_events or [], day),
        "thoughts": keep.thoughts(inputs.notes or [], day=day, used=inputs.notes_used,
                                  context=_context_line(day, inputs, advice, events)),
        "news": news, "prose": None,
        "failures": dict(inputs.failures),
    }


def _context_line(day: date, inputs: Inputs, advice: weather.Advice | None, events: list[agenda.Event]) -> str:
    """What today is like, for matching the client's notes: the weekday, the weather word, the day's titles and dates."""
    parts = [f"{day:%m월 %d일}", WEEKDAYS_KO[day.weekday()] + "요일"]
    if inputs.holiday_today is not None:
        parts.append(inputs.holiday_today.name)
    if advice is not None:
        parts += [advice.headline, *advice.items]
    parts += [e.title for e in events]
    parts += [r.occurrence.label for r in people.reminders(inputs.book or people.parse({}), day)]
    return " ".join(parts)


def finance_dates(day: date, ahead: int = AHEAD_DAYS) -> list[dict[str, Any]]:
    """Dates only (⑦): the next rate meeting inside the window and tax periods open or opening inside it."""
    found: list[dict[str, Any]] = []
    meeting = evidence.next_policy_meeting(day)
    if meeting is not None and (meeting - day).days <= ahead:
        found.append({"date": meeting, "label": "한국은행 금통위 (기준금리 결정)", "days": (meeting - day).days})
    for start_month, start_day, end_month, end_day, label in TAX_CALENDAR:
        for year in (day.year, day.year + 1):
            start, end = date(year, start_month, start_day), date(year, end_month, end_day)
            if end < day or start > day + timedelta(days=ahead):
                continue
            if start <= day:
                found.append({"date": end, "label": f"{label} 마감", "days": (end - day).days, "open": True})
            else:
                found.append({"date": start, "label": f"{label} 시작", "days": (start - day).days, "open": False})
            break
    return sorted(found, key=lambda f: f["date"])


def model_people(data: dict[str, Any], *, first: tuple[str, ...] = (), limit: int = contacts.MODEL_PEOPLE) -> set[str]:
    """The contacts (person ids) one model call may see, at most `limit` (FR-36): `first`, then today's and the nearest
    reminders, then the people named in today's calendar, then the coming dates. Everyone else stays in the mail, which
    code renders, and out of the model's input."""
    chosen: list[str] = []
    candidates = [*first, *(r.occurrence.person_id for r in sorted(data["reminders"], key=lambda r: r.days_left)),
                  *(pid for about in data.get("event_about") or [] for pid, _, _ in about),
                  *(o.person_id for o in data["upcoming"])]
    for pid in candidates:
        if pid and pid.startswith(CONTACT_PREFIX) and pid not in chosen and len(chosen) < limit:
            chosen.append(pid)
    return set(chosen)


def visible(person_id: str | None, allowed: set[str]) -> bool:
    """Whether an entry may reach a model: the household's own entries always, a contact's only when allowed."""
    return not (person_id or "").startswith(CONTACT_PREFIX) or person_id in allowed


def payload(data: dict[str, Any], *, people_limit: int = contacts.MODEL_PEOPLE) -> dict[str, Any]:
    """What the writer model and the reply answers receive: labels, times, rule outcomes; no ids, coordinates or links.
    Contacts appear for at most `people_limit` people (`model_people`), marked with `person` so a later step can tell
    them apart from the household's own entries."""
    allowed = model_people(data, limit=people_limit)
    reminders = [r for r in data["reminders"] if visible(r.occurrence.person_id, allowed)]
    upcoming = [o for o in data["upcoming"] if visible(o.person_id, allowed)]
    names = {contacts.person_id(c): c.name for c in data.get("contacts", [])} if allowed else {}
    weather_part = None
    if data["weather"] is not None:
        weather_part = weather.for_model(data["weather"]["report"], data["weather"]["advice"])
        weather_part["location"] = data["weather"].get("label") or None
    tasks = data["tasks"]
    agenda_part = agenda.for_model(data["events"], (tasks["overdue"] + tasks["due_today"]), data["day"])
    for row, about in zip(agenda_part["events"], data.get("event_about") or []):
        shown = [{"person": name, "line": line} for pid, name, line in about if pid in allowed]
        if shown:
            row["about"] = shown
    reminder_rows = people.for_model(reminders)
    for row, item in zip(reminder_rows, reminders):
        if (item.occurrence.person_id or "").startswith(CONTACT_PREFIX):
            row["person"] = names.get(item.occurrence.person_id or "", "")
    return {
        "date": data["day"].isoformat(), "weekday": data["weekday"], "holiday": data["holiday"], "rest_day": data["rest_day"],
        "weather": weather_part,
        "agenda": agenda_part,
        "tasks_overdue_total": tasks["overdue_total"],
        # The counts a writer may state (its numbers are checked against the input's number tokens).
        "counts": {"events": len(data["events"]), "tasks_overdue": tasks["overdue_total"], "tasks_today": len(tasks["due_today"]),
                   "reminders": len(data["reminders"]), "news": 0 if data["news"] is None else len(data["news"])},
        "reminders": reminder_rows,
        "upcoming": [{"label": o.label, "date": o.on.isoformat(), "days_left": (o.on - data["day"]).days, "milestone": o.milestone}
                     | ({"person": names.get(o.person_id or "", "")} if (o.person_id or "").startswith(CONTACT_PREFIX) else {})
                     for o in upcoming],
        "holidays_ahead": [{"date": h.day.isoformat(), "name": h.name} for h in data["holidays_ahead"]],
        "long_weekends": [[a.isoformat(), b.isoformat()] for a, b in data["long_weekends"]],
        "finance_dates": [{"date": f["date"].isoformat(), "label": f["label"], "days": f["days"]} for f in data["finance_dates"]],
        "news": None if data["news"] is None else [{"ref": n.get("ref"), "headline": n.get("headline"), "publisher": n.get("publisher"),
                                                     "published_on": n.get("published_on"), "detail": n.get("detail")}
                                                    for n in data["news"]],
        "history_events": history.for_model(data.get("history_events", [])),
        "thoughts": keep.for_model(data.get("thoughts", [])),
        "failures": data["failures"],
    }
