"""`smith daily`: preview the daily-routine mail (FR-31) without sending, or print what a model would receive."""
import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smith import daily, daily_html, korean
from smith.config import load_config, local_time

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")
PREVIEW_DIR = Path("reports")   # Ignored by Git, like the report previews.


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("daily", help="The daily-routine mail: preview (never sends) and model inputs")
    command.add_argument("action", choices=("preview", "inputs", "send", "status"),
                         help="preview: build the mail and write the HTML to reports/; inputs: the model payload as JSON; "
                              "send: send today's mail now (manual run, with the model); status: recent runs")
    command.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default: today in the household timezone)")
    command.add_argument("--model", action="store_true",
                         help="preview: also run the news research and the writer (headless model calls; audited in the ledger)")
    command.add_argument("--force", action="store_true",
                         help="send: send again although the day's mail already went out (a second run under its own key)")
    command.add_argument("--out", type=Path, help="preview: where to write the HTML (default reports/daily-preview-<date>.html)")
    command.add_argument("--config", type=Path, default=DEFAULT_LOCAL_CONFIG)
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"Missing setup: local config missing or invalid: {args.config}")
        return 2
    now = datetime.now(timezone.utc)
    tz = config["app"]["timezone"]
    day = args.as_of or local_time(now, tz).date()
    if args.action == "status":
        from contextlib import closing

        from smith import daily_send, ledger
        with closing(ledger.connect_read_only(args.db)) as conn:
            print("\n".join(daily_send.describe(conn)))
        return 0
    if args.action == "send":
        return _send(args.db, config, now=now, day=day, with_model=args.model, force=args.force)
    inputs = daily.collect(args.db, config, now=now, day=day)
    data = daily.assemble(inputs, day=day, tz=tz, now=now,
                          airways_sensitive=bool(config.get("daily", {}).get("airways_sensitive", False)))
    if args.action == "inputs":
        print(json.dumps(daily.payload(data), ensure_ascii=False, indent=2, default=str))
        return 0
    if args.model:
        _with_model(args.db, config, data, now=now, day=day)
    subject, html, text = daily_html.render(data, inline=True)
    out = args.out or PREVIEW_DIR / f"daily-preview-{day.isoformat()}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Subject: {subject}")
    print(f"HTML: {out} ({len(html.encode('utf-8')) // 1024} KB) — a preview; nothing was sent.")
    print(text)
    if data["failures"]:
        print(f"Sources not read: {data['failures']}")
        return 1
    return 0


PREPARE_AHEAD = timedelta(hours=1)


def run_due(db: Path, config: dict[str, Any], *, now: datetime) -> list[str]:
    """For `smith report run-due`: send today's mail when due, with the model steps. In the hour before the slot the
    news research runs ahead (it is reused for six hours), so the 06:00 mail only waits for the writer. Returns log lines."""
    from smith import credentials, daily_news, daily_send, headless

    lines: list[str] = []
    day = local_time(now, config["app"]["timezone"]).date()
    slot = daily_send.slot_for(config, day)
    lines += _sync_keep(db, now)
    lines += _sync_contacts(db, now)
    if slot - PREPARE_AHEAD <= now < slot and daily_send.is_mail_day(config, day):
        daily_config = config.get("daily", {})
        try:
            news = daily_news.research_and_store(db, now=now, today=day, topics=list(daily_config.get("news_topics", [])),
                                                 count=int(daily_config.get("news_count", 5)), secrets=credentials.all_secrets(),
                                                 executable=headless.find_claude())
            if news["outcome"] != "reused":
                lines.append(f"daily news prepared for {day.isoformat()}: {news['outcome']}, {len(news['items'])} item(s)")
        except headless.HeadlessError as error:
            lines.append(f"daily news not prepared ({error})")
    # The recipient comes from the same config the scheduler loaded (review 2026-10-25: never a second file).
    lines += daily_send.run_due(db, config, now=now, recipient=config.get("mail", {}).get("recipient"),
                                credentials=credentials.load_gmail(), modeler=_with_model)
    return lines


