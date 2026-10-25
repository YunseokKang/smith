"""`smith agenda`: what the daily mail would read from Google Calendar and Google Tasks, sanitized, for checking."""
import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smith import agenda, credentials, gmail
from smith.config import DEFAULT_TIMEZONE, load_config, local_time

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")


def add_parser(sub: Any) -> None:
    command = sub.add_parser("agenda", help="Read-only view of the calendar day and open tasks (daily mail inputs)")
    command.add_argument("action", choices=("check", "events", "tasks"),
                         help="check: are the calendar and tasks scopes granted; events: the day's events; "
                              "tasks: open tasks due today or earlier")
    command.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default: today)")
    command.add_argument("--days", type=int, default=1, help="events: window in days from the date (default 1)")
    command.add_argument("--config", type=Path, default=DEFAULT_LOCAL_CONFIG)


def run(args: argparse.Namespace) -> int:
    stored = credentials.load_gmail()
    if stored is None:
        print("No Gmail grant stored. Run `smith mail login --read --daily --client-file <client.json>` first.")
        return 2
    tz = _timezone(args.config)
    try:
        token, scopes = gmail.access_token(stored)
    except gmail.MailError as error:
        print(f"Could not get a Google access token ({error.code}).")
        return 1
    missing = agenda.missing_scopes(scopes)
    if args.action == "check" or missing:
        print("Calendar and Tasks read scopes: granted." if not missing
              else f"Missing scope(s): {', '.join(m.rsplit('/', 1)[-1] for m in missing)}. "
                   "Run `smith mail login --read --daily` to re-consent (read access only).")
        if args.action == "check" or missing:
            return 0 if not missing else 1
    today = args.as_of or local_time(datetime.now(timezone.utc), tz).date()
    try:
        if args.action == "events":
            return _events(token, today, args.days, tz)
        return _tasks(token, today)
    except agenda.AgendaError as error:
        hints = {"api-not-enabled": "enable the Google Calendar API and Google Tasks API in the Cloud project",
                 "no-scope": "run `smith mail login --read --daily`", "login-required": "run `smith mail login` again"}
        print(f"Read failed ({error.code}){': ' + hints[error.code] if error.code in hints else ''}.")
        return 1


def _events(token: str, today: date, days: int, tz: str) -> int:
    for offset in range(max(days, 1)):
        day = today + timedelta(days=offset)
        found = agenda.day_events(token, day, tz=tz)
        print(f"{day.isoformat()} ({day.strftime('%a')}): {len(found)} event(s)")
        for event in found:
            when = "종일" if event.all_day else f"{event.start:%H:%M}–{event.end:%H:%M}"   # type: ignore[str-format]
            extra = [x for x in (event.location, f"[{event.event_type}]" if event.event_type != "default" else None,
                                 "" if event.busy else "(free)") if x]
            print(f"  {when:<13} {event.title}" + (f" · {' · '.join(extra)}" if extra else ""))
            if event.description:
                print(f"               {event.description}")
        for a, b in agenda.overlaps(found):
            print(f"  ! overlap: {a.title} / {b.title}")
        for a, b, gap in agenda.tight_gaps(found):
            print(f"  ! {gap} min between: {a.title} → {b.title}")
    return 0


def _tasks(token: str, today: date) -> int:
    found = agenda.tasks(token, today)
    late, now = agenda.overdue(found, today), agenda.due_today(found, today)
    print(f"{today.isoformat()}: {len(now)} due today, {len(late)} overdue shown (of {sum(1 for t in found if t.due and t.due < today)})")
    for task in now:
        print(f"  today     {task.title} ({task.list_title})")
    for task in late:
        print(f"  D+{(today - task.due).days:<6} {task.title} ({task.list_title}, due {task.due.isoformat()})")   # type: ignore[operator]
    if not found:
        print("  (none: tasks without a due date are not listed; the Calendar app's '할 일' need a date to appear)")
    return 0


def _timezone(config_path: Path) -> str:
    try:
        return str(load_config(config_path)["app"]["timezone"])
    except (OSError, ValueError, KeyError, UnicodeDecodeError):
        return DEFAULT_TIMEZONE
