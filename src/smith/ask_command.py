"""`smith ask`: the counsel pipeline (FR-49) from the terminal. Prints the letter; never sends mail or stores a fact."""
import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith.config import DEFAULT_TIMEZONE, load_config

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")


def add_parser(sub: Any, default_db: Any) -> None:
    ask = sub.add_parser("ask", help="Ask Smith anything as the client (counsel, FR-49): prints the letter, sends nothing")
    ask.add_argument("question", help="The question, in the client's words")
    ask.add_argument("--db", type=Path, default=default_db)
    ask.add_argument("--config", type=Path, default=DEFAULT_LOCAL_CONFIG)
    ask.add_argument("--domains", help="Skip the router: comma-separated stores to read (finance,health,life); "
                                       "an empty string reads notes and memory only")
    ask.add_argument("--show-payload", action="store_true",
                     help="Route and assemble, then print what the answer model would receive; no answer call")
    ask.add_argument("--reader", action="store_true",
                     help="Also run the first reader (Codex) over the letter and rewrite what it marks; adds minutes")
    ask.add_argument("--json", action="store_true", help="Print the result as JSON")


def run(args: argparse.Namespace) -> int:
    from keyring.errors import KeyringError

    from smith import counsel, credentials, headless
    from smith.payload import PayloadRejected

    if os.environ.get(headless.HEADLESS_ENV):
        print("Refused: smith ask cannot run inside the adviser's own headless process.")
        return 2
    config = _config(args.config)
    if config is None:
        return 2
    try:
        secrets = credentials.all_secrets()
    except KeyringError:
        print("Blocked before sending: the credential store is unavailable, so stored secrets cannot be checked.")
        return 2
    now = datetime.now(timezone.utc)
    tz = config["app"]["timezone"]
    domains = None if args.domains is None else [d.strip() for d in args.domains.split(",") if d.strip()]
    try:
        if args.show_payload:
            caller = counsel._headless_caller(args.db, now, headless.find_claude(), subprocess.run)
            prepared = counsel.prepare(args.db, config, args.question, now=now, tz=tz, history=[], secrets=secrets,
                                       caller=caller, domains_override=domains)
            prepared["ctx"].pop("_health_meta", None)
            print(json.dumps({"plan": prepared["plan"], "context": prepared["ctx"], "searched": prepared["searched"],
                              "memos": prepared["memos"]}, ensure_ascii=False, indent=1, default=str))
            return 0
        first_reader = None
        if args.reader:   # On request only: the first reader adds minutes (docs/counsel-design.md §3.7).
            from smith import reader
            first_reader = reader.installed(brief=reader.COUNSEL_PROMPT, timeout_seconds=counsel.READER_TIMEOUT_SECONDS)[0]
        result = counsel.answer(args.db, config, args.question, now=now, received_at=now, history=[], secrets=secrets, tz=tz,
                                domains_override=domains, first_reader=first_reader)
    except PayloadRejected as rejected:
        print(f"Blocked before sending: payload matched {', '.join(rejected.rules)}. Nothing was sent.")
        return 2
    except headless.HeadlessError as error:
        print(f"Counsel failed ({error}). No letter produced.")
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
        return 0
    _, text = counsel.render(args.question, result)
    print(text)
    if args.reader:
        reading = result.get("reading") or {}
        print(f"\n(첫 독자: {reading.get('outcome')}, 지적 {reading.get('notes', 0)}건"
              + (f", 종류 {', '.join(reading['kinds'])}" if reading.get("kinds") else "") + f"; {reading.get('overall', '')})")
    if result["remember"] or result["noted"]:
        print("\n(저장하지 않았습니다. 기억에 넣으려면 `smith memory add`, 건강 진술은 메일 상담에서만 기록됩니다.)")
    return 0


def _config(path: Path) -> dict[str, Any] | None:
    """The local config, or the bootstrap defaults when there is none; a broken file stops the command."""
    if not path.exists():
        return {"app": {"timezone": DEFAULT_TIMEZONE}}
    try:
        return load_config(path)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        print(f"Local config is invalid ({error}): {path}.")
        return None
