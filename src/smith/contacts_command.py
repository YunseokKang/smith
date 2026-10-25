"""`smith contacts`: sync Google Contacts (read-only, every contact; FR-36) and look people up."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith import contacts


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("contacts", help="People to remember: Google Contacts, read-only")
    command.add_argument("action", choices=("sync", "list", "search"),
                         help="sync: fetch the contacts; list: what is stored; search WORDS: find a person or a note")
    command.add_argument("words", nargs="*", help="search: a name, nickname or words from the note")
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    file = contacts.path(args.db)
    if args.action == "sync":
        return _sync(file)
    stored = contacts.read(file)
    found = stored.contacts
    if args.action == "list":
        when = stored.synced_at
        state = contacts.health(stored, datetime.now(timezone.utc))
        print(f"{len(found)} contact(s)" + (f", synced {when:%Y-%m-%d %H:%M} UTC" if when else ", not synced yet")
              + (f" [{state}]" if state else ""))
        for contact in found:
            dates = [f"생일 {contact.birthday[1]:02d}-{contact.birthday[2]:02d}"] if contact.birthday else []
            dates += [f"음력 {m}/{d}" for m, d in ([contact.lunar_birthday] if contact.lunar_birthday else [])]
            dates += [f"{label} {month:02d}-{day:02d}" for label, _, month, day in contact.events]
            print(f"  {'★ ' if contact.starred else ''}{contact.name}" + (f" ({', '.join(contact.nicknames)})" if contact.nicknames else "")
                  + (f" · {contact.organization}" if contact.organization else "") + (f" · {' · '.join(dates)}" if dates else "")
                  + (f"\n      {contact.note[:140]}" if contact.note else ""))
        return 0
    query = " ".join(args.words)
    if not query:
        print("Give a name or words: smith contacts search 준호")
        return 2
    hits = contacts.search(found, query, limit=5)
    print(f"{len(hits)} match(es) for {query!r}")
    for contact in hits:
        print(f"  {contact.name}: {contacts.about_line(contact)}")
    return 0


def _sync(file: Path) -> int:
    from smith import credentials, gmail

    stored = credentials.load_gmail()
    if stored is None:
        print("No Google grant stored. Run `smith mail login --read --daily --drive --contacts` first.")
        return 2
    try:
        token, scopes = gmail.access_token(stored)
    except gmail.MailError as error:
        print(f"Could not get a Google access token ({error.code}).")
        return 1
    if not contacts.has_scope(scopes):
        print("The grant has no Contacts read scope. Run `smith mail login --read --daily --drive --contacts` to re-consent.")
        return 1
    result = contacts.sync(file, token, now=datetime.now(timezone.utc))
    hints = {"api-not-enabled": "enable the People API in the Cloud project", "no-scope": "re-consent with --contacts",
             "login-required": "run smith mail login again"}
    hints["page-limit"] = f"more than {contacts.MAX_PAGES * 200} contacts: the copy is partial"
    hints["sync-running"] = "another sync holds the lock; try again shortly"
    print(f"Contacts sync: {result['outcome']}"
          + (f", {result['count']} contact(s)" if result["outcome"] in ("synced", "partial") else "")
          + (f" ({result['error_code']}: {hints.get(result['error_code'], '')})" if result["error_code"] else ""))
    return 0 if result["outcome"] == "synced" else 1
