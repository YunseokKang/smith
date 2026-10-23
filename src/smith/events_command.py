"""`smith events`: the same-day event notes (report design §29). `check` is what `run-due` does at the daily
check time; `preview` saves one note as HTML and never sends; `status` lists the notes and the last check."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith import ledger
from smith.config import local_time


def add_parser(sub: Any, default_db: Path) -> None:
    events = sub.add_parser("events", help="Same-day event notes: check now, preview (never sends), status")
    events.add_argument("action", choices=("check", "preview", "status"))
    events.add_argument("--kind", choices=("bok", "move"), default="move",
                        help="preview: the latest rate meeting, or the holdings' move over the last day (default)")
    events.add_argument("--out", type=Path, help="preview: output HTML path (default: reports/event-<kind>-<time>.html)")
    events.add_argument("--research", action="store_true", help="preview: run the web research too (a model call)")
    events.add_argument("--no-sync", action="store_true", help="check: use stored data, no Toss or evidence refresh")
    events.add_argument("--force", action="store_true", help="check: run now even if today's check time has not come")
    events.add_argument("--db", type=Path, default=default_db)
    events.add_argument("--config", type=Path, default=Path("config/smith.local.toml"))


def run(args: argparse.Namespace) -> int:
    from smith.config import load_config

    if not args.db.exists():
        print(f"No ledger at {args.db}.")
        return 1
    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        print(f"Local config missing or invalid: {args.config} ({type(error).__name__})")
        return 2
    now = datetime.now(timezone.utc)
    if args.action == "status":
        return _status(args)
    if args.action == "preview":
        return _preview(args, config, now)
    return _check(args, config, now)


def _check(args: argparse.Namespace, config: dict[str, Any], now: datetime) -> int:
    from smith import credentials, events

    if not (config.get("events") or {}).get("enabled"):
        print("Event notes are disabled ([events] enabled = false).")
        return 0
    if not args.force:
        with closing(ledger.connect(args.db)) as conn:
            if events.slot_due(conn, config, now) is None:
                print("No event check due (use --force to check now).")
                return 0
    outcomes = events.check(args.db, config, now=now, reason="manual" if args.force else "slot", sync=not args.no_sync,
                            researcher=events.research_event, recipient=config.get("mail", {}).get("recipient"),
                            credentials=credentials.load_gmail(), secrets=credentials.all_secrets())
    if not outcomes:
        print("No event found.")
    for outcome in outcomes:
        print(f"{outcome['key']}: {outcome['status']} {outcome.get('detail') or ''}".rstrip())
    return 0


def _preview(args: argparse.Namespace, config: dict[str, Any], now: datetime) -> int:
    from smith import credentials, events

    researcher = events.research_event if args.research else None
    rendered = events.preview(args.db, config, args.kind, now=now, researcher=researcher,
                              secrets=credentials.all_secrets() if args.research else [])
    if rendered is None:
        print("Nothing to preview: no confirmed rate decision yet, or no valued listed holding a day ago.")
        return 1
    subject, html, _ = rendered
    out = args.out or Path("reports") / f"event-{args.kind}-{local_time(now, config['app']['timezone']):%Y%m%d-%H%M%S}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Saved (not sent): {out}\nSubject: {subject}")
    return 0


def _status(args: argparse.Namespace) -> int:
    with closing(ledger.connect_read_only(args.db)) as conn:
        last = ledger.last_event_check(conn)
        notes = ledger.event_notes(conn)
    print(f"Last check: {last.isoformat(timespec='minutes') if last else 'none'}")
    for note in notes:
        print(f"{note['event_key']}: {note['status']} (attempts {note['attempts']}) {note.get('subject') or note.get('error_code') or ''}".rstrip())
    if not notes:
        print("No event notes yet.")
    return 0
