"""Command line entry point. Network access is read-only; the adviser runs in a restricted headless process."""
import argparse
import getpass
import json
import sqlite3
import sys
import tomllib
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from smith import ledger
from smith.config import DEFAULT_TIMEZONE, load_config, local_time
from smith.importer import load_import_file
from smith.records import Action, ImportBatch, ImportRejected, Kind

DEFAULT_DB = Path("data/smith.db")
# Manual files should use the same owner id so that API and manual records belong together.
DEFAULT_OWNER = "self"
_AMOUNT_FIELD = {Kind.ASSET: "value", Kind.LIABILITY: "outstanding_principal",
                 Kind.CASHFLOW: "amount", Kind.GOAL: "target_amount"}


def main(argv: list[str] | None = None) -> int:
    # Piped output on Korean Windows is cp949; external titles may hold characters it cannot encode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
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
    toss.add_argument("action", choices=("login", "logout", "check", "sync"))
    toss.add_argument("--show-values", action="store_true",
                      help="check/sync: also print amounts and symbols (for your own terminal only)")
    toss.add_argument("--db", type=Path, default=DEFAULT_DB)
    toss.add_argument("--owner", default=DEFAULT_OWNER, help=f"sync: owner id (default: {DEFAULT_OWNER})")
    toss.add_argument("--dry-run", action="store_true", help="sync: show planned changes without writing")
    toss.add_argument("--close-missing", action="store_true",
                      help="sync: close positions that the snapshot no longer reports (sold)")
    toss.add_argument("--allow-empty", action="store_true",
                      help="sync: with --close-missing, accept an answer with no holdings at all (you sold everything)")
    report = sub.add_parser("summary", help="Net worth, allocation, liquidity and cash flow from the ledger")
    report.add_argument("--db", type=Path, default=DEFAULT_DB)
    report.add_argument("--as-of", type=_aware_datetime, help="ISO 8601 time with UTC offset (default: now)")
    report.add_argument("--known-at", type=_aware_datetime, help="Use only data recorded by this time")
    report.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    macro = sub.add_parser("evidence", help="Korean and US rates from ECOS and FRED")
    macro.add_argument("action", choices=("login", "sync", "show"))
    macro.add_argument("--db", type=Path, default=DEFAULT_DB)
    macro.add_argument("--days", type=int, default=400, help="sync: history window in days (default: 400)")
    macro.add_argument("--as-of", type=date.fromisoformat, help="show: YYYY-MM-DD (default: today)")
    from smith import (advise_command, backup_command, doctor_command, followup_command, mail_command,
                       realestate_command, report_command)
    advise_command.add_parser(sub, DEFAULT_DB)
    backup_command.add_parser(sub, DEFAULT_DB)
    doctor_command.add_parser(sub, DEFAULT_DB)
    realestate_command.add_parser(sub, DEFAULT_DB)
    report_command.add_parser(sub, DEFAULT_DB)
    followup_command.add_parser(sub, DEFAULT_DB)
    mail_command.add_parser(sub)
    args = parser.parse_args(argv)
    commands = {"check-config": _check_config, "import": _import, "records": _records, "toss": _toss,
                "summary": _summary, "evidence": _evidence, "advise": advise_command.run,
                "report": report_command.run, "mail": mail_command.run,
                "research": followup_command.run_research, "proposal": followup_command.run_proposal,
                "realestate": realestate_command.run, "doctor": doctor_command.run, "backup": backup_command.run}
    return commands[args.command](args)


def _summary(args: argparse.Namespace) -> int:
    from smith.summary import build_summary, render, to_dict

    if not args.db.exists():
        print(f"No ledger at {args.db}. Import a JSON file or run `smith toss sync` first.")
        return 1
    now = datetime.now(timezone.utc)
    try:
        with closing(ledger.connect_read_only(args.db)) as conn:
            summary = build_summary(conn, as_of=local_time(args.as_of or now), known_at=args.known_at or now)
    except (sqlite3.Error, ledger.LedgerError) as error:
        print(f"Ledger error ({type(error).__name__}): not a readable Smith ledger.")
        return 1
    if args.json:
        print(json.dumps(to_dict(summary), ensure_ascii=False, indent=2))
    else:
        print("\n".join(render(summary)))
    return 0


