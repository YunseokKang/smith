"""`smith health`: the health ledger (FR-39) — check the files, list what is on record, show one value's trend, the
follow-ups due, and import a batch. Nothing here sends mail or calls a model."""
import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smith import health, korean
from smith.config import load_config, local_time

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")
PREVIEW_DIR = Path("reports")   # Ignored by Git, like the report previews.
# The values `list` tabulates by year: the series a reader follows first (health design §3).
LIST_CODES = ("weight", "bmi", "waist", "body_fat_pct", "sbp", "dbp", "tc", "ldl", "hdl", "tg", "glucose_fasting", "hba1c",
              "ast", "alt", "ggt", "egfr", "uric_acid", "vitamin_d", "psa", "hb", "tsh", "iop_r", "iop_l")


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("health", help="The health ledger (data/health next to the finance ledger)")
    command.add_argument("action", choices=("check", "list", "series", "followups", "import", "resolve", "preview", "send", "status",
                                            "fitbit", "path"),
                         help="check: validate the files; list: values by year, findings, regimen; series CODE: one "
                              "value's trend; followups: next checks; import FILE: merge a JSON batch; resolve ID: mark a "
                              "finding resolved on the client's word (--on, --note); preview: build this week's letter and "
                              "write the HTML to reports/ (never sends); send: send this week's letter now (manual run); "
                              "status: recent runs; fitbit login|sync|status|week|logout: the Google Health (Fitbit) record; "
                              "path: the folder")
    command.add_argument("--model", action="store_true", help="preview/send: run the writer model (headless; audited in the ledger)")
    command.add_argument("--force", action="store_true", help="send: send again although this week's letter already went out")
    command.add_argument("target", nargs="?", help="series: a code such as ldl; import: the batch file; resolve: a finding id")
    command.add_argument("--on", type=date.fromisoformat, help="resolve: the day it was resolved (YYYY-MM-DD)")
    command.add_argument("--note", help="resolve: what settled it, as the client said (재측정 정상)")
    command.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default: today)")
    command.add_argument("--topic", help="preview: force a topic (vascular, uric-weight, nutrition-gut, balance, stomach-habits)")
    command.add_argument("--out", type=Path, help="preview: where to write the HTML (default reports/health-preview-<date>.html)")
    command.add_argument("--config", type=Path, default=DEFAULT_LOCAL_CONFIG)
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    folder = health.path(args.db)
    if args.action == "path":
        print(folder)
        return 0
    today = args.as_of or local_time(datetime.now(timezone.utc)).date()
    try:
        if args.action == "import":
            return _import(folder, args.target)
        if args.action == "preview":
            return _preview(args, folder, today)
        if args.action in ("send", "status"):
            return _send_or_status(args, today)
        if args.action == "fitbit":
            return _fitbit(args, folder, today)
        if args.action == "resolve":
            if not args.target or args.on is None or not args.note:
                print("resolve needs: smith health resolve FINDING_ID --on YYYY-MM-DD --note TEXT")
                return 2
            finding = health.resolve(folder, args.target, args.on, args.note)
            print(f"{finding.id} resolved {finding.resolved_on}: {finding.resolved_note}")
            return 0
        ledger = health.load(folder)
    except health.HealthError as error:
        print(f"health ledger problem: {error}")
        return 2
    if args.action == "check":
        print(f"{folder}: {len(ledger.observations)} observations, {len(ledger.findings)} findings, "
              f"{len(ledger.posture)} posture values, {len(ledger.genetics)} traits, "
              f"{'a' if ledger.microbiome else 'no'} microbiome report, {len(ledger.regimen)} regimen items, "
              f"{len(ledger.routine)} routines. OK.")
        for note in health.check(ledger):
            print(f"  note: {note}")
        return 0
    if args.action == "series":
        return _series(ledger, args.target)
    if args.action == "followups":
        _followups(ledger, today)
        return 0
    _list(ledger, today)
    return 0


