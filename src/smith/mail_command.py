"""`smith mail`: connect the Gmail grant, send a test message, and answer the client's e-mail questions."""
import argparse
import tomllib
from pathlib import Path
from typing import Any

from smith import credentials, gmail
from smith.config import load_config

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")


def add_parser(sub: Any) -> None:
    mail = sub.add_parser("mail", help="Gmail delivery and answers to e-mail questions")
    mail.add_argument("action", choices=("login", "logout", "status", "test", "answer"),
                      help="answer: reply to the client's questions in report threads (needs login --read)")
    mail.add_argument("--client-file", type=Path, help="login: Google 'Desktop app' client JSON (keep it outside the repo)")
    mail.add_argument("--read", action="store_true",
                      help="login: also grant read access, used only to read replies in Smith's own report threads")
    mail.add_argument("--db", type=Path, default=Path("data/smith.db"))
    mail.add_argument("--config", type=Path, default=DEFAULT_LOCAL_CONFIG,
                      help="test: local config holding [mail] recipient (default: config/smith.local.toml)")


def run(args: argparse.Namespace) -> int:
    if args.action == "logout":
        credentials.delete_gmail()
        print("Removed the Gmail grant from the OS credential store.")
        return 0
    if args.action == "status":
        print("Gmail grant: stored" if credentials.load_gmail() else "Gmail grant: not stored (run smith mail login)")
        return 0
    if args.action == "login":
        return _login(args)
    if args.action == "answer":
        return answer(args.db, args.config, verbose=True)
    return _test(args)


def _login(args: argparse.Namespace) -> int:
    if args.client_file is None:
        print("Pass --client-file with the downloaded 'Desktop app' client JSON.")
        return 2
    try:
        client_id, client_secret = gmail.load_client_file(args.client_file)
        scope = "send and read permission" if args.read else "send-only permission"
        print(f"Opening the browser for Google consent ({scope}). Waiting up to 5 minutes...")
        if args.read:
            print("Google shows an 'unverified app' screen for read access: choose Advanced, then continue to the app.")
        refresh_token = gmail.login(client_id, client_secret, read=args.read)
    except gmail.MailError as error:
        print(f"Gmail login failed ({error.code}). Nothing stored.")
        return 1
    credentials.save_gmail(client_id, client_secret, refresh_token)
    print("Gmail grant saved to the OS credential store. You may delete the client JSON file now.")
    if args.read:
        print("To answer replies on schedule, set `answer_replies = true` under [mail] in the local config.")
    return 0


def answer(db: Path, config_path: Path, *, verbose: bool = False) -> int:
    """Answer new questions in Smith's report threads. Quiet when there is nothing to do."""
    from datetime import datetime, timezone

    from smith import qa
    from smith.config import local_time
    from smith.report_data import build_report

    from smith.report_command import profile

    try:
        config = load_config(config_path)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"Missing setup: local config missing or invalid: {config_path}")
        return 2
    to = config.get("mail", {}).get("recipient")
    stored = credentials.load_gmail()
    if to is None or stored is None:
        print("Missing setup: a [mail] recipient and a stored Gmail grant are required.")
        return 2
    household, tz = profile(config), config["app"]["timezone"]
    now = datetime.now(timezone.utc)

    def build() -> dict[str, Any]:
        from contextlib import closing

        from smith import ledger
        with closing(ledger.connect_read_only(db)) as conn:
            kind = "thursday" if local_time(now, tz).weekday() == 3 else "monday"
            return build_report(conn, as_of=now, known_at=now, baseline=None, kind=kind, tz=tz, household=household)
    try:
        results = qa.poll(db, recipient=to, credentials=stored, now=now, build=build, secrets=credentials.all_secrets())
    except gmail.MailError as error:
        if verbose or error.code != "no-read-scope":
            print(f"Answering questions failed ({error.code}).")
        return 1
    for item in results:
        print(f"Question {item['message_id'][:8]}...: {item['status']}")
    if verbose and not results:
        print("No new questions.")
    return 0


def recipient(config_path: Path) -> str | None:
    """The trusted recipient from local configuration; never from model output or reports."""
    try:
        return load_config(config_path).get("mail", {}).get("recipient")
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None


def _test(args: argparse.Namespace) -> int:
    to = recipient(args.config)
    stored = credentials.load_gmail()
    if to is None or stored is None:
        print("Missing setup: a [mail] recipient in the local config and a stored Gmail grant are both required.")
        return 2
    try:
        message_id = gmail.send(stored, recipient=to, subject="[Smith] 메일 연결 시험",
                                text="Smith 메일 연결 시험입니다. 이 메일이 보이면 정기 보고서를 받으실 수 있습니다.",
                                html="<p>Smith 메일 연결 시험입니다. 이 메일이 보이면 정기 보고서를 받으실 수 있습니다.</p>")
    except gmail.MailError as error:
        note = " The message may have been sent; check the inbox before retrying." if error.uncertain else ""
        print(f"Test send failed ({error.code}).{note}")
        return 1
    print(f"Test message sent (Gmail id {message_id[:8]}...).")
    return 0
