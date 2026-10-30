"""`smith fundamentals`: refresh the holdings' official quarterly figures and show what the markets report reads.

`sync` reads DART's periodic reports (KR) and SEC companyfacts (US), and the insiders' dealings; `show` prints each
holding's latest period, revenue, operating income, net income, change on a year earlier, operating margin and
liabilities / equity.
`smith report run-due` syncs on its own once the figures are a week old, when no report is due.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith import credentials, fundamentals, holdings, insiders, ledger
from smith.config import load_config, local_time
from smith.payload import load_view


def add_parser(sub: Any, default_db: Path) -> None:
    parser = sub.add_parser("fundamentals", help="Holdings' official quarterly figures: sync, show")
    parser.add_argument("action", choices=("sync", "show"))
    parser.add_argument("--db", type=Path, default=default_db)
    parser.add_argument("--config", type=Path, default=Path("config/smith.local.toml"))


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    now = datetime.now(timezone.utc)
    local = local_time(now, config["app"]["timezone"])
    with closing(ledger.connect_read_only(args.db)) as conn:
        view = load_view(conn, as_of=local, known_at=now)
    if args.action == "sync":   # The official quarters and the insiders' dealings together.
        failed = False
        for module, label in ((fundamentals, "Fundamentals"), (insiders, "Insiders")):
            result = module.sync(args.db, view, now=now, today=local.date(), dart_key=credentials.load_api_key("dart"),
                                 contact=config.get("mail", {}).get("recipient"))
            print(f"{label}: {result['stored']} new point(s)")
            for failure in result["failures"]:
                print(f"  {failure}")
            failed = failed or bool(result["failures"])
        return 1 if failed else 0
    with closing(ledger.connect_read_only(args.db)) as conn:
        found = fundamentals.load(conn, now)
    for p in holdings.review(view)["positions"]:
        if p["hermes"]:
            continue
        shown = fundamentals.summary(found.get(f"{p['market']}:{p['symbol']}"), local.date())
        if shown is None:
            print(f"{p['public_name']}: -")
            continue
        print(f"{p['public_name']}: {shown['period']} ({shown['source']}, {shown['filed']}) 매출 {shown['revenue']} "
              f"({_pct(shown['revenue_yoy'])}) 영업이익 {shown['operating_income']} 이익률 {_pct(shown['operating_margin'])} "
              f"부채비율 {_pct(shown['debt_ratio'])}")
    return 0


def _pct(ratio: str | None) -> str:
    return "-" if ratio is None else f"{float(ratio) * 100:+.1f}%"
