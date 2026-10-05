"""`smith advise`: build the sanitized context for one use case, check it, ask the adviser, log the run."""
import argparse
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from smith.config import DEFAULT_TIMEZONE

from keyring.errors import KeyringError

from smith import adviser, cases, credentials, ledger
from smith.payload import PayloadRejected, build_context, check_outbound, load_view, redact

CASES = ("portfolio", "funding", "home")
_DECIMAL_INPUT = re.compile(r"\d{1,15}(?:\.\d{1,8})?")
_WHOLE_AMOUNT = re.compile(r"\d{1,15}")
DEFAULT_QUESTIONS = {
    "portfolio": "현재 투자 포트폴리오와 현금흐름을 유지해야 할까, 바꿔야 할까?",
    "funding": "{amount}원을 급하게 마련하려면 무엇이 가장 합리적인가?",
    "home": "{date}까지 {price}원짜리 집으로 이사하려면 어떻게 자금을 마련할까?",
}


def add_parser(sub: Any, default_db: Any) -> None:
    advise = sub.add_parser("advise", help="Ask the read-only adviser about one use case")
    advise.add_argument("--case", choices=CASES, required=True)
    advise.add_argument("--question", help="Override the default question for the case")
    advise.add_argument("--amount", type=_positive_whole_amount, default=Decimal(100_000_000),
                        help="funding: target KRW")
    advise.add_argument("--target-price", type=_positive_whole_amount, default=Decimal(3_000_000_000),
                        help="home: KRW today")
    advise.add_argument("--target-date", type=date.fromisoformat,
                        help="home: YYYY-MM-DD (default: the earliest future home goal, else 3 years ahead)")
    advise.add_argument("--show-payload", action="store_true", help="Print the sanitized context; do not call the model")
    advise.add_argument("--budget-usd", type=_positive_budget, default=adviser.DEFAULT_BUDGET_USD,
                        help=f"Spend cap for the model call (default: {adviser.DEFAULT_BUDGET_USD})")
    advise.add_argument("--model", default=adviser.DEFAULT_MODEL, help=f"Claude model alias or name (default: {adviser.DEFAULT_MODEL})")
    advise.add_argument("--db", type=type(default_db), default=default_db)


def run(args: argparse.Namespace) -> int:
    if os.environ.get(adviser.HEADLESS_ENV):
        print("Refused: smith advise cannot run inside the adviser's own headless process.")
        return 2
    if not args.db.exists():
        print(f"No ledger at {args.db}.")
        return 1
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).date()
    try:
        with closing(ledger.connect_read_only(args.db)) as conn:
            view = load_view(conn, as_of=now.astimezone(ZoneInfo(DEFAULT_TIMEZONE)), known_at=now)
    except (sqlite3.Error, ledger.LedgerError) as error:
        print(f"Ledger error ({type(error).__name__}): not a readable Smith ledger.")
        return 1
    args.target_date = args.target_date or _home_goal_date(view, today) or _years_after(today, 3)
    if args.case == "home" and args.target_date <= today:
        print("Invalid target: --target-date must be in the future.")
        return 2
    facts = _facts(view, args)
    context = build_context(view, facts, include_positions=args.case == "portfolio")
    question = args.question or DEFAULT_QUESTIONS[args.case].format(
        amount=f"{args.amount:,.0f}", price=f"{args.target_price:,.0f}", date=args.target_date.isoformat())
    question = redact(view, question)
    payload = json.dumps({"question": question, "context": context}, ensure_ascii=False, indent=1)
    try:
        secrets = _secrets()
    except KeyringError:
        print("Blocked before sending: the credential store is unavailable, so stored secrets cannot be checked.")
        return 2
    try:
        check_outbound(payload, secrets)
    except PayloadRejected as rejected:
        print(f"Blocked before sending: payload matched {', '.join(rejected.rules)}. Nothing was sent.")
        return 2
    if args.show_payload:
        print(payload)
        return 0
    return _ask(args, view, question, context, payload, now)


def _facts(view: Any, args: argparse.Namespace) -> dict[str, Any]:
    if args.case == "portfolio":
        return cases.portfolio_facts(view)
    if args.case == "funding":
        return cases.funding_facts(view, amount=args.amount)
    return cases.home_facts(view, target_price=args.target_price, target_date=args.target_date)


def _secrets() -> list[str]:
    return credentials.all_secrets()


