"""`smith picks`: run or show the product review team's work (smith.picks, report design §26)."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smith import ledger
from smith.config import local_time


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("picks", help="Product review: scout, verify, critique and select products for a proposal")
    command.add_argument("action", choices=("run", "show"),
                         help="run: review now and store the result (never sends anything); show: print the latest review")
    command.add_argument("--class", dest="product_class", default="core-etf",
                         help="Product class (default: core-etf)")
    command.add_argument("--force", action="store_true", help="run: review even if a fresh review exists")
    command.add_argument("--db", type=Path, default=default_db)
    command.add_argument("--config", type=Path, default=Path("config/smith.local.toml"))


def run(args: argparse.Namespace) -> int:
    from smith import credentials, picks
    from smith.payload import load_view
    from smith.report_command import _optional_config, profile

    if not args.db.exists():
        print(f"No ledger at {args.db}.")
        return 1
    spec = picks.CLASSES.get(args.product_class)
    if spec is None:
        print(f"Unknown product class {args.product_class}; known: {', '.join(picks.CLASSES)}.")
        return 2
    now = datetime.now(timezone.utc)
    if args.action == "show":
        with closing(ledger.connect_read_only(args.db)) as conn:
            stored = ledger.latest_product_review(conn, spec.key, since=now - timedelta(days=3650))
        if stored is None:
            print(f"No successful review of {spec.key} yet. Run `smith picks run`.")
            return 1
        _print(stored)
        return 0
    config, problem = _optional_config(args.config)
    if problem:
        print(problem)
        return 2
    with closing(ledger.connect_read_only(args.db)) as conn:
        view = load_view(conn, as_of=local_time(now), known_at=now)
        fresh = None if args.force else ledger.latest_product_review(conn, spec.key, since=now - picks.REUSE_AGE)
    if fresh is not None:
        print(f"A review of {spec.key} from {local_time(fresh['created_at']):%Y-%m-%d %H:%M} is still fresh; use --force to redo it.")
        _print(fresh)
        return 0
    outcome = picks.review_and_store(args.db, view, spec, profile(config) if config else None, now=now,
                                     today=local_time(now).date(), secrets=credentials.all_secrets())
    if outcome["outcome"] != "success":
        print(f"Review failed ({outcome.get('error_code')}); recorded as {outcome['review_id'][:8]}.")
        return 1
    with closing(ledger.connect_read_only(args.db)) as conn:
        _print(ledger.latest_product_review(conn, spec.key, since=now - timedelta(minutes=5)))
    return 0


def _print(stored: dict[str, Any]) -> None:
    r = stored["review"]
    print(f"Review {stored['review_id'][:8]} of {r['label']} at {local_time(stored['created_at']):%Y-%m-%d %H:%M}"
          f" (cost {r.get('cost_usd') or '?'} USD)")
    for c in r["candidates"]:
        review = c.get("review") or {}
        score = f" score {review['score']}/10" if review else ""
        print(f"  {c['status']:<10} {c['symbol']:<8} {c['name'][:36]:<36} {c['market']} {c['expense_ratio']:<8} {c['price']} ({c['price_date']}){score}")
        for check in c.get("checks", []):
            print(f"             {check['verdict']:<10} {check['field']}: {check['claimed']} -> {check['found'][:60]}")
    for pick in r["picks"]:
        print(f"  pick {pick['rank']}: {pick['symbol']} — {pick['reason']} / 바뀌는 조건: {pick['condition']}")
    for rej in r["rejected"]:
        print(f"  rejected {rej['symbol']}: {rej['why']}")
    for gap in r["gaps"]:
        print(f"  gap: {gap}")
    if r.get("dropped"):
        print(f"  dropped: {r['dropped']}")
