"""Holdings review: per-holding facts and the plan for goal money held in stocks (docs/report-design.md §12).

Everything here is deterministic. The narrative stage gives an opinion on each holding (add, hold, trim,
exit) on top of these facts, and code checks that opinion (smith.narrative). Hermes-managed holdings are
listed but never get an opinion or a sale plan.
"""
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Any

from smith import cases
from smith.fmt import short_won
from smith.payload import LedgerView, base_amount, unrealized_gain
from smith.records import Kind

CONCENTRATION_LIMIT = Decimal("0.25")  # Assumption: one holding above a quarter of the stocks is concentrated.
GLIDE_HORIZON_MONTHS = 36              # Money needed within three years should not depend on stock prices.
GLIDE_START_MONTHS = 24                # Start moving it to safe assets this long before the date,
GLIDE_END_MONTHS = 6                   # and finish this long before.
STRESS_DRAWDOWN = Decimal("0.30")      # Assumption: a bad year for large-cap stock indices.
STANCES = {"add": "추가 매수 고려", "hold": "유지", "trim": "일부 정리", "exit": "정리"}
# Securities the stock checks cover, listed or not: a fund or security of unknown make-up held for trading
# is part of "the stocks" even without a symbol (review 2026-10-11: leaving it out made the checks pass).
SECURITIES = ("stock", "fund", "unclassified")
TRADABLE = ("immediate", "days")
# Instrument names from these import sources come from the provider; a manual name is free text and never
# leaves the PC in a research query or a model payload (the symbol and market do).
PUBLIC_NAME_SOURCES = ("toss",)
_CASH_LIKE = ("cash", "deposit", "installment_savings")
_MARKET_OVERSEAS = {"KR": False, "US": True, "other": True}


# Index funds recognised by symbol (an explicit, reviewable list; extend it as holdings change). Broad funds
# hold a whole market; narrow ones track an index concentrated in one sector or style. SPLG was renamed SPYM
# on 2025-10-31 (State Street notice); both are kept so older records still classify.
BROAD_INDEX = {"VOO": "S&P 500", "SPY": "S&P 500", "IVV": "S&P 500", "SPYM": "S&P 500", "SPLG": "S&P 500",
               "VTI": "미국 전체 시장", "ITOT": "미국 전체 시장", "SCHB": "미국 전체 시장", "VT": "세계 전체 시장",
               "069500": "코스피 200", "102110": "코스피 200", "360750": "S&P 500"}
NARROW_INDEX = {"QQQ": "나스닥 100(기술주 중심)", "QQQM": "나스닥 100(기술주 중심)", "133690": "나스닥 100(기술주 중심)"}
OTHER_FUNDS = {"SCHD": "미국 배당 성장 ETF", "JEPQ": "나스닥 커버드콜 ETF", "JEPI": "S&P 500 커버드콜 ETF"}
_KR_ETF_BRANDS = ("KODEX", "TIGER", "ACE", "KBSTAR", "RISE", "SOL", "HANARO", "ARIRANG", "PLUS", "KOSEF")


def kind(position: dict[str, Any]) -> str:
    """broad_index, narrow_index, fund (another ETF) or stock (an individual company)."""
    symbol, name = position["symbol"], str(position["name"])
    if symbol in BROAD_INDEX:
        return "broad_index"
    if symbol in NARROW_INDEX:
        return "narrow_index"
    if symbol in OTHER_FUNDS or name.upper().startswith(_KR_ETF_BRANDS):
        return "fund"
    return "stock"


KIND_LABELS = {"broad_index": "넓은 지수펀드", "narrow_index": "좁은 지수펀드", "fund": "기타 펀드·ETF", "stock": "개별 종목"}


@dataclass(frozen=True)
class Need:
    """A dated amount an owner must pay from their cash, savings or stocks (not from other sources such as
    a new tenant's deposit or a home sale, which the caller has already subtracted)."""
    label: str
    due: date
    amount: Decimal
    owner: str = "self"


