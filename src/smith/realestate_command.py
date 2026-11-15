"""`smith realestate`: keys for official real-estate data (data.go.kr MOLIT prices, R-ONE indices, V-World official
apartment prices for the holding tax, FR-53)."""
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

_LABELS = {"datagokr": "공공데이터포털(data.go.kr) 일반 인증키", "rone": "한국부동산원 R-ONE 인증키",
           "vworld": "V-World(국토교통부 공간정보) 인증키, 공동주택 공시가격용"}


def add_parser(sub: Any, default_db: Any) -> None:
    command = sub.add_parser("realestate", help="Official real-estate data: store API keys, check status")
    command.add_argument("action", choices=("login", "logout", "status", "sync", "show", "official"),
                         help="login: enter keys without echo; status: which keys exist; sync: fetch official "
                              "transactions for configured properties; show: price and jeonse summary; official: "
                              "read this year's official apartment prices (V-World) for the holding tax")
    command.add_argument("--year", type=int, help="official: the price year (default: this year)")
    command.add_argument("--only", choices=credentials.REAL_ESTATE_PROVIDERS,
                         help="login: ask for this one key only (e.g. --only vworld)")
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
    if args.action == "official":
        return official(args.db, args.config, year=args.year)
    if args.action == "logout":
        for provider in credentials.REAL_ESTATE_PROVIDERS:
            credentials.delete_api_key(provider)
        print("Removed real-estate API keys from the OS credential store.")
        return 0
    if not sys.stdin.isatty():
        print("Run `smith realestate login` in an interactive terminal; keys are never passed as arguments.")
        return 2
    for provider in (args.only,) if args.only else credentials.REAL_ESTATE_PROVIDERS:
        key = getpass.getpass(f"{_LABELS[provider]} (hidden, Enter to skip or keep): ").strip()
        if not key:
            continue
        # One key typed into the wrong prompt silently replaced the data.go.kr key on 2026-11-15 and stopped the
        # trade sync and the daily weather: a key already stored for another service is refused.
        clash = next((other for other in credentials.REAL_ESTATE_PROVIDERS
                      if other != provider and credentials.load_api_key(other) == key), None)
        if clash:
            print(f"Not saved: that key is already stored as {clash} ({_LABELS[clash]}). Check which prompt it belongs to.")
            continue
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
        targets = config.get("targets") or {}
    except (OSError, ValueError, UnicodeDecodeError):
        properties, targets, tz = {}, {}, "Asia/Seoul"
    key = credentials.load_api_key("datagokr")
    if not (properties or targets) or key is None:
        print("Missing setup: [properties] or [targets] in the local config and `smith realestate login` are required.")
        _record(db, now, "failure", "no-setup")
        return 2
    try:
        deals, fetched = realestate.sync(properties, key, today=local_time(now, tz).date(), months=months,
                                         targets=targets)
    except realestate.RealEstateError as error:
        print(f"Real-estate sync failed ({error.code}). Nothing stored.")
        _record(db, now, "failure", error.code)
        return 1
    # Deals of complexes no longer configured (a property sold, a target dropped) are removed with the sync.
    refs = set(properties) | {realestate.target_ref(key) for key in targets}
    with closing(ledger.connect(db)) as conn:
        stored = ledger.replace_deals(conn, deals, months=fetched, properties=refs,
                                      fetched_at=datetime.now(timezone.utc))
    # The lot numbers the official-price lookup asks by (FR-53), from the same records; kept beside the ledger.
    from smith import official_price
    try:
        official_price.remember_lots(Path(db).parent, deals)
    except OSError:
        print("Warning: could not keep the lot numbers for the official-price lookup.")
    _record(db, now, "success")
    print(f"Real-estate sync: {len(deals)} matching deal(s), {stored} row(s) stored")
    return 0


def official(db: Path, config_path: Path, *, year: int | None = None) -> int:
    """Read the year's official apartment price of every configured home and move target with a known lot number
    (FR-53). Needs the V-World key (`smith realestate login`) and one `sync` before it (the lot numbers)."""
    from smith import official_price, realestate
    from smith.config import load_config

    now = datetime.now(timezone.utc)
    try:
        config = load_config(config_path)
    except (OSError, ValueError, UnicodeDecodeError):
        print("Missing setup: the local config could not be read.")
        return 2
    key = credentials.load_api_key("vworld")
    specs = {**(config.get("properties") or {}),
             **{realestate.target_ref(k): v for k, v in (config.get("targets") or {}).items()}}
    if key is None or not specs:
        print("Missing setup: [properties] in the local config and a V-World key (`smith realestate login`).")
        return 2
    year = year or local_time(now, config["app"]["timezone"]).year
    outcomes = official_price.sync(Path(db).parent, specs, key, year=year, now=now)
    for ref, outcome in sorted(outcomes.items()):
        print(f"{ref}: {outcome}")
    return 0 if outcomes and all(o == "ok" for o in outcomes.values()) else 1


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
