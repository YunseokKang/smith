"""`smith memory`: add what the client said, list what Smith remembers, preview what a call would send, and the monthly
memory check (FR-52): preview it, send it now, or show its runs."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith import memory
from smith.config import load_config, local_time

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")
PREVIEW_DIR = Path("reports")   # Ignored by Git, like the other previews.


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("memory", help="What the client told Smith (Markdown files next to the ledger)")
    command.add_argument("action", choices=("add", "list", "search", "path", "review", "review-send", "review-status"),
                         help="add TOPIC TEXT: remember a fact; list: show all; search TEXT: the facts a call "
                              "about TEXT would receive; path: the memory folder; review: build this month's memory "
                              "check and write the HTML to reports/ (never sends); review-send: send it now (manual run); "
                              "review-status: recent runs")
    command.add_argument("words", nargs="*", help=f"add: topic ({', '.join(memory.TOPICS)}) then the fact; "
                                                   "search: the text of a question or topic")
    command.add_argument("--source", default="대화", help="add: where the fact came from (default: 대화)")
    command.add_argument("--force", action="store_true", help="review-send: send again although this month's check went out")
    command.add_argument("--config", type=Path, default=DEFAULT_LOCAL_CONFIG)
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    folder = memory.directory(args.db)
    if args.action == "path":
        print(folder)
        return 0
    if args.action.startswith("review"):
        return _review(args)
    if args.action == "add":
        if len(args.words) < 2 or args.words[0] not in memory.TOPICS:
            print(f"Give a topic ({', '.join(memory.TOPICS)}) and the fact, for example: "
                  "smith memory add property \"솔방울은 거주 중인 집을 말한다\"")
            return 2
        fact = memory.add(folder, args.words[0], " ".join(args.words[1:]),
                          on=local_time(datetime.now(timezone.utc)).date(), source=args.source)
        print("Already remembered or empty; nothing added." if fact is None
              else f"Remembered under {memory.TOPICS[fact.topic]}: {fact.text}")
        return 0
    facts = memory.load(folder, private=True)   # The client's own listing shows the counsel topic too.
    if args.action == "search":
        facts = memory.retrieve(facts, " ".join(args.words))
    if not facts:
        print("Nothing remembered yet." if args.action == "list" else "No fact would be sent.")
    for fact in facts:
        when = fact.recorded_on.isoformat() if fact.recorded_on else "날짜 없음"
        print(f"[{memory.TOPICS[fact.topic]}] {when} · {fact.source} · {fact.text}")
    return 0


def _review(args: argparse.Namespace) -> int:
    from smith import credentials, ledger, memory_review

    if args.action == "review-status":
        with closing(ledger.connect_read_only(args.db)) as conn:
            print("\n".join(memory_review.describe(conn)))
        return 0
    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"Missing setup: local config missing or invalid: {args.config}")
        return 2
    now = datetime.now(timezone.utc)
    today = local_time(now, config["app"]["timezone"]).date()
    if args.action == "review":
        data = memory_review.build(args.db, config, now=now, day=today)
        subject, html, text = memory_review.render(data)
        out = PREVIEW_DIR / f"memory-review-preview-{today.isoformat()}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"Subject: {subject}")
        print(f"HTML: {out} — a preview; nothing was sent.")
        return 0
    result = memory_review.publish(args.db, config, now=now, day=memory_review.review_day(config, today), trigger="manual",
                                   recipient=config.get("mail", {}).get("recipient"), credentials=credentials.load_gmail(),
                                   force=args.force)
    if result["status"] == "skipped" and str(result.get("error_code")).startswith("already-"):
        print("This month's check already went out; add --force to send another one.")
        return 0
    print(memory_review.as_line(result))
    return 0 if result["status"] in ("sent", "skipped") else 1