def review(view: LedgerView) -> dict[str, Any]:
    """Listed holdings, largest first, and portfolio figures. Lots of one instrument (market, symbol,
    currency) held in several accounts are one position; Hermes-managed lots stay apart. Shares are of all
    the securities Smith may give opinions on, including those without a symbol (`unlisted`); a total that
    includes an unconverted amount is unknown."""
    lots: dict[tuple[Any, ...], list[Any]] = {}
    for record in sorted(view.records, key=lambda r: r.record_id):
        f = record.fields
        if record.kind is Kind.ASSET and f.get("symbol"):
            lots.setdefault((f.get("market"), f["symbol"], f["currency"], _hermes(record)), []).append(record)
    positions = [_position(view, records) for records in lots.values()]
    mine = [p for p in positions if not p["hermes"]]
    unlisted = _sum(base_amount(view, r) for r in view.records if r.kind is Kind.ASSET and not r.fields.get("symbol")
                    and r.fields["category"] in SECURITIES and r.fields["liquidity"] in TRADABLE and not _hermes(r))
    listed = _sum(p["value"] for p in mine)
    total = None if listed is None or unlisted is None else listed + unlisted
    symbols = [p["symbol"] for p in mine]
    for p in positions:
        p["share"] = p["value"] / total if total and p["value"] is not None and not p["hermes"] else None
        # A fund spreads over many companies; the single-company limit is for individual companies.
        p["kind"] = kind(p)
        p["concentrated"] = p["kind"] == "stock" and p["share"] is not None and p["share"] > CONCENTRATION_LIMIT
        # One opinion per position: the symbol, or symbol.market when the symbol is listed on two markets.
        p["key"] = p["symbol"] if symbols.count(p["symbol"]) <= 1 else f"{p['symbol']}.{p['market'] or 'unknown'}"
    positions.sort(key=lambda p: (p["value"] is None, -(p["value"] or 0), p["key"]))
    shares = [p["share"] for p in mine]
    known = bool(shares) and None not in shares
    overseas = None if any(p["overseas"] is None for p in mine) else _sum(p["value"] for p in mine if p["overseas"])
    return {"positions": positions, "total": total, "listed_total": listed, "unlisted": unlisted,
            "top1_share": max(shares) if known else None,
            "top5_share": sum(sorted(shares, reverse=True)[:5], Decimal(0)) if known else None,
            "overseas_share": overseas / total if total and overseas is not None else None,
            "concentrated": [p["key"] for p in mine if p["concentrated"]],
            "concentration_limit": CONCENTRATION_LIMIT}


def _position(view: LedgerView, records: list[Any]) -> dict[str, Any]:
    """One position from the lots of an instrument. Quantity and value add up; the average cost is the
    quantity-weighted one, unknown (with the return and gain) when any lot lacks its cost or quantity."""
    first = records[0].fields
    quantities = [_dec(r.fields.get("quantity")) for r in records]
    costs = [_dec(r.fields.get("average_cost")) for r in records]
    quantity = _sum(quantities)
    cost = (_sum(q * c for q, c in zip(quantities, costs)) / quantity
            if quantity and None not in costs else None)
    priced = [r for r in records if r.fields.get("unit_price") is not None]
    # The newest valuation's price: a manual lot can carry an older price than a synced one.
    price = _dec(max(priced, key=lambda r: r.effective_at).fields["unit_price"]) if priced else None
    public = all(view.record_sources.get(r.record_id) in PUBLIC_NAME_SOURCES for r in records)
    return {"ref": view.aliases[records[0].record_id], "symbol": first["symbol"],
            "name": first.get("instrument_name") or first["symbol"],
            "public_name": (first.get("instrument_name") or first["symbol"]) if public else first["symbol"],
            "market": first.get("market"), "currency": first["currency"], "quantity": quantity,
            "unit_price": price, "average_cost": cost, "value": _sum(base_amount(view, r) for r in records),
            "lots": len(records), "return_on_cost": (price - cost) / cost if price is not None and cost else None,
            "unrealized_gain": _sum(unrealized_gain(view, r) for r in records),
            "overseas": _MARKET_OVERSEAS.get(first.get("market") or ""), "hermes": _hermes(records[0])}


def owner_resources(view: LedgerView) -> dict[str, dict[str, Decimal | None]]:
    """Per owner: tradable securities (listed or not), cash and deposits usable within days, and the
    monthly net cash flow after restricted transfers (negative when spending exceeds income). Hermes-managed
    assets are left out. An owner's goal is paid from that owner's resources only."""
    owners = sorted({r.owner_id for r in view.records})
    found = {}
    for owner in owners:
        assets = [r for r in view.records if r.kind is Kind.ASSET and r.owner_id == owner and not _hermes(r)
                  and r.fields["liquidity"] in TRADABLE]
        found[owner] = {
            "stocks": _sum(base_amount(view, r) for r in assets
                           if r.fields.get("symbol") or r.fields["category"] in SECURITIES),
            "cash": _sum(base_amount(view, r) for r in assets if r.fields["category"] in _CASH_LIKE),
            "monthly": cases.monthly_net_cash_flow(view, owner_id=owner)}
    return found


