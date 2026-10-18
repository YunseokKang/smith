"""Move-up plan: the target complexes against what selling both homes would leave (report design §18.3).

The household's stated goal is to sell its two homes around the end of the rental lease and move up to one
of a few named complexes, with the expected pay (bonuses, share grants) closing part of the gap. Every
figure here is deterministic and dated:

- each home's net proceeds = price (the recent official trade median, else the ledger value) less the loans
  secured on it, the lease deposit owed and the estimated capital-gains tax (general case; the one-home
  exemption case beside it when the acquisition is known);
- each target's cost = its recent trade median (or the reference price the client entered for a complex with
  no recorded trades, such as one still under construction) plus the purchase costs;
- the spread = cost less the household's net proceeds, now and six months ago (from each complex's own
  six-month change), so the report can say whether the gap is widening or narrowing;
- the expected pay usable before the horizon (base and adverse scenarios) is shown against the spread.

Unknown inputs stay None (never zero); assumptions are named constants echoed in the result.
"""
from datetime import date
from decimal import Decimal
from typing import Any

from smith import future_income, lease
from smith.payload import LedgerView
from smith.realestate import MIN_BASIS, target_ref

BUY_COST_RATE = Decimal("0.035")       # Assumption: acquisition tax and fees on the target (as cases.home_facts).
PAY_RETURN = Decimal("0.04")           # Assumption: pay received before the horizon grows at the base return.
TREND_TOLERANCE = Decimal("0.02")      # A spread move within 2% of the target cost is "flat", not a trend.


def plan(view: LedgerView, properties: list[dict[str, Any]], household: dict[str, Any] | None,
         pay: list[future_income.Inflow], *, today: date, horizon: date,
         market: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The plan for the configured targets; None without `[targets]` in the local config.

    `properties` is report_data's real-estate sector list (ledger value, secured loans, deposits, official
    market figures per property); `market` the official-deal analysis of the targets by `target:<key>` ref.
    """
    config = household or {}
    targets = config.get("targets") or {}
    if not targets:
        return None
    homes = [_home(view, p, (config.get("properties") or {}).get(p["record_id"]) or {}, today) for p in properties]
    proceeds = _proceeds(homes)
    scenarios = {name: future_income.at_goal(pay, owner_id="self", scenario=name, horizon=horizon, today=today,
                                             rate=PAY_RETURN)["value"] for name in ("adverse", "base")}
    market = market or {}
    rows = [_target(key, spec, market.get(target_ref(key)), proceeds, scenarios["base"], scenarios["adverse"], f"후보 {i}")
            for i, (key, spec) in enumerate(targets.items(), 1)]
    return {"horizon": horizon, "homes": homes, "proceeds": proceeds, "pay": scenarios, "targets": rows,
            "assumptions": {"buy_cost_rate": str(BUY_COST_RATE), "pay_return": str(PAY_RETURN),
                            "sale_costs": "매도 비용 제외", "loans_and_deposits": "담보대출·보증금은 지금 잔액 그대로"}}


def _home(view: LedgerView, prop: dict[str, Any], acquisition: dict[str, Any], today: date) -> dict[str, Any]:
    """One home's sale: price basis, what comes off it, and the two tax cases."""
    record = next((r for r in view.records if r.record_id == prop["record_id"]), None)
    owner = record.owner_id if record else "self"
    occupancy = record.fields.get("occupancy") if record else None
    market = prop.get("market") or {}
    estimate, trades = market.get("estimate"), market.get("estimate_basis") or 0
    change, hold = _change(market)
    if estimate is not None and trades >= MIN_BASIS:
        price, basis = estimate, f"최근 6개월 실거래 {trades}건 중간값"
    else:
        price, basis = prop.get("value"), "원장 평가액(실거래 3건 미만)"
    loans, deposits = prop.get("loans"), prop.get("deposits")
    taxes = None if price is None else lease._capital_gains(price, acquisition, owner, occupancy, today)
    before_tax = None if None in (price, loans, deposits) else price - loans - deposits
    earlier = None if price is None or change is None else price / (1 + change)
    return {"record_id": prop["record_id"], "label": prop["label"], "owner": owner, "occupancy": occupancy,
            "price": price, "price_basis": basis, "change_6m": change, "change_hold": hold, "loans": loans, "deposits": deposits,
            "before_tax": before_tax, "tax": None if taxes is None else {k: v["total"] for k, v in taxes.items()},
            "net_general": None if before_tax is None or taxes is None else before_tax - taxes["general"]["total"],
            "net_exempt": None if before_tax is None or taxes is None else before_tax - taxes["exempt"]["total"],
            "exemption_value": None if taxes is None else taxes["general"]["total"] - taxes["exempt"]["total"],
            "before_tax_6m_ago": None if earlier is None or loans is None or deposits is None else earlier - loans - deposits}