def _toss(args: argparse.Namespace) -> int:
    # Imported lazily so ledger commands work without the credential store.
    from smith import credentials
    from smith.toss import TossClient, TossError
    from smith.toss_check import run_check
    from smith.toss_sync import SyncError, collect_snapshot

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
        if args.action == "sync" and not args.dry_run:
            # Recorded so that scheduled reports warn that holdings were not refreshed.
            _record_sync(args.db, datetime.now(timezone.utc), "failure", "no-credentials")
        return 2
    if args.action == "check":
        return run_check(TossClient(*stored), show_values=args.show_values)
    attempted_at = datetime.now(timezone.utc)
    try:
        batch = collect_snapshot(TossClient(*stored), owner_id=args.owner, collected_at=attempted_at)
    except (TossError, SyncError) as error:
        print(f"Toss sync failed: {error}. No records written.")
        code = error.code if isinstance(error, TossError) else "invalid-response"
        if not args.dry_run:
            _record_sync(args.db, attempted_at, "failure", code)
        return 1
    if args.close_missing and not batch.records and not args.allow_empty:
        # An empty but well-formed answer would close every position at once. Real "sold everything"
        # is rare and confirmed by hand; a provider hiccup must not wipe the holdings.
        print("Toss returned no holdings. Not closing every position; if you sold everything, run "
              "`smith toss sync --close-missing --allow-empty`.")
        if not args.dry_run:
            _record_sync(args.db, attempted_at, "failure", "empty-snapshot")
        return 1
    result = _apply_batch(batch, args.db, dry_run=args.dry_run, close_missing=args.close_missing,
                          list_records=args.show_values)
    if not args.dry_run:
        if result == 0:
            _record_sync(args.db, attempted_at, "success", import_id=batch.import_id)
        else:
            _record_sync(args.db, attempted_at, "failure", "not-applied")
    return result


def _evidence(args: argparse.Namespace) -> int:
    from smith import credentials, evidence

    if args.action == "login":
        if not sys.stdin.isatty():
            print("Run `smith evidence login` in an interactive terminal; keys are never passed as arguments.")
            return 2
        for provider in credentials.EVIDENCE_PROVIDERS:
            key = getpass.getpass(f"{provider.upper()} API key (hidden, Enter to keep current): ").strip()
            if key:
                credentials.save_api_key(provider, key)
        print("Saved to the OS credential store. Next: smith evidence sync")
        return 0
    if args.action == "show":
        return _evidence_show(args)
    today = local_time(datetime.now(timezone.utc)).date()
    failed = 0
    for provider in credentials.EVIDENCE_PROVIDERS:
        attempted_at = datetime.now(timezone.utc)
        key = credentials.load_api_key(provider)
        try:
            if key is None:
                raise evidence.EvidenceError(provider, "no-api-key")
            points = evidence.fetch_provider(provider, key, start=today - timedelta(days=args.days), end=today)
            # Data counts as known only once fully received, so `known_at` never sees it earlier.
            received_at = datetime.now(timezone.utc)
            args.db.parent.mkdir(parents=True, exist_ok=True)
            with closing(ledger.connect(args.db)) as conn:
                added = ledger.store_evidence(conn, points, retrieved_at=received_at)
        except evidence.EvidenceError as error:
            print(f"{provider}: failed ({error.code}). Nothing stored for this provider.")
            _record_sync(args.db, attempted_at, "failure", error.code, source=provider)
            failed += 1
            continue
        print(f"{provider}: {len(points)} observation(s) fetched, {added} new or revised")
        _record_sync(args.db, attempted_at, "success", source=provider)
    failed += _sync_feeds(args.db)
    return 1 if failed else 0


def _sync_feeds(db: Path) -> int:
    from smith import announcements

    failed = 0
    for feed in announcements.FEEDS:
        attempted_at = datetime.now(timezone.utc)
        try:
            items = announcements.fetch_feed(feed)
            db.parent.mkdir(parents=True, exist_ok=True)
            with closing(ledger.connect(db)) as conn:
                added = ledger.store_announcements(conn, items, retrieved_at=datetime.now(timezone.utc))
        except announcements.FeedError as error:
            print(f"{feed.feed_id}: failed ({error.code}). Nothing stored for this feed.")
            _record_sync(db, attempted_at, "failure", error.code, source=feed.feed_id)
            failed += 1
            continue
        print(f"{feed.feed_id}: {len(items)} item(s), {added} new")
        _record_sync(db, attempted_at, "success", source=feed.feed_id)
    return failed