def glide_plan(*, today: date, needs: list[Need], resources: dict[str, dict[str, Decimal | None]]) -> dict[str, Any] | None:
    """The part of each owner's dated needs within the horizon that only selling that owner's stocks can pay,
    and a quarterly schedule to move it to safe assets before each date.

    An owner's needs share one pool: cash now plus the monthly net saved until each date (a deficit shrinks
    it). By each date the stocks must cover what all that owner's needs so far exceed the pool, up to the
    stocks held. A need later this month counts with no months of saving and is moved now. None when no need
    depends on stocks and every input is known; `unknown` lists owners whose inputs are unconverted.
    """
    steps, unknown = [], []
    stocks_total = covered_total = Decimal(0)
    owners = sorted({n.owner for n in needs})
    for owner in owners:
        mine = sorted((n for n in needs if n.owner == owner and today < n.due and n.amount > 0
                       and _months(today, n.due) <= GLIDE_HORIZON_MONTHS), key=lambda n: n.due)
        res = resources.get(owner, {})
        stocks, cash, monthly = res.get("stocks"), res.get("cash"), res.get("monthly")
        if not mine:
            continue
        if stocks is None or cash is None or monthly is None:
            unknown.append(owner)
            continue
        stocks_total += stocks
        total_need, covered = Decimal(0), Decimal(0)
        for need in mine:
            months = _months(today, need.due)
            total_need += need.amount
            from_stocks = min(stocks, max(Decimal(0), total_need - (cash + monthly * months))) - covered
            if from_stocks <= 0:
                continue
            covered += from_stocks
            start = max(today, add_months(need.due, -GLIDE_START_MONTHS))
            end = max(start, add_months(need.due, -GLIDE_END_MONTHS))
            quarters = max(1, int((Decimal(_months(start, end)) / 3).to_integral_value(rounding=ROUND_CEILING)))
            steps.append({"label": need.label, "owner": owner, "due": need.due, "months": months, "amount": need.amount,
                          "from_stocks": from_stocks, "start": start, "end": end, "quarters": quarters,
                          "per_quarter": from_stocks / quarters, "started": start <= today})
        covered_total += covered
    if not steps and not unknown:
        return None
    steps.sort(key=lambda s: s["due"])
    return {"steps": steps, "unknown": unknown, "earmarked": covered_total, "stress": covered_total * STRESS_DRAWDOWN,
            "stocks": stocks_total, "free": stocks_total - covered_total, "drawdown": STRESS_DRAWDOWN}


def add_condition(*, reserve_gap: Decimal | None, glide: dict[str, Any] | None, unsecured_debt: Decimal | None,
                  core_short: bool) -> str:
    """What must hold before buying more, written by code and attached to an "add" opinion: the emergency
    reserve first, no unsecured debt, goal money not grown with stocks, and the index core before single
    companies (`core_short` for an individual stock while the core is below its standard)."""
    parts = []
    if reserve_gap is None:
        parts.append("비상금이 충분한지 먼저 확인하신 뒤")
    elif reserve_gap > 0:
        parts.append(f"비상금 부족분 {short_won(reserve_gap)}을 먼저 채우신 뒤")
    if unsecured_debt is None:
        parts.append("신용성 대출이 없는지 확인하신 뒤")
    elif unsecured_debt > 0:
        parts.append(f"신용대출 등 {short_won(unsecured_debt)}을 먼저 갚으신 뒤")
    if core_short:
        parts.append("넓은 지수펀드 비중을 기준까지 먼저 채운 다음")
    if glide and glide["steps"]:
        labels = "·".join(dict.fromkeys(s["label"] for s in glide["steps"]))
        parts.append(f"{labels} 자금 계획에 들어가지 않는 돈으로만(지금 주식 {short_won(glide['earmarked'])}이 그 계획에 "
                     "들어가 있습니다)")
    return "" if not parts else "전제 조건(Smith 계산): " + ", ".join(parts) + " 사시기 바랍니다."


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, min(day.day, 28))


def _months(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def _hermes(record: Any) -> bool:
    return record.fields.get("managed_by") == "hermes"


def _sum(amounts: Any) -> Decimal | None:
    total = Decimal(0)
    for amount in amounts:
        if amount is None:
            return None
        total += amount
    return total


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))
