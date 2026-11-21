"""`smith letter-review`: the weekly review of Smith's own letters (FR-63): preview it, send it now, or show its runs."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith.config import load_config, local_time

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")
PREVIEW_DIR = Path("reports")   # Ignored by Git, like the other previews.


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("letter-review", help="The weekly review of Smith's own finance letters (read from outside)")
    command.add_argument("action", choices=("preview", "send", "status"),
                         help="preview: build the review of the last seven days and write the HTML to reports/ (never sends; "
                              "it does call the models unless --no-model); send: send it now (manual run); status: recent runs")
    command.add_argument("--no-model", action="store_true", help="preview: the measures alone, without research or reading")
    command.add_argument("--force", action="store_true", help="send: send again although this week's review went out")
    command.add_argument("--config", type=Path, default=DEFAULT_LOCAL_CONFIG)
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    from smith import credentials, gmail, ledger, letter_review

    if args.action == "status":
        with closing(ledger.connect_read_only(args.db)) as conn:
            print("\n".join(letter_review.describe(conn)))
        return 0
    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"Missing setup: local config missing or invalid: {args.config}")
        return 2
    now = datetime.now(timezone.utc)
    today = local_time(now, config["app"]["timezone"]).date()
    day = today   # A manual run reads the seven days that end at today's slot time; on the review day that is its own run.
    if args.action == "preview":
        try:
            fetch, researcher, consultant, context_reader = letter_review.live(config, models=not args.no_model)
        except gmail.MailError as error:
            print(f"Missing setup ({error.code}): the Gmail grant with the read scope is required (`smith mail login --read`).")
            return 2
        data = letter_review.build(args.db, config, now=now, day=day, fetch=fetch, researcher=researcher, consultant=consultant,
                                   context_reader=context_reader)
        if not data["letters"]:
            print("No letter of the week could be read: " + (", ".join(data["unread"]) or "none was sent."))
            return 1
        subject, html, _ = letter_review.render(data)
        out = PREVIEW_DIR / f"letter-review-preview-{day.isoformat()}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"Subject: {subject}")
        for layer, code in data["failed"].items():
            print(f"Layer failed: {layer} ({code})")
        print(f"HTML: {out} (a preview; nothing was sent)")
        return 0
    result = letter_review.publish(args.db, config, now=now, day=day, trigger="manual",
                                   recipient=config.get("mail", {}).get("recipient"), credentials_=credentials.load_gmail(),
                                   force=args.force)
    if result["status"] == "skipped" and str(result.get("error_code")).startswith("already-"):
        print("This week's review already went out; add --force to send another one.")
        return 0
    print(letter_review.as_line(result))
    return 0 if result["status"] in ("sent", "skipped") else 1