def _ask(args: argparse.Namespace, view: Any, question: str, context: dict[str, Any], payload: str,
         now: datetime) -> int:
    run_id = uuid.uuid4().hex
    try:
        result = adviser.run_adviser(question, context, executable=adviser.find_claude(),
                                     budget_usd=args.budget_usd, model=args.model, prompt=payload)
    except adviser.AdviserError as error:
        print(f"Adviser failed ({error}). No advice produced.")
        _log(args, run_id, now, question, payload, "failure", error_code=str(error))
        return 1
    if not _log(args, run_id, now, question, payload, "success", cost=result["cost_usd"], advice=result["advice"]):
        print("Advice was validated but withheld because its audit record could not be saved.")
        return 1
    print("\n".join(render(result["advice"], view.aliases)))
    cost = "unknown" if result["cost_usd"] is None else f"${result['cost_usd']}"
    print(f"\n(run {run_id}, {result['prompt_version']}, cost {cost})")
    return 0


def _positive_decimal(value: str) -> Decimal:
    if not _DECIMAL_INPUT.fullmatch(value):
        raise argparse.ArgumentTypeError("must have at most 15 integer and 8 fractional digits")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("must be a decimal amount") from None
    if not number.is_finite() or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite amount")
    return number


def _positive_whole_amount(value: str) -> Decimal:
    if not _WHOLE_AMOUNT.fullmatch(value):
        raise argparse.ArgumentTypeError("must be a whole KRW amount of at most 15 digits")
    return _positive_decimal(value)


def _positive_budget(value: str) -> str:
    _positive_decimal(value)
    return value


def _home_goal_date(view: Any, today: date) -> date | None:
    """The earliest future home goal in the ledger, so the default matches the user's own plan."""
    dates = [g["target_date"] for g in view.summary.goals if g["category"] == "home" and g["target_date"] > today]
    return min(dates, default=None)


def _years_after(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # February 29 -> February 28.
        return value.replace(year=value.year + years, day=28)


def _log(args: argparse.Namespace, run_id: str, now: datetime, question: str, payload: str, outcome: str, *,
         error_code: str | None = None, cost: Any = None, advice: dict[str, Any] | None = None) -> bool:
    try:
        with closing(ledger.connect(args.db)) as conn:
            ledger.record_advice_run(conn, run_id=run_id, created_at=now, use_case=args.case, question=question,
                                     payload=payload, prompt_version=adviser.PROMPT_VERSION,
                                     model=args.model, cost_usd=None if cost is None else str(cost),
                                     outcome=outcome, error_code=error_code,
                                     advice=None if advice is None else json.dumps(advice, ensure_ascii=False))
        return True
    except (OSError, sqlite3.Error, ledger.LedgerError) as error:
        print(f"Warning: could not record the advice run ({type(error).__name__}).")
        return False


def render(advice: dict[str, Any], aliases: dict[str, str]) -> list[str]:
    """Show the advice with a local legend from aliases back to ledger ids (never sent to the model)."""
    lines = ["[추천]", advice["recommendation"]["summary"], advice["recommendation"]["rationale"], "", "[근거: 개인 재무]"]
    lines += [f"- {b['point']} ({', '.join(b['refs'])})" for b in advice["personal_basis"]]
    lines += ["", "[근거: 외부 환경]"] + [f"- {b['point']} ({', '.join(b['refs'])})" for b in advice["external_basis"]]
    lines += ["", "[대안 비교]"]
    for alt in advice["alternatives"]:
        lines += [f"* {alt['name']}: {alt['description']}", f"  비용: {alt['cost']}", f"  위험: {alt['risk']}",
                  f"  유동성: {alt['liquidity']}", f"  장점: {'; '.join(alt['pros'])}", f"  단점: {'; '.join(alt['cons'])}"]
    lines += ["", "[판단을 바꿀 조건]"] + [f"- {c}" for c in advice["reconsider_if"]]
    lines += ["", "[데이터 한계]"] + [f"- {c}" for c in advice["data_limitations"]]
    cited = {r for section in ("personal_basis", "external_basis") for b in advice[section] for r in b["refs"]}
    legend = sorted((alias, record_id) for record_id, alias in aliases.items() if alias in cited)
    if legend:
        lines += ["", "[참조 (로컬 전용)] " + ", ".join(f"{a}={r}" for a, r in legend)]
    return lines
