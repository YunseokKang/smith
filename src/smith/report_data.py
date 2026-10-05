"""Deterministic report data: KPIs, net-worth change decomposition, timeline and sector facts.

Every number a report shows is computed here from one ledger snapshot (see docs/report-design.md
§4-§6). The narrative layer may explain these numbers but never produces new ones.
"""
import logging
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

from smith import cases, checklist, ledger, proposals, realestate
from smith.config import DEFAULT_TIMEZONE, local_time
from smith.payload import LedgerView, base_amount, load_view
from smith.records import Kind, RecordInput, Status
from smith.summary import BASE_CURRENCY, DECIMAL_PRECISION, build_summary, recurring_active

logger = logging.getLogger(__name__)

CATEGORY_LABELS = {
    "cash": "현금", "deposit": "예금", "installment_savings": "적금·청약", "stock": "주식", "fund": "펀드",
    "bond": "채권", "real_estate": "부동산", "lease_deposit": "임차보증금", "insurance_surrender_value": "보험 해약환급금",
    "crypto": "가상자산", "unclassified": "구성 미상 증권", "other": "기타",
}
LIQUIDITY_LABELS = {"immediate": "즉시", "days": "수일 내", "months": "수개월", "restricted": "인출 제한"}
HERMES_LABEL = "Hermes 운용"
# Real changes only: a correction of a past input restates the start and is shown apart (design §6).
COMPONENT_LABELS = {
    "market": "시장 손익(시세 변동)", "fx": "환율 효과", "trades": "매매·입출금·신규(추정)",
    "cash_savings": "현금·예금 증감", "revaluation": "재평가(부동산 등)", "debt": "부채 상환",
}
_CASH_LIKE = ("cash", "deposit", "installment_savings")
_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}
EMERGENCY_MONTHS = 6       # Assumption: emergency reserve of six months of outflow.
RATE_SHOCKS = (Decimal("0.005"), Decimal("0.01"), Decimal("0.02"))
TIMELINE_YEARS = 5


