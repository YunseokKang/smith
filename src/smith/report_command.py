"""`smith report`: preview (saves HTML, never sends), scheduled and manual publication (run-due, send-now),
delivery status and schedule activation."""
import argparse
import io
import sqlite3
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from smith import editions, ledger
from smith.config import DEFAULT_TIMEZONE
from smith.narrative import PIPELINES
from smith.report_data import build_report
from smith import report_html
from smith.report_html import render


def add_parser(sub: Any, default_db: Path) -> None:
    report = sub.add_parser("report", help="Report preview (never sends) and publication (run-due, send-now)")
    report.add_argument("action", choices=("preview", "compare", "run-due", "send-now", "status", "activate"),
                        help="preview: save HTML only; compare: save the single and team narratives of one "
                             "snapshot side by side (never sends); run-due: send the due scheduled report (for "
                             "the task scheduler); send-now: publish immediately; status: list recent runs")
    report.add_argument("--kind", choices=editions.KINDS,
                        help="Edition: overview (Mon), markets (Tue), property (Wed), rates (Thu), tax (Fri), "
                             "life (Sat), or the original full 'monday' / short 'thursday' report "
                             "(default: today's edition from [reports.editions])")
    report.add_argument("--since", type=_aware_datetime,
                        help="Compare against the ledger as known at this time (default: first report, no comparison)")
    report.add_argument("--out", type=Path, help="Output HTML path (default: reports/preview-<time>.html); "
                                                 "compare: output folder (default: reports/compare-<time>)")
    report.add_argument("--narrative", action="store_true",
                        help="preview: add the AI narrative (uses the latest research brief; run-due and "
                             "send-now always research and narrate)")
    report.add_argument("--research", action="store_true",
                        help="preview/compare: research the web first (implies --narrative)")
    report.add_argument("--pipeline", choices=PIPELINES,
                        help="preview: how the narrative is written (default: single; team is the compare experiment)")
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
    config, problem = _optional_config(args.config)
    if problem:
        print(problem)
        return 2
    tz = config["app"]["timezone"] if config else DEFAULT_TIMEZONE
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(tz))
    kind = args.kind or editions.for_day(config, local.date())
    if args.since is not None and args.since >= now:
        print("Invalid --since: it must be in the past.")
        return 2
    try:
        brief_id = None
        if args.research:
            researched = _research(args.db, now, reuse_fresh=False, kind=kind, regions=target_regions(config))
            brief_id = researched["brief_id"] if researched["outcome"] == "success" else None
        if args.action == "compare":
            return _compare(args, now=now, kind=kind, tz=tz, config=config, brief_id=brief_id)
        with closing(ledger.connect_read_only(args.db)) as conn:
            data = build_report(conn, as_of=now, known_at=now, baseline=args.since, kind=kind, tz=tz,
                                household=profile(config) if config else None, schedule=(config or {}).get("reports"))
    except (sqlite3.Error, ledger.LedgerError) as error:
        print(f"Ledger error ({type(error).__name__}): not a readable Smith ledger.")
        return 1
    if args.narrative or args.research or args.pipeline:
        _narrator(args.db, now, brief_id, args.pipeline or "single")(data)
        print(f"Narrative ({data['narrative']['status']['pipeline']}): {data['narrative']['status']['outcome']}")
    subject, html = render(data)
    out = args.out or Path("reports") / f"preview-{local:%Y%m%d-%H%M%S}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Preview saved (not sent): {out}")
    print(f"Subject: {subject}")
    # The mail no longer prints its reasons and data status (report design §18.1); the preview does.
    notes = report_html.system_notes(data)
    print(f"System notes ({len(notes)}, not in the mail; the e-mail answers use them):")
    for line in notes:
        print(f"  · {line}")
    return 0


def _compare(args: argparse.Namespace, *, now: datetime, kind: str, tz: str, config: dict[str, Any] | None,
             brief_id: str | None) -> int:
    from smith import compare, credentials

    out_dir = args.out or Path("reports") / f"compare-{now.astimezone(ZoneInfo(tz)):%Y%m%d-%H%M%S}"
    print("Writing both narratives from one snapshot (single and team, at the same time). Nothing is sent.")
    page = compare.run(args.db, now=now, kind=kind, tz=tz, baseline=args.since,
                       household=profile(config) if config else None, brief_id=brief_id,
                       secrets=credentials.all_secrets(), out_dir=out_dir)
    print(f"Comparison saved (not sent): {page}")
    return 0