def _evidence_show(args: argparse.Namespace) -> int:
    from smith import evidence

    if not args.db.exists():
        print(f"No ledger at {args.db}. Run `smith evidence sync` first.")
        return 1
    from smith.relevance import exposures

    now = datetime.now(timezone.utc)
    # A requested date is a household-local calendar day, ending at 23:59:59 local time.
    as_of = (datetime.combine(args.as_of, datetime.max.time(), ZoneInfo(DEFAULT_TIMEZONE)) if args.as_of
             else local_time(now))
    try:
        with closing(ledger.connect_read_only(args.db)) as conn, ledger.snapshot(conn):
            rows = evidence.describe(ledger.load_evidence(conn, known_at=now), as_of=as_of.date())
            status = ledger.sync_status(conn, known_at=now)
            links = exposures(conn, as_of=as_of, known_at=now)
            news = ledger.load_announcements(conn, since=as_of - timedelta(days=90), until=as_of, known_at=now)
    except (sqlite3.Error, ledger.LedgerError) as error:
        print(f"Ledger error ({type(error).__name__}): not a readable Smith ledger.")
        return 1
    print(f"{'series':<34} {'value':>9} {'observed':<10} {'3m chg':>7} {'12m chg':>8}  source")
    for row in rows:
        spec = row["spec"]
        value = f"{row['value']}{spec.unit if spec.unit == '%' else ''}" if row["value"] is not None else "-"
        print(f"{spec.label:<34} {value:>9} {str(row['observed_on'] or '-'):<10} {_change(row['change_3m']):>7} "
              f"{_change(row['change_12m']):>8}  {spec.source_url}{'  STALE' if row['stale'] else ''}")
    labels = {f"{s.provider}:{s.series_id}": s.label for s in evidence.SERIES}
    print("\nExposure links (amounts in KRW; interpretation is left to the adviser)")
    if not links.complete:
        missing = ", ".join(f"{currency} {amount}" for currency, amount in sorted(links.unconverted.items()))
        print(f"  PARTIAL: values without an FX rate are excluded ({missing}); shares are withheld")
    for link in links.exposures:
        share = f" ({link.share_of_assets * 100:.1f}% of assets)" if link.share_of_assets is not None else ""
        print(f"  {link.key:<40} {link.amount:>18,.0f}{share}")
        print(f"    {link.note}; evidence: {', '.join(labels.get(s, s) for s in link.series)}; "
              f"feeds: {', '.join(link.feeds)}")
    print("\nOfficial announcements, last 90 days (untrusted titles, shown as data)")
    for item in news[:15]:
        print(f"  {item.published_at.date()} {item.feed_id:<22} {item.title}  {item.link}")
    print()
    for provider in ("ecos", "fred") + tuple(f.feed_id for f in _feeds()):
        entry = status.get(provider, {})
        success, failure = entry.get("last_success"), entry.get("last_failure")
        print(f"sync {provider}: last success {success.isoformat(timespec='minutes') if success else 'never'}, "
              f"last failure {failure.isoformat(timespec='minutes') + ' (' + str(entry['last_error']) + ')' if failure else 'none'}")
    return 0


def _feeds() -> tuple:
    from smith.announcements import FEEDS
    return FEEDS


def _change(value: object) -> str:
    return "-" if value is None else f"{value:+}"


def _record_sync(db: Path, attempted_at: datetime, outcome: str, error_code: str | None = None,
                 import_id: str | None = None, source: str = "toss") -> None:
    # Failures are stored too, so `summary` can warn that the latest data was not refreshed.
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(ledger.connect(db)) as conn:
            ledger.record_sync_run(conn, source=source, attempted_at=attempted_at, outcome=outcome,
                                   error_code=error_code, import_id=import_id)
    except (OSError, sqlite3.Error, ledger.LedgerError) as error:
        print(f"Warning: could not record the sync outcome ({type(error).__name__}).")


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
    print("Check only: this command never schedules or sends anything.")
    return 0


def _import(args: argparse.Namespace) -> int:
    try:
        batch = load_import_file(args.file)
    except OSError:
        print(f"Cannot read import file: {args.file}")
        return 2
    except ImportRejected as rejected:
        return _print_rejected(rejected)
    return _apply_batch(batch, args.db, dry_run=args.dry_run)


def _apply_batch(batch: ImportBatch, db: Path, *, dry_run: bool, close_missing: bool = False,
                 list_records: bool = True) -> int:
    try:
        if dry_run:
            conn = ledger.connect_for_planning(db)
        else:
            db.parent.mkdir(parents=True, exist_ok=True)
            conn = ledger.connect(db)
        with closing(conn):
            result = ledger.apply_import(conn, batch, recorded_at=datetime.now(timezone.utc),
                                         dry_run=dry_run, close_missing=close_missing)
    except ImportRejected as rejected:
        return _print_rejected(rejected)
    except (OSError, sqlite3.Error, ledger.LedgerError) as error:
        print(f"Ledger error ({type(error).__name__}). No changes written.")
        return 1
    if result.outcome == "already_imported":
        print(f"Import {result.import_id} was already applied with identical content. No changes.")
        return 0
    print(f"Import {result.import_id}: {len(result.actions)} record(s), {len(batch.observations)} observation(s)")
    for record_id, revision, action in result.actions if list_records else ():
        print(f"  {action:<10} {record_id} (revision {revision})")
    counts = Counter(action for _, _, action in result.actions)
    print(" ".join(f"{action}={counts[action]}" for action in Action))
    print("Dry run: no changes written." if result.outcome == "dry_run" else f"Applied to {db}.")
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
