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
    mail.add_argument("--daily", action="store_true",
                      help="login: also grant read-only access to the primary calendar and tasks (daily mail)")
    mail.add_argument("--drive", action="store_true",
                      help="login: also grant read-only Drive access, used only for the scheduled Takeout archives of Keep")
    mail.add_argument("--contacts", action="store_true",
                      help="login: also grant read-only Contacts access (names, birthdays, 중요한 날, 메모; no phone, e-mail or address)")
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
    stored = credentials.load_gmail()
    if args.client_file is None and stored is None:
        print("Pass --client-file with the downloaded 'Desktop app' client JSON.")
        return 2
    try:
        if args.client_file is not None:
            client_id, client_secret = gmail.load_client_file(args.client_file)
        else:  # Re-consenting with more scopes: the stored client is reused, so the JSON file is not needed again.
            client_id, client_secret = stored[0], stored[1]   # type: ignore[index]
            print("Reusing the stored Google client; the grant is replaced by the one you consent to now.")
        granted = (["send"] + (["Gmail read"] if args.read else []) + (["calendar read", "tasks read"] if args.daily else [])
                   + (["Drive read (Takeout archives)"] if args.drive else []) + (["Contacts read"] if args.contacts else []))
        print(f"Opening the browser for Google consent ({', '.join(granted)}). Waiting up to 5 minutes...")
        if args.read or args.daily or args.drive or args.contacts:
            print("Google shows an 'unverified app' screen for these scopes: choose Advanced, then continue to the app.")
        refresh_token = gmail.login(client_id, client_secret, read=args.read, daily=args.daily, drive=args.drive,
                                    contacts=args.contacts)
    except gmail.MailError as error:
        print(f"Gmail login failed ({error.code}). Nothing stored.")
        return 1
    credentials.save_gmail(client_id, client_secret, refresh_token)
    print("Gmail grant saved to the OS credential store. You may delete the client JSON file now.")
    if args.read:
        print("To answer replies on schedule, set `answer_replies = true` under [mail] in the local config.")
    if args.daily:
        print("Check the calendar and tasks reads with `smith agenda check`, then `smith agenda events` and `smith agenda tasks`.")
    if args.drive:
        print("Check the Keep archive sync with `smith keep sync`.")
    if args.contacts:
        print("Check the address book sync with `smith contacts sync`, then `smith contacts list`.")
    return 0


def answer(db: Path, config_path: Path, *, verbose: bool = False) -> int:
    """Answer new questions in Smith's report threads. Quiet when there is nothing to do."""
    from datetime import datetime, timezone

    from smith import qa
    from smith.report_command import lineage, profile

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
    current = lineage(config_path)

    def build(report: dict[str, Any] | None) -> dict[str, Any]:
        return qa.rebuild(db, report, now=now, tz=tz, household=household, lineage=current)
    secrets = credentials.all_secrets()
    # A reply to any daily mail Smith sent gets the daily answerer, whether or not the schedule is on; it reads
    # nothing until such a question arrives.
    from smith import daily_qa, health_qa
    daily_answerer = daily_qa.answerer(db, config, now=now, tz=tz, secrets=secrets)
    health_answerer = health_qa.answerer(db, config, now=now, tz=tz, secrets=secrets)
    try:
        results = qa.poll(db, recipient=to, credentials=stored, now=now, build=build, secrets=secrets, tz=tz,
                          daily=daily_answerer, health=health_answerer, dart_key=credentials.load_api_key("dart"))
    except gmail.MailError as error:
        if verbose or error.code != "no-read-scope":
            print(f"Answering questions failed ({error.code}).")
        return 1
    for item in results:
        kept = (", looked up" if item.get("looked_up") else "") + (
            f", remembered {item['remembered']}" if item.get("remembered") else "")
        failed = f", memory not saved ({item['memory_error']})" if item.get("memory_error") else ""
        print(f"Question {item['message_id'][:8]}...: {item['status']}{kept}{failed}")
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
