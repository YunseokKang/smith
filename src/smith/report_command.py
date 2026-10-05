"""`smith report preview`: build a report from the ledger and save it as an HTML file. Never sends."""
import argparse
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from smith import ledger
from smith.config import DEFAULT_TIMEZONE
from smith.report_data import build_report
from smith.report_html import render


def add_parser(sub: Any, default_db: Path) -> None:
    report = sub.add_parser("report", help="Report preview (never sends) and publication (run-due, send-now)")
    report.add_argument("action", choices=("preview", "run-due", "send-now", "status", "activate"),
                        help="preview: save HTML only; run-due: send the due scheduled report (for the task "
                             "scheduler); send-now: publish immediately; status: list recent runs")
    report.add_argument("--kind", choices=("monday", "thursday"),
                        help="Edition (default: thursday on Thursdays, otherwise monday)")
    report.add_argument("--since", type=_aware_datetime,
                        help="Compare against the ledger as known at this time (default: first report, no comparison)")
    report.add_argument("--out", type=Path, help="Output HTML path (default: reports/preview-<time>.html)")
    report.add_argument("--narrative", action="store_true",
                        help="preview: add the AI narrative (uses the latest research brief; run-due and "
                             "send-now always research and narrate)")
    report.add_argument("--research", action="store_true", help="preview: research the web first (implies --narrative)")
    report.add_argument("--db", type=Path, default=default_db)
    report.add_argument("--config", type=Path, default=Path("config/smith.local.toml"),
                        help="run-due/send-now: local config with schedule and [mail] recipient")


def run(args: argparse.Namespace) -> int:
    if args.action == "status":
        return _status(args)
    if args.action == "activate":
        return _activate(args)
    if args.action in ("run-due", "send-now"):
        return _publish(args)
    if not args.db.exists():
        print(f"No ledger at {args.db}.")
        return 1
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    kind = args.kind or ("thursday" if local.weekday() == 3 else "monday")
    if args.since is not None and args.since >= now:
        print("Invalid --since: it must be in the past.")
        return 2
    try:
        brief_id = None
        if args.research:
            researched = _research(args.db, now, reuse_fresh=False)
            brief_id = researched["brief_id"] if researched["outcome"] == "success" else None
        with closing(ledger.connect_read_only(args.db)) as conn:
            data = build_report(conn, as_of=now, known_at=now, baseline=args.since, kind=kind)
    except (sqlite3.Error, ledger.LedgerError) as error:
        print(f"Ledger error ({type(error).__name__}): not a readable Smith ledger.")
        return 1
    if args.narrative or args.research:
        _narrator(args.db, now, brief_id)(data)
        print(f"Narrative: {data['narrative']['status']['outcome']}")
    subject, html = render(data)
    out = args.out or Path("reports") / f"preview-{local:%Y%m%d-%H%M%S}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Preview saved (not sent): {out}")
    print(f"Subject: {subject}")
    return 0


def _aware_datetime(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("include a UTC offset, e.g. 2026-10-01T06:00:00+09:00")
    return parsed


def _status(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"No ledger at {args.db}.")
        return 1
    now = datetime.now(timezone.utc)
    with closing(ledger.connect_read_only(args.db)) as conn:
        runs = ledger.report_runs(conn)
    for run_ in runs:
        started = run_["send_started_at"] if run_["status"] == "sending" else run_["created_at"]
        stale = run_["status"] in ("building", "sending") and \
            datetime.fromisoformat(started) < now - ledger.REPORT_LEASE
        print(f"{run_['created_at'][:16]}  slot={run_['slot'] or 'manual':<32} {run_['status']:<8} "
              f"attempts={run_['attempts']} {run_['error_code'] or ''}"
              + ("  ! interrupted: resolved on the next run-due" if stale else "")
              + ("  ! check the inbox: delivery not confirmed" if run_["status"] == "unknown" else ""))
    log = _log_path(args.db)
    if log.exists():
        print(f"Unattended run log: {log}")
    return 0


def _activate(args: argparse.Namespace) -> int:
    """Mark the slot that already passed so enabling the schedule does not send it late."""
    from smith import delivery
    from smith.config import load_config

    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"Local config missing or invalid: {args.config}")
        return 2
    with closing(ledger.connect(args.db)) as conn:
        slot = delivery.activate(conn, config, datetime.now(timezone.utc))
    print(f"Scheduled delivery starts now; past slot {slot:%Y-%m-%d %H:%M} will not be sent." if slot
          else "Scheduled delivery starts now.")
    return 0


# A brief researched this recently for the same topic set is reused (for example one prepared before
# the scheduled time) instead of researching again.
FRESH_BRIEF = timedelta(hours=3)
PREPARE_AHEAD = timedelta(hours=1)