def _preview(args: argparse.Namespace, folder: Path, today: date) -> int:
    """This week's letter as HTML in reports/: the day is the coming letter day (or --as-of), the ledger as it is now."""
    from smith import health_html, health_letter

    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"Missing setup: local config missing or invalid: {args.config}")
        return 2
    if args.topic and args.topic not in health_letter.TOPICS:
        print(f"unknown topic; choose from {', '.join(health_letter.TOPICS)}")
        return 2
    from smith import health_send

    from smith import fitbit

    ledger = health.load(folder)
    now = datetime.now(timezone.utc)
    data = health_letter.assemble(ledger, config, day=today, today=today, topic=args.topic,
                                  previous=health_send.previous_snapshot(args.db, today),
                                  fitbit=fitbit.weekly_summary(folder, today - timedelta(days=7), today - timedelta(days=1)))
    if args.model:
        _with_model(args.db, config, data, now, today)
    subject, html, text = health_html.render(data, inline=True)   # Charts as data URIs: the file opens on its own.
    out = args.out or PREVIEW_DIR / f"health-preview-{today.isoformat()}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Subject: {subject}")
    print(f"HTML: {out} ({len(html.encode('utf-8')) // 1024} KB) — a preview; nothing was sent.")
    print(text)
    return 0


def _send_or_status(args: argparse.Namespace, today: date) -> int:
    from contextlib import closing

    from smith import credentials, health_send, ledger

    if args.action == "status":
        with closing(ledger.connect_read_only(args.db)) as conn:
            print("\n".join(health_send.describe(conn)))
        return 0
    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"Missing setup: local config missing or invalid: {args.config}")
        return 2
    result = health_send.publish(args.db, config, now=datetime.now(timezone.utc), day=today, trigger="manual",
                                 recipient=config.get("mail", {}).get("recipient"), credentials=credentials.load_gmail(),
                                 modeler=_with_model if args.model else None, force=args.force, topic=args.topic)
    if result["status"] == "skipped" and result.get("error_code") == "already-sent":
        print("This week's letter already went out; add --force to send another one.")
    print(f"{result['status']}: {result.get('subject') or ''}" + (f" ({result['error_code']})" if result.get("error_code") else ""))
    return 0 if result["status"] == "sent" else 1


def run_due(db: Path, config: dict[str, Any], *, now: datetime) -> list[str]:
    """For `smith report run-due`: pull the Fitbit record once a day, then send this week's letter when due, with the
    writer. Returns log lines."""
    from smith import credentials, health_send

    lines = _sync_fitbit(db, now)
    return lines + health_send.run_due(db, config, now=now, recipient=config.get("mail", {}).get("recipient"),
                                       credentials=credentials.load_gmail(), modeler=_with_model)


def _sync_fitbit(db: Path, now: datetime) -> list[str]:
    """Once a day: the last two weeks of daily summaries from the Google Health API (health design §5). Never raises;
    a failure keeps the stored days and is reported on the letter's activity card."""
    from smith import credentials, fitbit, gmail

    folder = health.path(db)
    if not fitbit.sync_due(folder, now):
        return []
    stored = credentials.load_health()
    if stored is None:
        return []   # Not opted in (smith health fitbit login); nothing to say every hour.
    try:
        token, scopes = gmail.access_token(stored)
    except gmail.MailError as error:
        return [f"fitbit sync skipped ({error.code})"]
    if not fitbit.has_scope(scopes):
        return ["fitbit sync skipped (grant lacks the Google Health scopes: smith health fitbit login)"]
    try:
        result = fitbit.sync(folder, token, now=now)
    except (OSError, ValueError) as error:
        return [f"fitbit sync: error ({type(error).__name__})"]
    return [f"fitbit sync: {result['outcome']} {result.get('days', 0)} day(s)" + (f" ({result['error_code']})" if result.get("error_code") else "")]


def _fitbit(args: argparse.Namespace, folder: Path, today: date) -> int:
    """`smith health fitbit sync|status|week`: pull now, show the stored state, or print last week's summary."""
    from smith import credentials, fitbit, gmail

    what = args.target or "status"
    if what == "login":
        return _fitbit_login()
    if what == "logout":
        credentials.delete_health()
        print("Removed the Google Health grant from the OS credential store.")
        return 0
    if what == "sync":
        stored = credentials.load_health()
        if stored is None:
            print("No Google Health grant stored: run `smith health fitbit login` first (its own consent, read-only).")
            return 2
        try:
            token, scopes = gmail.access_token(stored)
        except gmail.MailError as error:
            print(f"Google token refresh failed ({error.code}).")
            return 1
        if not fitbit.has_scope(scopes):
            print("The grant has no Google Health scopes: re-consent with `smith health fitbit login`.")
            return 2
        result = fitbit.sync(folder, token, now=datetime.now(timezone.utc))
        print(f"fitbit sync: {result['outcome']}, {result.get('days', 0)} day(s) stored"
              + (f" ({result['error_code']})" if result.get("error_code") else ""))
        for line in result.get("notes", []):
            print(f"  {line}")
        return 0 if result["outcome"] == "synced" else 1
    if what == "week":
        summary = fitbit.weekly_summary(folder, today - timedelta(days=7), today - timedelta(days=1))   # The letter's window.
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str) if summary else "No Fitbit days stored for last week.")
        return 0
    for line in fitbit.describe(folder):
        print(line)
    return 0