def _sync_keep(db: Path, now: datetime) -> list[str]:
    """Once a day: pull the newest Takeout archive of Keep from Drive (daily design §13). Never raises."""
    from smith import credentials, drive, gmail, keep

    folder = keep.directory(db)
    if not keep.sync_due(folder, now):
        return []
    stored = credentials.load_gmail()
    if stored is None:
        return []
    try:
        token, scopes = gmail.access_token(stored)
    except gmail.MailError as error:
        return [f"keep sync skipped ({error.code})"]
    if not drive.has_scope(scopes):
        keep._write_state(folder, {**keep.sync_state(folder), "last_checked": now.isoformat()})
        return ["keep sync skipped (no Drive scope: smith mail login --read --daily --drive)"]
    result = keep.sync(folder, token, now=now)
    if result["outcome"] in ("imported", "error"):
        return [f"keep sync: {result['outcome']}" + (f" {result['imported']} notes from {result['stamp']}" if result["outcome"] == "imported" else "")
                + (f" ({result['error_code']})" if result["error_code"] else "")]
    return []


def _sync_contacts(db: Path, now: datetime) -> list[str]:
    """Once a day: refresh the address book copy (daily design §14). Never raises; a failure keeps yesterday's copy."""
    from smith import contacts, credentials, gmail

    file = contacts.path(db)
    if not contacts.sync_due(file, now):
        return []
    stored = credentials.load_gmail()
    if stored is None:
        return []
    try:
        token, scopes = gmail.access_token(stored)
    except gmail.MailError as error:
        return [f"contacts sync skipped ({error.code})"]
    if not contacts.has_scope(scopes):
        return []   # Not opted in (smith mail login --contacts); nothing to say every hour.
    # `contacts.sync` turns network, response and write failures into a result; the mail that follows must go out
    # whatever happens here, so a local failure the sync did not foresee is caught as well (review 2026-10-25).
    try:
        result = contacts.sync(file, token, now=now)
    except (OSError, ValueError) as error:
        return [f"contacts sync: error ({type(error).__name__})"]
    if result["outcome"] == "busy":
        return ["contacts sync skipped (another sync is running)"]
    if result["outcome"] in ("synced", "partial"):
        return [f"contacts sync: {result['outcome']} {result['count']} contact(s)"
                + (f" ({result['error_code']})" if result["error_code"] else "")]
    return [f"contacts sync: error ({result['error_code']})"]


def _send(db: Path, config: dict[str, Any], *, now: datetime, day: date, with_model: bool, force: bool = False) -> int:
    from smith import credentials, daily_send

    recipient = config.get("mail", {}).get("recipient")
    result = daily_send.publish(db, config, now=now, day=day, trigger="manual", recipient=recipient,
                                credentials=credentials.load_gmail(), modeler=_with_model if with_model else None, force=force)
    if result["status"] == "skipped" and result.get("error_code") == "already-sent":
        print("Today's mail already went out; add --force to send another one.")
    print(f"{result['status']}: {result.get('subject') or ''}" + (f" ({result['error_code']})" if result.get("error_code") else ""))
    return 0 if result["status"] == "sent" else 1


def _with_model(db: Path, config: dict[str, Any], data: dict[str, Any], now: datetime, day: date) -> None:
    """News research and the writer; a failure of either leaves the code's own lines in place and is printed."""
    from smith import credentials, daily_news, daily_writer, headless

    daily_config = config.get("daily", {})
    secrets = credentials.all_secrets()
    try:
        executable = headless.find_claude()
    except headless.HeadlessError as error:
        print(f"Model step skipped ({error}).")
        data["failures"]["news"] = data["failures"]["writer"] = "no-claude"
        return
    news = daily_news.research_and_store(db, now=now, today=day, topics=list(daily_config.get("news_topics", [])),
                                         count=int(daily_config.get("news_count", 5)), secrets=secrets, executable=executable)
    data["news"] = news["items"]
    print(f"News: {news['outcome']}, {len(news['items'])} item(s)" + (f" ({news['error_code']})" if news["error_code"] else ""))
    if news["outcome"] == "failure":
        data["failures"]["news"] = str(news["error_code"])[:40]
    written = daily_writer.write_and_record(db, data, now=now, secrets=secrets, executable=executable)
    data["prose"] = written["prose"]
    print(f"Writer: {'ok' if written['prose'] is not None else 'failed'}"
          + (f" ({written['error_code']})" if written["error_code"] else "")
          + (f"; dropped: {'; '.join(written['problems'])}" if written["problems"] else "")
          + (f"; {korean.summary({'tidied': written['prose'].get('korean_fixes') or []})}" if written["prose"] else ""))
    if written["prose"] is None:
        data["failures"]["writer"] = str(written["error_code"])[:40]
