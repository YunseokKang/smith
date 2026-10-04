"""Command line entry point. Network access is limited to read-only Toss requests; no LLM or email."""
import argparse
import getpass
import sqlite3
import sys
import tomllib
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from smith import ledger
from smith.config import load_config
from smith.importer import load_import_file
from smith.records import Action, ImportRejected, Kind

DEFAULT_DB = Path("data/smith.db")
_AMOUNT_FIELD = {Kind.ASSET: "value", Kind.LIABILITY: "outstanding_principal",
                 Kind.CASHFLOW: "amount", Kind.GOAL: "target_amount"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smith read-only personal wealth adviser")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-config", help="Validate configuration without external side effects")
    check.add_argument("--config", type=Path, required=True)
    load = sub.add_parser("import", help="Validate a manual JSON file and apply it to the ledger")
    load.add_argument("file", type=Path)
    load.add_argument("--db", type=Path, default=DEFAULT_DB)
    load.add_argument("--dry-run", action="store_true", help="Show planned changes without writing")
    show = sub.add_parser("records", help="List record states from the ledger")
    show.add_argument("--db", type=Path, default=DEFAULT_DB)
    show.add_argument("--as-of", type=_aware_datetime, help="ISO 8601 time with UTC offset (default: now)")
    show.add_argument("--known-at", type=_aware_datetime,
                      help="Use only revisions recorded by this time, to reproduce an earlier view (default: now)")
    toss = sub.add_parser("toss", help="Toss Securities read-only connection")
    toss.add_argument("action", choices=("login", "logout", "check"))
    toss.add_argument("--show-values", action="store_true",
                      help="check: also print amounts and symbols (for your own terminal only)")
    args = parser.parse_args(argv)
    commands = {"check-config": _check_config, "import": _import, "records": _records, "toss": _toss}
    return commands[args.command](args)


def _toss(args: argparse.Namespace) -> int:
    # Imported lazily so ledger commands work without the credential store.
    from smith import credentials
    from smith.toss import TossClient
    from smith.toss_check import run_check

    if args.action == "logout":
        credentials.delete_toss_client()
        print("Removed Toss credentials from the OS credential store.")
        return 0
    if args.action == "login":
        if not sys.stdin.isatty():
            print("Run `smith toss login` in an interactive terminal; secrets are never passed as arguments.")
            return 2
        client_id = getpass.getpass("Toss client ID (hidden): ").strip()
        client_secret = getpass.getpass("Toss client secret (hidden): ").strip()
        if not client_id or not client_secret:
            print("Both values are required. Nothing saved.")
            return 2
        credentials.save_toss_client(client_id, client_secret)
        print("Saved to the OS credential store (service smith.toss). Next: smith toss check")
        return 0
    stored = credentials.load_toss_client()
    if stored is None:
        print("No Toss credentials stored. Run `smith toss login` in your terminal first.")
        return 2
    return run_check(TossClient(*stored), show_values=args.show_values)


def _aware_datetime(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("include a UTC offset, e.g. 2026-09-27T09:00:00+09:00")
    return parsed


def _check_config(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        # Do not echo file contents: a local config may contain private fields.
        print("Configuration invalid or unreadable. Check the documented example.")
        return 2
    reports = config["reports"]
    print("Configuration valid. Financial access: read_only.")
    print(f"Report schedule: {', '.join(reports['weekdays'])} {reports['time']} ({config['app']['timezone']})")
    print("Scaffold only: no schedule registered and no email sent.")
    return 0


def _import(args: argparse.Namespace) -> int:
    try:
        batch = load_import_file(args.file)
    except OSError:
        print(f"Cannot read import file: {args.file}")
        return 2
    except ImportRejected as rejected:
        return _print_rejected(rejected)
    try:
        if args.dry_run:
            conn = ledger.connect_for_planning(args.db)
        else:
            args.db.parent.mkdir(parents=True, exist_ok=True)
            conn = ledger.connect(args.db)
        with closing(conn):
            result = ledger.apply_import(conn, batch, recorded_at=datetime.now(timezone.utc),
                                         dry_run=args.dry_run)
    except ImportRejected as rejected:
        return _print_rejected(rejected)
    except (OSError, sqlite3.Error, ledger.LedgerError) as error:
        print(f"Ledger error ({type(error).__name__}). No changes written.")
        return 1
    if result.outcome == "already_imported":
        print(f"Import {result.import_id} was already applied with identical content. No changes.")
        return 0
    print(f"Import {result.import_id}: {len(result.actions)} record(s)")
    for record_id, revision, action in result.actions:
        print(f"  {action:<10} {record_id} (revision {revision})")
    counts = Counter(action for _, _, action in result.actions)
    print(" ".join(f"{action}={counts[action]}" for action in Action))
    print("Dry run: no changes written." if result.outcome == "dry_run" else f"Applied to {args.db}.")
    return 0


def _print_rejected(rejected: ImportRejected) -> int:
    print(f"Import rejected: {len(rejected.problems)} problem(s). No changes written.")
    for problem in rejected.problems:
        print(f"  - {problem}")
    return 2


def _records(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"No ledger at {args.db}. Import a JSON file first.")
        return 1
    now = datetime.now(timezone.utc)
    as_of, known_at = args.as_of or now, args.known_at or now
    try:
        with closing(ledger.connect_read_only(args.db)) as conn:
            states = ledger.record_states(conn, as_of=as_of, known_at=known_at)
    except (sqlite3.Error, ledger.LedgerError) as error:
        print(f"Ledger error ({type(error).__name__}): not a readable Smith ledger.")
        return 1
    print(f"Records in effect at {as_of.isoformat()}, as known at {known_at.isoformat()} ({len(states)})")
    print(f"  {'kind':<10} {'id':<32} {'rev':>3}  {'status':<7} {'effective_at (UTC)':<26} {'currency':<8} amount")
    for state in states:
        record = state.record
        amount = record.fields.get(_AMOUNT_FIELD[record.kind]) or "-"
        effective = record.effective_at.astimezone(timezone.utc).isoformat(timespec="seconds")
        print(f"  {record.kind:<10} {record.record_id:<32} {record.revision:>3}  {record.status:<7} "
              f"{effective:<26} {record.fields.get('currency') or '-':<8} {amount}")
    return 0
