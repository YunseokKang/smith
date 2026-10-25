"""`smith keep`: import a Google Takeout export of Keep and see which notes a morning would offer the writer."""
import argparse
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from smith import keep
from smith.config import local_time


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("keep", help="Google Keep notes from a Takeout export (the client's own thoughts)")
    command.add_argument("action", choices=("import", "status", "pick", "sync", "search"),
                         help="import PATH: a Takeout zip or folder; status: what is stored; pick: today's candidate notes; "
                              "sync: fetch the newest Takeout archive from Drive; search WORDS: the notes about something")
    command.add_argument("path", nargs="*", help="import: the Takeout zip or the extracted folder; search: the words")
    command.add_argument("--as-of", type=date.fromisoformat, help="pick: YYYY-MM-DD (default: today)")
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    folder = keep.directory(args.db)
    if args.action == "import":
        source = Path(args.path[0]) if args.path else None
        if source is None or not source.exists():
            print("Give the Takeout zip or the extracted folder: smith keep import \"C:\\Users\\me\\Downloads\\takeout-xxxx.zip\"")
            return 2
        try:
            result = keep.import_takeout(source, folder)
        except (OSError, ValueError) as error:
            print(f"Import failed: {error}")
            return 1
        print(f"Imported {result.imported} notes to {folder} (trashed skipped {result.trashed}, empty skipped {result.empty}).")
        return 0
    if args.action == "sync":
        return _sync(folder)
    notes = keep.load(folder)
    if args.action == "search":
        query = " ".join(args.path)
        if not query:
            print("Give the words to look for: smith keep search 와이프 선물")
            return 2
        found = keep.search(notes, query, limit=8)
        print(f"{len(found)} note(s) about {query!r}")
        for entry in found:
            print(f"  [{entry.id[:28]}] {entry.on or '----'} {entry.text[:120]}")
        return 0
    if args.action == "status":
        when = keep.imported_at(folder)
        found = keep.entries(notes)
        print(f"{len(notes)} notes, {len(found)} entries" + (f", imported {when:%Y-%m-%d %H:%M} UTC" if when else ", nothing imported yet")
              + f"; notes edited within a year: {keep.recent_count(notes)}; entries eligible as thoughts: "
              f"{sum(1 for e in found if keep._eligible(e))}; used recently: {len(keep.used(folder))}")
        return 0
    today = args.as_of or local_time(datetime.now(timezone.utc)).date()
    chosen = keep.thoughts(notes, day=today, context=f"{today:%m월 %d일} {'월화수목금토일'[today.weekday()]}요일", used=keep.used(folder))
    state = keep.sync_state(folder)
    if state:
        print(f"Drive sync: last checked {state.get('last_checked', '-')[:16]}, last export {state.get('last_stamp', '-')}")
    print(f"{today.isoformat()}: {len(chosen)} candidate note(s) for the sentence of the day")
    for entry in chosen:
        print(f"  [{entry.id[:28]}] {entry.on or '----'} {entry.text[:90]}")
    return 0


def _sync(folder: Path) -> int:
    from smith import credentials, drive, gmail

    stored = credentials.load_gmail()
    if stored is None:
        print("No Google grant stored. Run `smith mail login --read --daily --drive` first.")
        return 2
    try:
        token, scopes = gmail.access_token(stored)
    except gmail.MailError as error:
        print(f"Could not get a Google access token ({error.code}).")
        return 1
    if not drive.has_scope(scopes):
        print("The grant has no Drive read scope. Run `smith mail login --read --daily --drive` to re-consent.")
        return 1
    result = keep.sync(folder, token, now=datetime.now(timezone.utc))
    print(f"Keep sync: {result['outcome']}" + (f", export {result['stamp']}" if result["stamp"] else "")
          + (f", {result['imported']} notes" if result["outcome"] == "imported" else "")
          + (f" ({result['error_code']})" if result["error_code"] else ""))
    if result["outcome"] == "none":
        print("No Takeout archive in Drive yet: set up the scheduled export (Keep only, to Drive) at takeout.google.com.")
    return 0 if result["outcome"] in ("imported", "unchanged") else 1