def _proceeds(homes: list[dict[str, Any]]) -> dict[str, Any]:
    """The household's cash after selling every home: before tax, general taxation, and with the one-home
    exemption on each (an upper bound; the law allows it for one home at a time). Unknown when any part is."""
    def total(key: str) -> Decimal | None:
        values = [h[key] for h in homes]
        return None if not values or any(v is None for v in values) else sum(values, Decimal(0))
    before = total("before_tax")
    taxed = total("net_general")
    return {"before_tax": before, "general": taxed if taxed is not None else before, "exempt": total("net_exempt"),
            "tax_known": taxed is not None, "six_months_ago": total("before_tax_6m_ago"),
            "hold": [{"label": h["label"], **h["change_hold"]} for h in homes if h["change_hold"]]}


def _change(market: dict[str, Any]) -> tuple[Decimal | None, dict[str, int] | None]:
    """(six-month change, hold): the change only when both half-years have MIN_BASIS deals; otherwise no change
    and, when there were deals at all, the counts that withheld it (review 2026-10-18: one deal against one is no
    trend, and a "참고" label did not say how thin it was)."""
    if market.get("change_6m") is not None:
        return market["change_6m"], None
    recent, older = market.get("estimate_basis") or 0, market.get("previous_basis") or 0
    return None, ({"recent": recent, "older": older} if recent or older else None)


def _target(key: str, spec: dict[str, Any], market: dict[str, Any] | None, proceeds: dict[str, Any],
            pay_base: Decimal | None, pay_adverse: Decimal | None = None, alias: str = "") -> dict[str, Any]:
    m = market or {}
    estimate, trades = m.get("estimate"), m.get("estimate_basis") or 0
    reference = spec.get("reference_price")
    if estimate is not None and trades >= MIN_BASIS:
        price, basis = estimate, f"최근 6개월 실거래 {trades}건 중간값"
    elif estimate is not None:
        price, basis = estimate, f"최근 6개월 실거래 {trades}건(참고용)"
    elif reference:
        price, basis = Decimal(reference), "입력하신 참고 가격(분양가·호가)"
    else:
        price, basis = None, "실거래가 없고 참고 가격도 입력되지 않음"
    cost = None if price is None else price * (1 + BUY_COST_RATE)
    net = proceeds["general"]
    spread = None if cost is None or net is None else cost - net
    # A reference price (no recorded trades) has no past; a thin side withholds the trend.
    change, hold = _change(m) if estimate is not None else (None, None)
    earlier_cost = None if price is None or change is None else price / (1 + change) * (1 + BUY_COST_RATE)
    # The trend compares like with like: both ends before the capital-gains tax (the tax six months ago is not
    # computed). Comparing an after-tax spread now with a pre-tax one then read the tax as a widening gap
    # (review 2026-10-18).
    spread_before_tax = None if cost is None or proceeds["before_tax"] is None else cost - proceeds["before_tax"]
    spread_6m = (None if earlier_cost is None or proceeds["six_months_ago"] is None
                 else earlier_cost - proceeds["six_months_ago"])
    trend = None
    if spread_before_tax is not None and spread_6m is not None and cost:
        moved = (spread_before_tax - spread_6m) / cost
        trend = "widening" if moved > TREND_TOLERANCE else "narrowing" if moved < -TREND_TOLERANCE else "flat"
    return {"key": key, "alias": alias, "label": spec["label"], "region": spec.get("region"), "price": price,
            "price_basis": basis, "spread_before_tax": spread_before_tax,
            "spread_with_pay_adverse": None if spread is None or pay_adverse is None else spread - pay_adverse,
            "trades_6m": trades, "trades_12m": m.get("trades_12m", 0), "change_6m": change, "change_hold": hold,
            "cost_with_fees": cost,
            "spread": spread, "spread_exempt": None if cost is None or proceeds["exempt"] is None else cost - proceeds["exempt"],
            "spread_with_pay": None if spread is None or pay_base is None else spread - pay_base,
            "spread_6m_ago": spread_6m, "trend": trend,
            # Withheld for want of deals; a complex priced by reference (no trades at all) simply has no past.
            "trend_hold": trend is None and estimate is not None and bool(hold or proceeds["hold"])}
