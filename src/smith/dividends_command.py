"""`smith dividends`: refresh the holdings' dividend evidence (FR-45) and show each owner's yearly run rate.

`sync` reads the KR decisions from OpenDART and asks one web-tool call for the US 12-month figures (a model call);
`show` prints the run rate the tax note uses, per owner, from what the ledger knows now. `smith report run-due`
syncs on its own once the evidence is a week old, when no report is due.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith import credentials, dividends, ledger
from smith.config import load_config, local_time
from smith.payload import load_view


def add_parser(sub: Any, default_db: Path) -> None:
    parser = sub.add_parser("dividends", help="Holdings' dividends: sync the evidence, show the yearly run rate")
    parser.add_argument("action", choices=("sync", "show"))
    parser.add_argument("--db", type=Path, default=default_db)
    parser.add_argument("--config", type=Path, default=Path("config/smith.local.toml"))


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    now = datetime.now(timezone.utc)
    local = local_time(now, config["app"]["timezone"])
    with closing(ledger.connect_read_only(args.db)) as conn:
        view = load_view(conn, as_of=local, known_at=now)
    if args.action == "sync":
        result = sync_now(args.db, view, now=now, today=local.date())
        print(f"Dividend evidence: {result['stored']} new point(s)")
        for failure in result["failures"]:
            print(f"  {failure}")
        return 0 if not result["failures"] else 1
    with closing(ledger.connect_read_only(args.db)) as conn:
        found = dividends.yearly(view, dividends.load(conn, now), local.date())
    for owner, mine in sorted(found.items(), key=lambda item: (item[0] != "self", item[0])):
        print(f"{owner}: {mine['amount']:,} won a year in general accounts")
        for name, won, basis in mine["parts"]:
            print(f"  {name}: {won:,} ({'전자공시' if basis == 'filing' else '웹 조사 추정'})")
        if mine["unknown"]:
            print(f"  unknown: {', '.join(mine['unknown'])}")
        if mine["excluded"]:
            print(f"  ISA·pension (taxed apart): {', '.join(mine['excluded'])}")
    return 0


def sync_now(db: Path, view: Any, *, now: datetime, today: Any) -> dict[str, Any]:
    """`dividends.sync` with the stored DART key and secrets."""
    return dividends.sync(db, view, now=now, today=today, dart_key=credentials.load_api_key("dart"),
                          secrets=credentials.all_secrets())