def _research(db: Path, now: datetime, *, reuse_fresh: bool = True) -> dict[str, Any]:
    """Research generalized topics on the web (no amounts, holdings, accounts or direct identifiers; only
    the approved city/district and asset and debt types) and store the brief. Returns the outcome with
    the brief_id the report must use."""
    from smith import credentials, research
    from smith.config import local_time
    from smith.payload import load_view

    with closing(ledger.connect_read_only(db)) as conn:
        view = load_view(conn, as_of=local_time(now), known_at=now)
        topics = [{"ref": t.ref, "title": t.title, "question": t.question} for t in research.topics(view)]
        fresh = ledger.latest_research(conn, since=now - FRESH_BRIEF, topics=topics) if reuse_fresh else None
    if fresh is not None:
        return {"brief_id": fresh["brief_id"], "outcome": "success", "items": len(fresh["brief"]["items"]),
                "reused": True}
    return research.research_and_store(db, view, now=now, today=local_time(now).date(),
                                       secrets=credentials.all_secrets())


def _narrator(db: Path, now: datetime, brief_id: str | None = None) -> Any:
    """The narrative stage as a callback on report data (6c/6d), pinned to `brief_id` when research
    succeeded; failures leave deterministic content."""
    from smith import credentials, narrative

    secrets = credentials.all_secrets()
    return lambda data: narrative.narrate(db, data["view"], data, now=now, secrets=secrets, brief_id=brief_id)


def _log_path(db: Path) -> Path:
    return db.parent / "report-runs.log"


def _say(db: Path, message: str, *, log: bool = True) -> None:
    """Print, and append to a log next to the ledger: the scheduler runs pythonw, which has no console."""
    print(message)
    if not log:
        return
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        with _log_path(db).open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {message}\n")
    except OSError:
        pass  # The ledger row (when there is one) still records the outcome.


def _publish(args: argparse.Namespace) -> int:
    try:
        return _publish_once(args)
    except Exception as error:  # noqa: BLE001 - an unattended run must leave a trace of any crash.
        _say(args.db, f"{args.action} crashed: {type(error).__name__}")
        raise


def _publish_once(args: argparse.Namespace) -> int:
    from smith import credentials, delivery
    from smith.cli import main as cli_main
    from smith.config import load_config

    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError):
        _say(args.db, f"{args.action}: local config missing or invalid: {args.config}")
        return 2
    tz = config["app"]["timezone"]
    now = datetime.now(timezone.utc)
    slot, missed = None, []
    kind = args.kind or ("thursday" if now.astimezone(ZoneInfo(tz)).weekday() == 3 else "monday")
    if args.action == "run-due":
        if not config["reports"]["enabled"]:
            print("Scheduled reports are disabled (reports.enabled = false).")
            return 0
        with closing(ledger.connect(args.db)) as conn:
            for run_ in delivery.recover(conn, now):
                _say(args.db, f"Interrupted run {run_['report_id']} ({run_['slot'] or 'manual'}, was {run_['was']}) closed.")
            found = delivery.due(conn, config, now)
        if found is None:
            upcoming = delivery.slots_between(config["reports"], tz, now, now + PREPARE_AHEAD)
            if upcoming:
                # Prepare: research ahead of the scheduled time so the report itself is not delayed.
                prepared = _research(args.db, now)
                if not prepared.get("reused"):
                    _say(args.db, f"Prepared research for {upcoming[0]:%m-%d %H:%M}: {prepared['outcome']}")
                return 0
            _say(args.db, "No report due.", log=False)  # Every 15 minutes: not worth a log line.
            return 0
        slot, missed = found
        kind = "thursday" if slot.astimezone(ZoneInfo(tz)).weekday() == 3 else "monday"
    recipient = config.get("mail", {}).get("recipient")
    stored = credentials.load_gmail()
    if recipient is None or stored is None:
        code = "setup-no-recipient" if recipient is None else "setup-no-gmail-grant"
        _say(args.db, f"{args.action}: missing setup ({code}): [mail] recipient and `smith mail login` are required.")
        if slot is not None:
            delivery.record_failure(args.db, now=now, slot=slot, missed=missed, kind=kind, code=code)
        return 2
    # Refresh data first. Failures are recorded in sync_runs and named in the report's delivery note.
    sync_failures = []
    if cli_main(["toss", "sync", "--db", str(args.db)]) != 0:
        sync_failures.append("토스증권 보유 내역")
    if cli_main(["evidence", "sync", "--db", str(args.db)]) != 0:
        sync_failures.append("금리·공식 발표")
    # Research before claiming the slot (it needs no lease), unless a fresh prepared brief exists.
    researched = _research(args.db, datetime.now(timezone.utc))
    _say(args.db, f"Research {researched['outcome']}{' (prepared)' if researched.get('reused') else ''}: "
                  f"{researched.get('items', researched.get('error_code'))}")
    brief_id = researched["brief_id"] if researched["outcome"] == "success" else None
    publish_at = datetime.now(timezone.utc)
    result = delivery.publish(args.db, recipient=recipient, credentials=stored, now=publish_at,
                              slot=slot, missed=missed, kind=kind, tz=tz, sync_failures=sync_failures,
                              narrator=_narrator(args.db, publish_at, brief_id))
    _say(args.db, f"Report {result['status']}: {result.get('subject') or ''} {result.get('error_code') or ''}".rstrip())
    return 0 if result["status"] in ("sent", "skipped") else 1
