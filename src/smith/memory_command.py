"""`smith memory`: add what the client said, list what Smith remembers, and preview what a call would send."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith import memory
from smith.config import local_time


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("memory", help="What the client told Smith (Markdown files next to the ledger)")
    command.add_argument("action", choices=("add", "list", "search", "path"),
                         help="add TOPIC TEXT: remember a fact; list: show all; search TEXT: the facts a call "
                              "about TEXT would receive; path: the memory folder")
    command.add_argument("words", nargs="*", help=f"add: topic ({', '.join(memory.TOPICS)}) then the fact; "
                                                   "search: the text of a question or topic")
    command.add_argument("--source", default="대화", help="add: where the fact came from (default: 대화)")
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    folder = memory.directory(args.db)
    if args.action == "path":
        print(folder)
        return 0
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
    facts = memory.load(folder)
    if args.action == "search":
        facts = memory.retrieve(facts, " ".join(args.words))
    if not facts:
        print("Nothing remembered yet." if args.action == "list" else "No fact would be sent.")
    for fact in facts:
        when = fact.recorded_on.isoformat() if fact.recorded_on else "날짜 없음"
        print(f"[{memory.TOPICS[fact.topic]}] {when} · {fact.source} · {fact.text}")
    return 0