def _fitbit_login() -> int:
    """Consent to the three Google Health read scopes alone, with the stored Google client (the Gmail grant's client
    id and secret); the refresh token is kept apart from the Gmail one."""
    from smith import credentials, fitbit, gmail

    stored = credentials.load_gmail()
    if stored is None:
        print("Run `smith mail login --client-file ...` first: the Google Health grant reuses that client.")
        return 2
    client_id, client_secret = stored[0], stored[1]
    print("Opening the browser for Google consent (Google Health read: activity, sleep, health metrics). Waiting up to 5 minutes...")
    print("Google shows an 'unverified app' screen: choose Advanced, then continue to the app. Only '보기' (read) rights should be listed.")
    try:
        refresh_token = gmail.login(client_id, client_secret, health_only=True)
    except gmail.MailError as error:
        print(f"Google Health login failed ({error.code}). Nothing stored.")
        return 1
    credentials.save_health(client_id, client_secret, refresh_token)
    print("Google Health grant saved to the OS credential store.")
    # Google asks for this check right after consent: a Google account without a linked Google Health (Fitbit) profile
    # answers every data call with ACCOUNT_NOT_LINKED (review 2026-10-25).
    try:
        token, _ = gmail.access_token((client_id, client_secret, refresh_token))
        linked, detail = fitbit.check_linked(token)
    except gmail.MailError as error:
        linked, detail = None, error.code
    if linked is False:
        print("The Google account has no Google Health profile linked yet: sign in to the Google Health (Fitbit) app with this "
              "Google account and migrate the Fitbit account, then run `smith health fitbit sync`.")
    elif linked is None:
        print(f"Could not confirm the Google Health profile link ({detail}); `smith health fitbit sync` will tell.")
    else:
        print("Google Health profile linked. Next: `smith health fitbit sync`, then `smith health fitbit status`.")
    return 0


def _with_model(db: Path, config: dict[str, Any], data: dict[str, Any], now: datetime, day: date) -> None:
    """The writer; a failure leaves the code's own lines (guideline actions) in place and is recorded in `failures`."""
    from smith import credentials, headless, health_writer

    try:
        executable = headless.find_claude()
    except headless.HeadlessError as error:
        print(f"Model step skipped ({error}).")
        data["failures"]["writer"] = "no-claude"
        return
    written = health_writer.write_and_record(db, data, now=now, secrets=credentials.all_secrets(), executable=executable)
    data["prose"] = written["prose"]
    print(f"Writer: {'ok' if written['prose'] is not None else 'failed'}"
          + (f" ({written['error_code']})" if written["error_code"] else "")
          + (f"; dropped: {'; '.join(written['problems'])}" if written["problems"] else "")
          + (f"; {korean.summary({'tidied': written['prose'].get('korean_fixes') or []})}" if written["prose"] else ""))
    if written["prose"] is None:
        data["failures"]["writer"] = str(written["error_code"])[:40]