def build_report(conn: sqlite3.Connection, *, as_of: datetime, known_at: datetime,
                 baseline: datetime | None, kind: str, tz: str = DEFAULT_TIMEZONE,
                 household: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble report data. `baseline` is the previous report's time; None marks a first report.

    `as_of` is converted to the household timezone first, because calendar dates come from it.
    The whole build reads one ledger snapshot: an import landing mid-build cannot give the KPIs one
    ledger state and the change analysis or proposals another.
    """
    with ledger.snapshot(conn), localcontext() as context:
        context.prec = DECIMAL_PRECISION  # The same precision policy as the summary (no rounded products).
        return _build_report(conn, as_of=local_time(as_of, tz), known_at=known_at,
                             baseline=None if baseline is None else local_time(baseline, tz), kind=kind,
                             household=household)


def _build_report(conn: sqlite3.Connection, *, as_of: datetime, known_at: datetime,
                  baseline: datetime | None, kind: str, household: dict[str, Any] | None) -> dict[str, Any]:
    view = load_view(conn, as_of=as_of, known_at=known_at)
    change = None if baseline is None else decompose(conn, baseline, as_of, known_at)
    kpis, sectors = _kpis(view, change), _sectors(view)
    # Official transactions matched to each property (local data; only derived figures leave this PC).
    market = realestate.analyze(ledger.load_deals(conn, since=as_of.date() - timedelta(days=400)), today=as_of.date())
    for prop in sectors["real_estate"]["properties"]:
        prop["market"] = market.get(prop["record_id"])
    return {"kind": kind, "as_of": as_of, "baseline": baseline, "kpis": kpis,
            "change": change, "timeline": _timeline(view), "sectors": sectors,
            "advice": proposals.build(view, kpis, sectors, active=ledger.active_proposals(conn), household=household),
            "requests": checklist.requests(view, household),
            "household_profile": _profile(household, as_of),
            "completeness": {"complete": view.summary.complete, "areas": view.summary.incomplete_areas,
                             "unconverted": view.summary.unconverted},
            "warnings": view.summary.warnings, "freshness": view.summary.freshness, "sync": view.summary.sync,
            "view": view}


def decompose(conn: sqlite3.Connection, t0: datetime, t1: datetime, known_at: datetime) -> dict[str, Any]:
    """Split the real net-worth change between t0 and t1 into causes whose sum equals it.

    Corrections recorded after t0 restate the starting point. They are not a real change (design §6),
    so `total` runs from the restated start and the restatement is reported apart as `correction`.
    Positions with quantity and price split into market, trade and FX parts. If the parts do not add up
    (a defect, never rounding), `balanced` is False and the report shows the total without the parts:
    one wrong breakdown must not stop the report from going out.
    """
    with ledger.snapshot(conn):
        original = _active(conn, t0, t0)
        restated = _active(conn, t0, known_at)
        current = _active(conn, t1, known_at)
        rates0 = _rates(conn, t0, t0)
        rates0_restated = _rates(conn, t0, known_at)
        rates1 = _rates(conn, t1, known_at)
    parts = {key: Decimal(0) for key in COMPONENT_LABELS}
    unconverted: set[str] = set()
    # Enough precision that quantity x price x rate is exact, so the sum check only fails when the
    # decomposition itself is wrong, never because of rounding.
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        start = _net_worth(original, rates0, unconverted)
        restated_start = _net_worth(restated, rates0_restated, unconverted)
        for record_id in sorted(set(restated) | set(current)):
            before, after = restated.get(record_id), current.get(record_id)
            for key, amount in _record_change(before, after, rates0_restated, rates1, unconverted).items():
                parts[key] += amount
        end = _net_worth(current, rates1, unconverted)
        total = end - restated_start
        balanced = bool(unconverted) or abs(sum(parts.values()) - total) <= Decimal("0.5")
    if not balanced:
        logger.warning("Net-worth change components do not add up; the breakdown is withheld from the report")
    return {"start": start, "restated_start": restated_start, "end": end, "total": total,
            "correction": restated_start - start, "parts": parts, "balanced": balanced,
            "complete": not unconverted, "unconverted_currencies": sorted(unconverted)}


def _active(conn: sqlite3.Connection, as_of: datetime, known_at: datetime) -> dict[str, RecordInput]:
    return {s.record.record_id: s.record for s in ledger.record_states(conn, as_of=as_of, known_at=known_at)
            if s.record.status is Status.ACTIVE and s.record.kind in (Kind.ASSET, Kind.LIABILITY)}


def _rates(conn: sqlite3.Connection, as_of: datetime, known_at: datetime) -> dict[str, Decimal]:
    summary = build_summary(conn, as_of=as_of, known_at=known_at)
    return {BASE_CURRENCY: Decimal(1), **{f["subject"]: f["value"] for f in summary.fx}}


def _signed_native(record: RecordInput) -> Decimal:
    if record.kind is Kind.ASSET:
        return Decimal(record.fields["value"])
    return -Decimal(record.fields["outstanding_principal"])


def _net_worth(records: dict[str, RecordInput], rates: dict[str, Decimal], unconverted: set[str]) -> Decimal:
    total = Decimal(0)
    for record in records.values():
        currency = record.fields["currency"]
        if currency not in rates:
            unconverted.add(currency)
            continue
        total += _signed_native(record) * rates[currency]
    return total


def _record_change(before: RecordInput | None, after: RecordInput | None, rates0: dict[str, Decimal],
                   rates1: dict[str, Decimal], unconverted: set[str]) -> dict[str, Decimal]:
    record = after or before
    currency = record.fields["currency"]
    # A new record needs only today's rate and a closed one only the old rate.
    needed = [rates for rates, present in ((rates0, before), (rates1, after)) if present is not None]
    if any(currency not in rates for rates in needed):
        unconverted.add(currency)
        return {}
    # Kind first: a new or paid-off loan is a debt change, not a trade.
    opened_or_closed = "debt" if record.kind is Kind.LIABILITY else "trades"
    if before is None:
        return {opened_or_closed: _signed_native(after) * rates1[currency]}
    if after is None:
        return {opened_or_closed: -_signed_native(before) * rates0[currency]}
    fx0, fx1 = rates0[currency], rates1[currency]
    f0, f1 = before.fields, after.fields
    if record.kind is Kind.ASSET and all(f.get(k) for f in (f0, f1) for k in ("quantity", "unit_price")):
        q0, p0 = Decimal(f0["quantity"]), Decimal(f0["unit_price"])
        q1, p1 = Decimal(f1["quantity"]), Decimal(f1["unit_price"])
        # Value = q*p*fx. Market at old quantity and rate, trades at new price, FX on the new value.
        # Residual between quoted value and q*p (fees, rounding) is booked as market movement.
        v0, v1 = Decimal(f0["value"]), Decimal(f1["value"])
        market = q0 * (p1 - p0) * fx0 + ((v1 - q1 * p1) - (v0 - q0 * p0)) * fx0
        return {"market": market, "trades": (q1 - q0) * p1 * fx0, "fx": v1 * (fx1 - fx0)}
    n0, n1 = _signed_native(before), _signed_native(after)
    if record.kind is Kind.LIABILITY:
        key = "debt"
    else:
        key = "cash_savings" if record.fields["category"] in _CASH_LIKE else "revaluation"
    return {key: (n1 - n0) * fx0, "fx": n1 * (fx1 - fx0)}


def _profile(household: dict[str, Any] | None, as_of: datetime) -> dict[str, Any] | None:
    """The part of the local household profile the narrative may see: age, not birth date."""
    if not household:
        return None
    birth = household.get("birth_year")
    return {"age": None if birth is None else as_of.year - int(birth),
            "marriage_registered": household.get("marriage_registered"), "cohabiting": household.get("cohabiting"),
            "retirement_monthly_spend": None if household.get("retirement_monthly_spend") is None
            else str(household["retirement_monthly_spend"]),
            "risk_preference": household.get("risk_preference")}


def _liquidity(view: LedgerView) -> dict[str, Decimal | None]:
    """Asset totals by liquidity; a bucket holding an amount without an FX rate is unknown, not partial.

    Hermes-managed assets are left out: Smith never proposes selling or changing them, so they are not
    money the client can draw on for a reserve, a deposit return or a home (see `_hermes_total`)."""
    totals: dict[str, Decimal | None] = {key: Decimal(0) for key in LIQUIDITY_LABELS}
    for record in view.records:
        if record.kind is not Kind.ASSET or is_hermes(record):
            continue
        key, amount = record.fields["liquidity"], base_amount(view, record)
        current = totals.get(key, Decimal(0))
        totals[key] = None if current is None or amount is None else current + amount
    return totals


def _flows(view: LedgerView) -> tuple[Decimal | None, Decimal | None]:
    """(monthly net, monthly outflow), both unknown when a cash flow could not be converted."""
    summary = view.summary
    if "cash_flow" in summary.incomplete_areas:
        return None, None
    return summary.monthly_net, summary.monthly_outflow


def _kpis(view: LedgerView, change: dict[str, Any] | None) -> dict[str, Any]:
    summary = view.summary
    immediate = _liquidity(view)["immediate"]
    monthly_net, outflow = _flows(view)
    months = immediate / outflow if immediate is not None and outflow else None
    home = _home_progress(view)
    return {"net_worth": summary.net_worth, "net_worth_change": None if change is None else change["total"],
            "monthly_net": monthly_net, "immediate": immediate, "immediate_months": months,
            "home_progress": home}


def _home_progress(view: LedgerView) -> dict[str, Any] | None:
    goals = [g for g in view.summary.goals if g["category"] == "home" and g["target"] is not None
             and g["target_date"] > view.as_of.date()]
    if not goals:
        return None
    goal = min(goals, key=lambda g: g["target_date"])
    facts = cases.home_facts(view, target_price=goal["target"], target_date=goal["target_date"])
    base = facts["scenarios"][0]
    if base["available_self"] is None:
        return None
    required, available = Decimal(base["required_with_costs"]), Decimal(base["available_self"])
    with_partner = None if base["gap_including_partner_property_equity"] is None else \
        required - Decimal(base["gap_including_partner_property_equity"])
    return {"target_date": goal["target_date"], "required": required, "available_self": available,
            "available_with_partner": with_partner, "ratio_self": available / required,
            "scenarios": facts["scenarios"]}


def _timeline(view: LedgerView) -> list[dict[str, Any]]:
    """Dated cash needs and goals within the next few years, oldest first."""
    today = view.as_of.date()
    horizon = date(today.year + TIMELINE_YEARS, today.month, min(today.day, 28))
    items = []
    for record in view.records:
        f = record.fields
        if record.kind is Kind.LIABILITY and f.get("maturity"):
            due = date.fromisoformat(f["maturity"])
            if today < due <= horizon:
                items.append({"date": due, "label": _liability_label(record), "amount": base_amount(view, record),
                              "kind": "cash_need"})
    for goal in view.summary.goals:
        if today < goal["target_date"] <= horizon:
            items.append({"date": goal["target_date"], "label": f"목표: {_goal_label(goal['category'])}",
                          "amount": goal["target"], "kind": "goal"})
    return sorted(items, key=lambda item: item["date"])


def _sectors(view: LedgerView) -> dict[str, Any]:
    return {"cash": _cash_sector(view), "debt": _debt_sector(view), "securities": _securities_sector(view),
            "real_estate": _real_estate_sector(view), "pension_insurance": _pension_insurance_sector(view),
            "macro": {"evidence": view.evidence, "exposures": view.exposures, "announcements": view.announcements}}


def _cash_sector(view: LedgerView) -> dict[str, Any]:
    liquidity = _liquidity(view)
    _, outflow = _flows(view)
    reserve = None if outflow is None else outflow * EMERGENCY_MONTHS
    immediate = liquidity["immediate"]
    gap = None if reserve is None or immediate is None else max(Decimal(0), reserve - immediate)
    ladder = [(LIQUIDITY_LABELS[k], liquidity[k]) for k in ("immediate", "days", "months", "restricted")]
    hermes = _hermes_total(view)
    if hermes != 0:
        ladder.append((HERMES_LABEL, hermes))
    return {"ladder": ladder, "liquidity": liquidity, "immediate": immediate, "reserve_target": reserve,
            "reserve_gap": gap, "emergency_months_assumption": EMERGENCY_MONTHS, "hermes": hermes,
            "toss_cash": _toss_cash(view)}


def is_hermes(record: RecordInput) -> bool:
    """Managed by Hermes, the separate fund manager: never proposed for sale or counted as spendable."""
    return record.fields.get("managed_by") == "hermes"


def _hermes_total(view: LedgerView) -> Decimal | None:
    return _total(base_amount(view, r) for r in view.records if r.kind is Kind.ASSET and is_hermes(r))


def _toss_cash(view: LedgerView) -> dict[str, Any]:
    """The Toss API has no cash balance (only buying power), so brokerage cash is never collected
    automatically. Report that plainly, with buying power as a reference and any manual cash records."""
    toss = any(s["source"] == "toss" for s in view.summary.freshness)
    power = [r for r in view.summary.reference if r["metric"] == "cash_buying_power"]
    manual = [r for r in view.records if r.kind is Kind.ASSET and r.fields["category"] == "cash"
              and r.fields.get("account_type") == "brokerage"]
    return {"synced": toss, "buying_power": power, "manual_records": len(manual),
            "manual_as_of": max((r.effective_at for r in manual), default=None)}


def _total(amounts: Iterable[Decimal | None]) -> Decimal | None:
    """Sum that is unknown when any part is unknown (missing is not zero)."""
    total = Decimal(0)
    for amount in amounts:
        if amount is None:
            return None
        total += amount
    return total


def _debt_sector(view: LedgerView) -> dict[str, Any]:
    loans = [r for r in view.records if r.kind is Kind.LIABILITY]
    variable = [r for r in loans if r.fields["rate_type"] in ("variable", "mixed")]
    variable_total = _total(base_amount(view, r) for r in variable)
    return {"loans": [{"label": _liability_label(r), "category": r.fields["category"], "amount": base_amount(view, r),
                       "rate": Decimal(r.fields["annual_rate"]), "rate_type": r.fields["rate_type"],
                       "maturity": r.fields.get("maturity")} for r in loans],
            "variable_total": variable_total,
            "shocks": [{"shock": s, "monthly_increase": None if variable_total is None else variable_total * s / 12}
                       for s in RATE_SHOCKS],
            "monthly_interest_estimate": view.summary.monthly_loan_interest_estimate}


def _securities_sector(view: LedgerView) -> dict[str, Any]:
    facts = cases.portfolio_facts(view)
    labels = {view.aliases[r.record_id]: r.fields.get("instrument_name") or r.fields.get("symbol")
              for r in view.records if r.kind is Kind.ASSET}
    positions = [{"name": labels.get(p["ref"]) or p["symbol"], "value": Decimal(p["value"]),
                  "share": Decimal(p["share_of_securities"])} for p in facts["top_positions"]
                 if p["value"] is not None and p["share_of_securities"] is not None]
    gains = [cases._unrealized_gain(view, r) for r in view.records if r.kind is Kind.ASSET and r.fields.get("symbol")]
    known = [g for g in gains if g is not None]
    # Positions without a cost basis are left out; their count is kept so the sum reads as partial.
    return {"total": None if facts["securities_total"] is None else Decimal(facts["securities_total"]),
            "positions": positions, "unclassified": _dec(facts["unclassified_securities"]),
            "unrealized_gain": sum(known, Decimal(0)) if known else None,
            "unrealized_gain_counted": len(known), "unrealized_gain_missing": len(gains) - len(known),
            "hermes": _hermes_total(view)}


def _real_estate_sector(view: LedgerView) -> dict[str, Any]:
    properties = []
    for record in (r for r in view.records if r.kind is Kind.ASSET and r.fields["category"] == "real_estate"):
        value = base_amount(view, record)
        debt = _total(base_amount(view, r) for r in view.records if r.kind is Kind.LIABILITY
                      and r.fields.get("collateral_record_id") == record.record_id)
        properties.append({"record_id": record.record_id, "label": _asset_label(record), "value": value, "debt": debt,
                           "ltv": debt / value if value and debt is not None else None,
                           "valued_on": record.effective_at.date()})
    total = view.summary.total_assets
    share = view.summary.by_category.get("real_estate", Decimal(0)) / total if total else None
    return {"properties": properties, "share_of_assets": share}


def _pension_insurance_sector(view: LedgerView) -> dict[str, Any]:
    summary = view.summary
    today = view.as_of.date()
    active = [r for r in view.records if r.kind is Kind.CASHFLOW and r.fields["category"] == "insurance_premium"
              and recurring_active(r.fields, today)]
    premiums = _total(_monthly(base_amount(view, r), r.fields["frequency"]) for r in active)
    restricted = summary.by_liquidity.get("restricted", Decimal(0))
    has_surrender = any(r.kind is Kind.ASSET and r.fields["category"] == "insurance_surrender_value"
                        for r in view.records)
    return {"restricted_total": restricted, "monthly_premiums": premiums,
            "premium_to_income": premiums / summary.monthly_inflow
            if premiums is not None and summary.monthly_inflow else None,
            "surrender_values_known": has_surrender}


def _monthly(amount: Decimal | None, frequency: str) -> Decimal | None:
    return None if amount is None else amount / _MONTHS[frequency]


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _owner_label(owner_id: str) -> str:
    return "본인" if owner_id == "self" else "배우자"


def _asset_label(record: RecordInput) -> str:
    occupancy = {"owner_occupied": "실거주 주택", "leased_out": "임대 중인 주택"}.get(record.fields.get("occupancy") or "")
    base = occupancy or CATEGORY_LABELS.get(record.fields["category"], "자산")
    return f"{base}({_owner_label(record.owner_id)})"


def _liability_label(record: RecordInput) -> str:
    names = {"mortgage": "주택담보대출", "jeonse_loan": "전세자금대출", "credit_loan": "신용대출",
             "credit_line": "마이너스통장", "card_balance": "카드대금", "policy_loan": "보험계약대출",
             "lease_deposit_obligation": "전세보증금 반환", "other": "기타 부채"}
    return f"{names.get(record.fields['category'], '부채')}({_owner_label(record.owner_id)})"


def _goal_label(category: str) -> str:
    return {"home": "주택 이전", "emergency_fund": "비상금", "retirement": "은퇴", "education": "교육",
            "major_purchase": "큰 지출", "debt_repayment": "부채 상환", "other": "기타"}.get(category, category)
