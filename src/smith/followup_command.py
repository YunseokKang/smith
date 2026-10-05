"""`smith research` (web briefs without household data) and `smith proposal` (follow-up decisions)."""
import argparse
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from smith import ledger
from smith.config import local_time


def add_parser(sub: Any, default_db: Any) -> None:
    research = sub.add_parser("research", help="Research policy and market topics on the web (no household data)")
    research.add_argument("action", choices=("run", "show", "topics"),
                          help="run: research now and store the brief; show: print the latest brief; "
                               "topics: print the generalized topics that would be researched")
    research.add_argument("--db", type=type(default_db), default=default_db)
    proposal = sub.add_parser("proposal", help="List proposals and record decisions for follow-up")
    proposal.add_argument("action", choices=("list", "accept", "decline", "done"))
    proposal.add_argument("key", nargs="?", help="Proposal key, for example lease-return")
    proposal.add_argument("--note", help="Optional short note (stored in the ledger)")
    proposal.add_argument("--db", type=type(default_db), default=default_db)


def run_research(args: argparse.Namespace) -> int:
    from smith import credentials, research
    from smith.payload import load_view

    if not args.db.exists():
        print(f"No ledger at {args.db}.")
        return 1
    now = datetime.now(timezone.utc)
    if args.action == "show":
        with closing(ledger.connect_read_only(args.db)) as conn:
            brief = ledger.latest_research(conn, since=now - timedelta(days=3650))
        if brief is None:
            print("No successful research brief yet. Run `smith research run`.")
            return 1
        print(f"Brief {brief['brief_id']} from {local_time(brief['created_at']):%Y-%m-%d %H:%M} ({brief['model']})")
        titles = {t["ref"]: t["title"] for t in brief["topics"]}
        for item in brief["brief"]["items"]:
            print(f"{item['ref']:>4} [{titles.get(item['topic'], item['topic'])}] ({item.get('tier', '?')}) {item['headline']}")
            print(f"      {item['publisher']} {item['published_on']} {item['source_url']}")
        for gap in brief["brief"]["gaps"]:
            print(f"  gap: {gap}")
        return 0
    with closing(ledger.connect_read_only(args.db)) as conn:
        view = load_view(conn, as_of=local_time(now), known_at=now)
    if args.action == "topics":
        for topic in research.topics(view):
            print(f"{topic.ref} {topic.title}: {topic.question}")
        return 0
    result = research.research_and_store(args.db, view, now=now, today=local_time(now).date(),
                                         secrets=credentials.all_secrets())
    if result["outcome"] != "success":
        print(f"Research failed ({result['error_code']}). The failure is recorded; reports continue without it.")
        return 1
    print(f"Research stored: {result['items']} item(s), {result['dropped']} dropped by validation, "
          f"cost {result['cost_usd'] or 'unknown'}")
    return 0


def run_proposal(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"No ledger at {args.db}.")
        return 1
    if args.action == "list":
        with closing(ledger.connect_read_only(args.db)) as conn:
            active = ledger.active_proposals(conn)
            events = ledger.proposal_events(conn, limit=20)
        if not active:
            print("No proposal has been delivered yet.")
        for item in sorted(active.values(), key=lambda i: i["last_shown_at"] or "", reverse=True):
            print(f"{item['key']:<18} {item['identity']:<32} {item['status']:<9} shown {item['times_shown']}x, "
                  f"since {item['created_at'][:10]}")
        if events:
            print("Recent events:")
            for event in events:
                print(f"  {event['at'][:16]} {event['key']:<18} {event['event']:<10}"
                      + (f" note: {event['note']}" if event["note"] else ""))
        return 0
    if not args.key:
        print("Give the proposal key, for example: smith proposal accept lease-return")
        return 2
    status = {"accept": "accepted", "decline": "declined", "done": "done"}[args.action]
    try:
        with closing(ledger.connect(args.db)) as conn:
            found = ledger.decide_proposal(conn, key=args.key, status=status, decided_at=datetime.now(timezone.utc),
                                           note=(args.note or "")[:200] or None)
    except (sqlite3.Error, ledger.LedgerError) as error:
        print(f"Ledger error ({type(error).__name__}).")
        return 1
    if not found:
        print(f"No delivered proposal with key {args.key!r}. See `smith proposal list`.")
        return 1
    print(f"Recorded: {args.key} -> {status}. Reports will follow up while its figures stay the same.")
    return 0