def profile(config: dict[str, Any]) -> dict[str, Any] | None:
    """The household profile plus the local property map (acquisition data) and the stated risk
    preference ([advice]), for reports, tax and the narrative."""
    household = config.get("household")
    risk = config.get("advice", {}).get("risk_preference")
    # Properties and move targets count on their own: a config with only [targets] still plans the move.
    if household is None and risk is None and not config.get("properties") and not config.get("targets"):
        return None
    return {**(household or {}), "properties": config.get("properties") or {}, "targets": config.get("targets") or {},
            "risk_preference": risk}


def _optional_config(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """(config, problem) for preview: a missing file means defaults, but a broken one is an error, so a
    preview never silently differs from what the scheduled run would send."""
    from smith.config import load_config

    if not path.exists():
        return None, None
    try:
        return load_config(path), None
    except (OSError, ValueError, UnicodeDecodeError) as error:
        return None, f"Local config is invalid ({error}): {path}. Fix it or pass --config to another file."


def lineage(config_path: Path) -> dict[str, str | None]:
    """Config hash and code version for a report run's lineage. The code version is the checked-out
    commit read from .git (no subprocess: the scheduler runs without a console); None outside a checkout."""
    import hashlib

    try:
        config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()
    except OSError:
        config_sha256 = None
    root = Path(__file__).resolve().parents[2]
    commit, source = _git_commit(root), _source_digest(root)
    # The commit alone read the same for four reports sent from an uncommitted tree (review 2026-10-18).
    return {"config_sha256": config_sha256,
            "code_version": None if commit is None and source is None else f"{commit or 'nogit'}+src:{source or 'unknown'}"}


def _source_digest(root: Path) -> str | None:
    """A short digest of the package source as it runs (paths and bytes of src/smith/**/*.py); None when unreadable."""
    import hashlib

    digest = hashlib.sha256()
    try:
        package = root / "src" / "smith"
        for path in sorted(package.rglob("*.py")):
            digest.update(path.relative_to(package).as_posix().encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    except OSError:
        return None
    return digest.hexdigest()[:12]


def _git_commit(root: Path) -> str | None:
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="ascii").strip()
        if not head.startswith("ref: "):
            return head[:40] or None
        ref = head[5:]
        loose = git / ref
        if loose.exists():
            return loose.read_text(encoding="ascii").strip()[:40]
        for line in (git / "packed-refs").read_text(encoding="ascii").splitlines():
            if line.endswith(f" {ref}"):
                return line.split(" ", 1)[0][:40]
    except OSError:
        return None
    return None


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
# The product review team (smith.picks, about 20 minutes and a dozen model calls) runs in its own window well ahead
# of a slot, never in the prepare hour or the send path, so a report is never delayed by it (review 2026-10-20).
REVIEW_AHEAD = timedelta(hours=4)
REVIEW_LEAD = timedelta(hours=2)


def target_regions(config: dict[str, Any] | None) -> tuple[str, ...]:
    """The districts of the configured move-target complexes: the only part of a target a research question
    may carry (never a complex name)."""
    targets = (config or {}).get("targets") or {}
    return tuple(sorted({spec["region"] for spec in targets.values() if spec.get("region")}))


def _research(db: Path, now: datetime, *, reuse_fresh: bool = True, kind: str | None = None,
              regions: tuple[str, ...] = ()) -> dict[str, Any]:
    """Research generalized topics on the web (no amounts, accounts or direct identifiers; only the
    approved city/district, asset and debt types, and holdings' public names on the markets day) and store
    the brief. Only the topics of the edition `kind` are researched. Returns the outcome with the brief_id
    the report must use ("skipped" with no brief for a day without research topics)."""
    from smith import credentials, research
    from smith.config import local_time
    from smith.payload import load_view

    with closing(ledger.connect_read_only(db)) as conn:
        view = load_view(conn, as_of=local_time(now), known_at=now)
        topics = [{"ref": t.ref, "title": t.title, "question": t.question}
                  for t in research.topics(view, kind, regions)]
        fresh = ledger.latest_research(conn, since=now - FRESH_BRIEF, topics=topics) if reuse_fresh and topics else None
    if fresh is not None:
        return {"brief_id": fresh["brief_id"], "outcome": "success", "items": len(fresh["brief"]["items"]),
                "reused": True}
    return research.research_and_store(db, view, now=now, today=local_time(now).date(),
                                       secrets=credentials.all_secrets(), kind=kind, target_regions=regions)


def review_slot(reports: dict[str, Any], tz: str, now: datetime) -> datetime | None:
    """The scheduled slot whose product reviews should run now: the next slot between REVIEW_LEAD and
    REVIEW_AHEAD away. None inside the last REVIEW_LEAD (the prepare step and the report itself own that time)."""
    from smith import delivery

    ahead = delivery.slots_between(reports, tz, now, now + REVIEW_AHEAD)
    return ahead[0] if ahead and ahead[0] - now >= REVIEW_LEAD else None


def _review_products(db: Path, config: dict[str, Any], now: datetime, kind: str | None) -> None:
    """The product reviews a report of `kind` needs (smith.picks: the review team), run when none is fresh.
    A failure is logged and recorded; the report shows the proposal without the picks table."""
    from smith import credentials, picks
    from smith.config import local_time
    from smith.payload import load_view

    try:
        with closing(ledger.connect_read_only(db)) as conn:
            view = load_view(conn, as_of=local_time(now), known_at=now)
            specs = picks.due(view, kind, conn, now)
        for spec in specs:
            outcome = picks.review_and_store(db, view, spec, profile(config), now=now, today=local_time(now).date(),
                                             secrets=credentials.all_secrets())
            detail = (f"picks {outcome['picks']}, verified {outcome['verified']}" if outcome["outcome"] == "success"
                      else outcome.get("error_code", ""))
            _say(db, f"Product review ({spec.key}): {outcome['outcome']} {detail}".rstrip())
    except Exception as error:  # noqa: BLE001 - a product review must never block a report.
        _say(db, f"Product review crashed: {type(error).__name__}")


def _narrator(db: Path, now: datetime, brief_id: str | None = None, pipeline: str = "single") -> Any:
    """The narrative stage as a callback on report data (6c/6d), pinned to `brief_id` when research
    succeeded; failures leave deterministic content."""
    from smith import credentials, narrative

    secrets = credentials.all_secrets()

    def narrate(data: dict[str, Any]) -> None:
        narrative.narrate(db, data["view"], data, now=now, secrets=secrets, brief_id=brief_id, pipeline=pipeline)
        status = data["narrative"]["status"]
        if status["outcome"] != "success":  # A report without its prose is worth a log line (BUG-2026-003).
            _say(db, f"Narrative ({data['kind']}) {status['outcome']}: {status.get('error_code') or ''}")
    return narrate


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
    kind = args.kind or editions.for_day(config, now.astimezone(ZoneInfo(tz)).date())
    if args.action == "run-due":
        if not config["reports"]["enabled"]:
            print("Scheduled reports are disabled (reports.enabled = false).")
            return 0
        with closing(ledger.connect(args.db)) as conn:
            for run_ in delivery.recover(conn, now):
                _say(args.db, f"Interrupted run {run_['report_id']} ({run_['slot'] or 'manual'}, was {run_['was']}) closed.")
            changed, marked = delivery.reconcile_schedule(conn, config, now)
            if changed:
                _say(args.db, "Schedule changed; it applies from now" + (
                    f" (passed slot {marked.astimezone(ZoneInfo(tz)):%m-%d %H:%M} will not be sent late)." if marked else "."))
            found = delivery.due(conn, config, now)
        if config.get("mail", {}).get("answer_replies"):
            # Replies to report e-mails: a few Gmail reads every run, a model call only for a real question.
            from smith import mail_command
            try:
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = mail_command.answer(args.db, args.config)
                for line in buffer.getvalue().splitlines():
                    _say(args.db, line)              # Answered questions and failures (e.g. an expired grant).
                if code != 0 and not buffer.getvalue():
                    _say(args.db, f"Answering questions failed (exit {code}).")
            except Exception as error:  # noqa: BLE001 - answering questions must never block a report.
                _say(args.db, f"Answering questions crashed: {type(error).__name__}")
        if found is None:
            # Same-day event notes (report design §29): once a day after the check time, apart from the reports.
            _check_events(args.db, config, now, reason="slot", sync=True)
            upcoming = delivery.slots_between(config["reports"], tz, now, now + PREPARE_AHEAD)
            if upcoming:
                # Prepare: research ahead of the scheduled time so the report itself is not delayed.
                upcoming_kind = editions.for_day(config, upcoming[0].date())
                prepared = _research(args.db, now, kind=upcoming_kind, regions=target_regions(config))
                if not prepared.get("reused") and prepared["outcome"] != "skipped":
                    _say(args.db, f"Prepared research for {upcoming[0]:%m-%d %H:%M} ({upcoming_kind}): "
                                  f"{prepared['outcome']}")
                return 0
            review_for = review_slot(config["reports"], tz, now)
            if review_for is not None:
                _review_products(args.db, config, now, editions.for_day(config, review_for.date()))
                return 0
            _say(args.db, "No report due.", log=False)  # Every 15 minutes: not worth a log line.
            return 0
        slot, missed = found
        kind = editions.for_day(config, slot.astimezone(ZoneInfo(tz)).date())
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
    # A successful snapshot is complete (any failed or malformed response aborts it), so a position it no
    # longer reports was sold and is closed. Without this one sale would block every later sync.
    if cli_main(["toss", "sync", "--close-missing", "--db", str(args.db)]) != 0:
        sync_failures.append("토스증권 보유 내역")
    if cli_main(["evidence", "sync", "--db", str(args.db)]) != 0:
        sync_failures.append("금리·공식 발표")
    if config.get("properties") or config.get("targets"):
        from smith import realestate_command
        if realestate_command.sync(args.db, args.config) != 0:
            sync_failures.append("아파트 실거래가")
    # Research before claiming the slot (it needs no lease), unless a fresh prepared brief exists.
    researched = _research(args.db, datetime.now(timezone.utc), kind=kind, regions=target_regions(config))
    _say(args.db, f"Research ({kind}) {researched['outcome']}{' (prepared)' if researched.get('reused') else ''}: "
                  f"{researched.get('items', researched.get('error_code'))}")
    brief_id = researched["brief_id"] if researched["outcome"] == "success" else None
    publish_at = datetime.now(timezone.utc)
    result = delivery.publish(args.db, recipient=recipient, credentials=stored, now=publish_at,
                              slot=slot, missed=missed, kind=kind, tz=tz, sync_failures=sync_failures,
                              narrator=_narrator(args.db, publish_at, brief_id), household=profile(config),
                              lineage=lineage(args.config), schedule=config["reports"])
    # The subject can carry an amount (net-worth change); the plain-text log gets only the run id and outcome.
    _say(args.db, f"Report {result['status']}: run {(result.get('report_id') or '-')[:8]} "
                  f"{result.get('error_code') or ''}".rstrip())
    if result.get("subject"):
        print(f"Subject: {result['subject']}")
    if result["status"] == "sent" and (config.get("backup") or {}).get("dir"):
        _backup_after_send(args.db, config["backup"])
    # The holdings and the evidence were just refreshed: look for events (the US session overnight) without syncing.
    if args.action == "run-due":
        _check_events(args.db, config, datetime.now(timezone.utc), reason="after-report", sync=False)
    return 0 if result["status"] in ("sent", "skipped") else 1


def _check_events(db: Path, config: dict[str, Any], now: datetime, *, reason: str, sync: bool) -> None:
    """The same-day event notes (smith.events, report design §29): at the daily check time (`reason` "slot", once a
    day) or right after a report ("after-report"). Research runs in the headless sandbox; a failure is logged and
    the event retried at a later check. Never blocks the reports."""
    from smith import credentials, events

    if not (config.get("events") or {}).get("enabled"):
        return
    try:
        if reason == "slot":
            with closing(ledger.connect(db)) as conn:
                if events.slot_due(conn, config, now) is None:
                    return
        outcomes = events.check(db, config, now=now, reason=reason, sync=sync, researcher=events.research_event,
                                recipient=config.get("mail", {}).get("recipient"), credentials=credentials.load_gmail(),
                                secrets=credentials.all_secrets(), say=lambda message: _say(db, message))
        for outcome in outcomes:
            # The detail can be the subject (an amount): the log gets the key and the status only.
            _say(db, f"Event {outcome['key']}: {outcome['status']}"
                     + (f" ({outcome['detail']})" if outcome["status"] != "sent" and outcome.get("detail") else ""))
        if not outcomes:
            _say(db, f"Event check ({reason}): nothing", log=reason != "slot")
    except Exception as error:  # noqa: BLE001 - an event check must never block or fail a report run.
        _say(db, f"Event check crashed: {type(error).__name__}")


def _backup_after_send(db: Path, settings: dict[str, Any]) -> None:
    """A verified ledger copy after each sent report; a failed copy is logged and never fails the run."""
    from smith import backup_command

    try:
        path, _, _ = backup_command.backup(db, Path(settings["dir"]), now=datetime.now(timezone.utc),
                                           keep=settings.get("keep", backup_command.DEFAULT_KEEP))
    except (backup_command.BackupError, OSError) as error:
        _say(db, f"Backup failed: {type(error).__name__}: {error}")
        return
    _say(db, f"Backup written: {path.name}")
