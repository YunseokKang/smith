"""`smith law`: the tax-law watch (FR-54) by hand: store the 법제처 OC key, check now, show the last result."""
import argparse
import getpass
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith import credentials, law_watch


def add_parser(sub: Any, default_db: Any) -> None:
    command = sub.add_parser("law", help="Tax-law watch (법제처 Open API): key, check, last result")
    command.add_argument("action", choices=("login", "logout", "status", "check", "show", "article"),
                         help="login: enter the OC key without echo; check: read the watched laws now; show: the "
                              "last result; article: print one article in force (development aid)")
    command.add_argument("law", nargs="?", help="article: the law name, e.g. '지방세법 시행령'")
    command.add_argument("number", nargs="?", help="article: the article number, e.g. 110의2")
    command.add_argument("--db", type=type(default_db), default=default_db)


def run(args: argparse.Namespace) -> int:
    folder = Path(args.db).parent
    if args.action == "status":
        print(f"law: {'stored' if credentials.load_api_key('law') else 'missing'} (법제처 국가법령정보 공동활용 OC)")
        return 0
    if args.action == "logout":
        credentials.delete_api_key("law")
        print("Removed the law API key from the OS credential store.")
        return 0
    if args.action == "login":
        if not sys.stdin.isatty():
            print("Run `smith law login` in an interactive terminal; keys are never passed as arguments.")
            return 2
        key = getpass.getpass("법제처 국가법령정보 공동활용 OC (hidden, Enter to keep): ").strip()
        if key:
            credentials.save_api_key("law", key)
        print("Saved to the OS credential store. Check with: smith law status")
        return 0
    if args.action == "show":
        return _print(law_watch.load(folder))
    oc = credentials.load_api_key("law")
    if oc is None:
        print("Missing setup: `smith law login` first.")
        return 2
    if args.action == "article":
        if not (args.law and args.number):
            print("Usage: smith law article '<law name>' <number, e.g. 110의2>")
            return 2
        try:
            for line in law_watch.article(args.law, args.number, oc):
                print(line)
        except law_watch.LawWatchError as error:
            print(f"Article lookup failed ({error.code}).")
            return 1
        return 0
    try:
        result = law_watch.check(oc, now=datetime.now(timezone.utc), previous=law_watch.load(folder))
    except law_watch.LawWatchError as error:
        print(f"Law check failed ({error.code}). Nothing stored.")
        return 1
    law_watch.save(folder, result)
    return _print(result)


def _print(result: dict[str, Any] | None) -> int:
    if result is None:
        print("No law check stored yet: run `smith law check`.")
        return 1
    print(f"Checked at {result['checked_at']}")
    for name, entry in result["laws"].items():
        if entry["error"]:
            print(f"  {name}: failed ({entry['error']})")
            continue
        newest = entry["versions"][0]
        print(f"  {name}: {len(entry['versions'])} version(s); newest promulgated {newest['promulgated']}, "
              f"effective {newest['effective']} ({newest['kind']}, {newest['status']})")
    for amendment in law_watch.pending(result):
        print(f"Not yet reflected: {amendment['name']} {amendment['kind']} promulgated {amendment['promulgated']}, "
              f"effective {amendment['effective']}")
    if any(entry["error"] == "auth-failed" for entry in result["laws"].values()):
        print("The 법제처 API refused the key: register this PC's public IP address (and a domain) on the OPEN API "
              "application at open.law.go.kr, then run `smith law check` again.")
    return 0 if not any(entry["error"] for entry in result["laws"].values()) else 1
