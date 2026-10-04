"""`smith mail`: connect the send-only Gmail grant and send a test message to the configured recipient."""
import argparse
import tomllib
from pathlib import Path
from typing import Any

from smith import credentials, gmail
from smith.config import load_config

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")


def add_parser(sub: Any) -> None:
    mail = sub.add_parser("mail", help="Gmail delivery (send-only permission)")
    mail.add_argument("action", choices=("login", "logout", "status", "test"))
    mail.add_argument("--client-file", type=Path, help="login: Google 'Desktop app' client JSON (keep it outside the repo)")
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
    return _test(args)


def _login(args: argparse.Namespace) -> int:
    if args.client_file is None:
        print("Pass --client-file with the downloaded 'Desktop app' client JSON.")
        return 2
    try:
        client_id, client_secret = gmail.load_client_file(args.client_file)
        print("Opening the browser for Google consent (send-only permission). Waiting up to 5 minutes...")
        refresh_token = gmail.login(client_id, client_secret)
    except gmail.MailError as error:
        print(f"Gmail login failed ({error.code}). Nothing stored.")
        return 1
    credentials.save_gmail(client_id, client_secret, refresh_token)
    print("Gmail send-only grant saved to the OS credential store. You may delete the client JSON file now.")
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
