"""`smith evernote`: read the allowed Evernote notebooks and spaces through the Aside browser, and look at the copy."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith import evernote, keep
from smith.config import load_config, local_time

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("evernote", help="Evernote notes read through the Aside browser (the client's own notes)")
    command.add_argument("action", choices=("sync", "status", "search"),
                         help="sync: read the allowed notebooks and spaces now (Aside must be running and signed in "
                              "to Evernote; takes 10-15 minutes); status: what is stored; search WORDS: the notes about something")
    command.add_argument("words", nargs="*", help="search: the words")
    command.add_argument("--config", type=Path, default=DEFAULT_LOCAL_CONFIG)
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    folder = evernote.directory(args.db)
    if args.action == "search":
        query = " ".join(args.words)
        if not query:
            print("Give the words to look for: smith evernote search 파이어족")
            return 2
        found = keep.search(evernote.notes(folder), query, limit=8)
        print(f"{len(found)} note(s) about {query!r}")
        for entry in found:
            print(f"  [{entry.labels[0] if entry.labels else '-'}] {entry.on or '----'} {entry.text[:120]}")
        return 0
    if args.action == "status":
        return _status(folder)
    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"Missing setup: local config missing or invalid: {args.config}")
        return 2
    settings = config.get("evernote", {})
    if not (settings.get("notebooks") or settings.get("spaces")):
        print("No Evernote source allowed: list notebooks and/or spaces under [evernote] in the local config.")
        return 2
    now = datetime.now(timezone.utc)
    print("Reading Evernote through Aside (this takes 10-15 minutes; Aside opens and closes its own tab)...")
    result = evernote.sync(folder, list(settings.get("notebooks", [])), list(settings.get("spaces", [])), now=now,
                           today=local_time(now, config["app"]["timezone"]).date())
    print(describe(result))
    return 0 if result["outcome"] == "synced" else 1


def describe(result: dict[str, Any]) -> str:
    """One line per sync for the console and the run-due log (no note content)."""
    if result["outcome"] in ("error", "busy"):
        return f"evernote sync: {result['outcome']} ({result['error_code']}); the stored copy ({result['notes']} notes) stands"
    sources = ", ".join(f"{name} {r.get('found', '?')}/{r.get('expected', '?')}" + (f" ({r['error']})" if r.get("error") else "")
                        for name, r in result["sources"].items())
    return (f"evernote sync: {result['outcome']}, {result['notes']} notes stored ({result['read']} read, {result['kept']} kept "
            f"from before, {result['unread']} not read); sources: {sources}")


def _status(folder: Path) -> int:
    items = evernote.load_raw(folder)
    state = evernote.sync_state(folder)
    if not items:
        print("No Evernote notes stored yet: set [evernote] in the local config and run `smith evernote sync`.")
        return 0
    by_source: dict[str, int] = {}
    for item in items:
        by_source[str(item.get("source"))] = by_source.get(str(item.get("source")), 0) + 1
    print(f"{len(items)} notes: " + ", ".join(f"{name} {count}" for name, count in sorted(by_source.items())))
    print(f"last sync: {str(state.get('last_checked', '-'))[:16]} UTC, {state.get('last_outcome', '-')}"
          + (f" ({state['error_code']})" if state.get("error_code") else "")
          + (f", {state['unread']} not read" if state.get("unread") else ""))
    usable = evernote.notes(folder)
    print(f"notes a model may see: {len(usable)} ({len(items) - len(usable)} held back as sensitive); "
          f"entries searchable: {len(keep.entries(usable))}; "
          f"notes offered for the sentence of the day: {len(evernote.notes(folder, spaces=False))}")
    return 0
