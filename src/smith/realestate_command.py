"""`smith realestate`: keys for official real-estate data (data.go.kr MOLIT prices, R-ONE indices)."""
import argparse
import getpass
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smith import credentials, ledger
from smith.config import local_time

_LABELS = {"datagokr": "공공데이터포털(data.go.kr) 일반 인증키", "rone": "한국부동산원 R-ONE 인증키"}


def add_parser(sub: Any, default_db: Any) -> None:
    command = sub.add_parser("realestate", help="Official real-estate data: store API keys, check status")
    command.add_argument("action", choices=("login", "logout", "status", "sync", "show"),
                         help="login: enter keys without echo; status: which keys exist; sync: fetch official "
                              "transactions for configured properties; show: price and jeonse summary")
    command.add_argument("--months", type=int, default=13, help="sync: months of history (default: 13)")
    command.add_argument("--config", type=Path, default=Path("config/smith.local.toml"))
    command.add_argument("--db", type=type(default_db), default=default_db)


def run(args: argparse.Namespace) -> int:
    if args.action == "status":
        for provider in credentials.REAL_ESTATE_PROVIDERS:
            state = "stored" if credentials.load_api_key(provider) else "missing"
            print(f"{provider}: {state} ({_LABELS[provider]})")
        return 0
    if args.action == "sync":
        return sync(args.db, args.config, months=args.months)
    if args.action == "show":
        return _show(args)
    if args.action == "logout":
        for provider in credentials.REAL_ESTATE_PROVIDERS:
            credentials.delete_api_key(provider)
        print("Removed real-estate API keys from the OS credential store.")
        return 0
    if not sys.stdin.isatty():
        print("Run `smith realestate login` in an interactive terminal; keys are never passed as arguments.")
        return 2
    for provider in credentials.REAL_ESTATE_PROVIDERS:
        key = getpass.getpass(f"{_LABELS[provider]} (hidden, Enter to skip or keep): ").strip()
        if key:
            credentials.save_api_key(provider, key)
    print("Saved to the OS credential store. Check with: smith realestate status")
    return 0


def sync(db: Path, config_path: Path, *, months: int = 13) -> int:
    """Fetch official transactions for the configured properties and store the matching ones. The outcome
    is recorded in sync_runs (source "molit") so reports can warn about stale market data."""
    from smith import realestate
    from smith.config import load_config

    now = datetime.now(timezone.utc)
    try:
        config = load_config(config_path)
        properties, tz = config.get("properties") or {}, config["app"]["timezone"]
    except (OSError, ValueError, UnicodeDecodeError):
        properties, tz = {}, "Asia/Seoul"
    key = credentials.load_api_key("datagokr")
    if not properties or key is None:
        print("Missing setup: [properties] in the local config and `smith realestate login` are required.")
        _record(db, now, "failure", "no-setup")
        return 2
    try:
        deals, fetched = realestate.sync(properties, key, today=local_time(now, tz).date(), months=months)
    except realestate.RealEstateError as error:
        print(f"Real-estate sync failed ({error.code}). Nothing stored.")
        _record(db, now, "failure", error.code)
        return 1
    with closing(ledger.connect(db)) as conn:
        stored = ledger.replace_deals(conn, deals, months=fetched, properties=set(properties),
                                      fetched_at=datetime.now(timezone.utc))
    _record(db, now, "success")
    print(f"Real-estate sync: {len(deals)} matching deal(s), {stored} row(s) stored")
    return 0


def _record(db: Path, at: datetime, outcome: str, code: str | None = None) -> None:
    try:
        with closing(ledger.connect(db)) as conn:
            ledger.record_sync_run(conn, source="molit", attempted_at=at, outcome=outcome, error_code=code)
    except (OSError, sqlite3.Error, ledger.LedgerError):
        print("Warning: could not record the sync outcome.")


def _show(args: argparse.Namespace) -> int:
    from smith import realestate
    from smith.fmt import short_won

    now = datetime.now(timezone.utc)
    today = local_time(now).date()
    with closing(ledger.connect_read_only(args.db)) as conn:
        deals = ledger.load_deals(conn, since=today - timedelta(days=400))
    for ref, a in realestate.analyze(deals, today=today).items():
        print(f"{ref}: trades {a['trades_12m']} in 12 months, estimate {short_won(a['estimate'])} "
              f"(median of {a['estimate_basis']} in 6 months), jeonse {short_won(a['jeonse'])} "
              f"({a['jeonse_basis']} contracts)")
        for month, price, count in a["monthly"]:
            print(f"    {month}: {short_won(price)} ({count})")
    return 0