def _import(folder: Path, target: str | None) -> int:
    if not target:
        print("import needs a batch file: smith health import FILE.json")
        return 2
    try:
        batch = json.loads(Path(target).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        print(f"cannot read {target}: {error}")
        return 2
    if not isinstance(batch, dict):
        print("the batch must be a JSON object with observations/findings/posture/genetics/microbiome/regimen/routine")
        return 2
    result = health.import_batch(folder, batch)
    for name in sorted(set(result.added) | set(result.skipped)):
        print(f"  {name}: {result.added.get(name, 0)} added, {result.skipped.get(name, 0)} already on file")
    return 0


def _series(ledger: health.Ledger, code: str | None) -> int:
    if not code or (code not in health.CODES and code not in health.POSTURE_CODES):
        print("series needs a known code, such as ldl, uric_acid, vitamin_d, forward_head_deg")
        return 2
    if code in health.POSTURE_CODES:
        rows = [p for p in ledger.posture if p.code == code]
        print(f"{health.label(code)} ({code}): {len(rows)} values")
        for row in rows:
            side = f" {row.side}" if row.side else ""
            print(f"  {row.on.isoformat()}  {row.text}{row.unit}{side}  [{row.device}]" + (f"  {row.note}" if row.note else ""))
        return 0
    rows = health.series(ledger, code)
    print(f"{health.label(code)} ({code}): {len(rows)} values")
    for row in rows:
        method = f" ({row.method})" if row.method else ""
        flag = "" if row.flag in ("normal", "unknown") else f"  {row.flag}"
        unsure = "  ?" if row.uncertain else ""
        print(f"  {row.on.isoformat()}  {row.text} {row.unit}{method}  ref {row.ref or '-'}{flag}{unsure}  <- {row.source}")
    for note in health.interpretation_notes(ledger):
        if code in note["codes"]:
            print(f"  note: {note['text']} ({note['source']})")
    return 0


def _followups(ledger: health.Ledger, today: date) -> None:
    items = health.follow_ups(ledger, today)
    print(f"{today.isoformat()} follow-ups: {len(items)}")
    for item in items:
        when = f"D-{item.days_left}" if item.days_left >= 0 else f"{-item.days_left}일 지남"
        print(f"  {item.due.isoformat()} {when:>10}  {item.label}: {item.finding.summary} ({item.finding.on.isoformat()}, "
              f"{item.finding.follow_up_months}개월 간격)")
    checkup = health.next_checkup(ledger)
    if checkup is not None:
        print(f"  {checkup.isoformat()}  건강검진(지난 검진 1년 뒤, 예상)")
    exported = health.as_dates(ledger, today)
    print(f"  -> {len(exported)} date(s) go to the daily date book: " + ", ".join(d.label for d in exported))


def _span(low: int, high: int) -> str:
    return str(low) if low == high else f"{low}~{high}"


def _list(ledger: health.Ledger, today: date) -> None:
    years = sorted({o.on.year for o in ledger.observations})
    if years:
        print("values by year (latest in the year; ? = uncertain reading):")
        print("  " + f"{'':24}" + "".join(f"{y:>9}" for y in years))
        for code in LIST_CODES:
            rows = health.series(ledger, code)
            if not rows:
                continue
            by_year: dict[int, health.Observation] = {}
            for row in rows:
                by_year[row.on.year] = row
            cells = "".join(f"{(by_year[y].text + ('?' if by_year[y].uncertain else '')) if y in by_year else '-':>9}" for y in years)
            print(f"  {health.label(code)[:12]:12}{code:12}{cells}")
    print(f"findings: {len(ledger.findings)}")
    for finding in ledger.findings:
        state = f" · 해소 {finding.resolved_on.isoformat()}" if finding.resolved_on else ""
        track = f" [{health.track_label(finding.track, finding)}]" if finding.track else ""
        print(f"  {finding.on.isoformat()} {finding.severity:10} {finding.area}{track}: {finding.summary}{state}")
    _followups(ledger, today)
    if ledger.posture:
        devices = sorted({p.device for p in ledger.posture})
        print(f"posture: {len(ledger.posture)} values from {', '.join(devices)}")
    if ledger.genetics:
        flagged = [t for t in ledger.genetics if t.grade and t.grade not in ("안심", "보통")]
        print(f"genetics ({ledger.genetics_on}): {len(ledger.genetics)} traits, types {', '.join(ledger.genetics_types) or '-'}; "
              f"flagged: {', '.join(t.trait for t in flagged) or '-'}")
    if ledger.microbiome:
        print(f"microbiome ({ledger.microbiome.on}, {ledger.microbiome.lab}): {len(ledger.microbiome.scores)} scores")
    if ledger.regimen:
        print("regimen: " + "; ".join(f"{r.name} ({r.frequency}{', since ' + r.since if r.since else ''})" for r in ledger.regimen))
    if ledger.routine:
        print("routine: " + "; ".join(f"{r.activity} {_span(r.per_week_min, r.per_week_max)}회/주 × "
                                     f"{_span(r.minutes_min, r.minutes_max)}분" for r in ledger.routine))
    for note in health.interpretation_notes(ledger):
        print(f"  note: {note['text']}")
    for note in health.check(ledger):
        print(f"  note: {note}")
