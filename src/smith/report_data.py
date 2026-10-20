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

from smith import (cases, checklist, editions, future_income, holdings, lease, ledger, move_plan, pension, picks, principles,
                   product_review, proposals, realestate, stress, tax)
from smith.config import DEFAULT_TIMEZONE, local_time
from smith.payload import LedgerView, base_amount, load_view, unrealized_gain
from smith.records import TRANSFER_CATEGORIES, Kind, RecordInput, Status
from smith.summary import BASE_CURRENCY, DECIMAL_PRECISION, build_summary, recurring_active

logger = logging.getLogger(__name__)

CATEGORY_LABELS = {
    "cash": "현금", "deposit": "예금", "installment_savings": "적금·청약", "stock": "주식", "fund": "펀드",
    "bond": "채권", "real_estate": "부동산", "lease_deposit": "임차보증금", "insurance_surrender_value": "보험 해약환급금",
    "crypto": "가상자산", "unclassified": "구성 미상 증권", "other": "기타",
}
LIQUIDITY_LABELS = {"immediate": "즉시", "days": "수일 내", "months": "수개월", "restricted": "인출 제한"}
PAY_LABELS = {"bonus": "성과급", "stock_compensation": "주식 보상"}
CERTAINTY_LABELS = {"confirmed": "확정", "expected": "예상", "possible": "가능성"}
HERMES_LABEL = "Hermes 운용"
# Real changes only: a correction of a past input restates the start and is shown apart (design §6).
COMPONENT_LABELS = {
    "market": "시장 손익(시세 변동)", "fx": "환율 효과", "trades": "매매·입출금·신규(추정)",
    "cash_savings": "현금·예금 증감", "revaluation": "재평가(부동산 등)", "debt": "부채 상환",
}
_CASH_LIKE = ("cash", "deposit", "installment_savings")
_SECURITIES = ("stock", "fund", "bond", "crypto", "unclassified")
_PENSION_ACCOUNTS = ("pension_savings", "irp", "dc")
# The hero's composition bar (report design §18.1): what the balance sheet is made of, by owner.
COMPOSITION_LABELS = {"real_estate": "부동산", "securities": "주식·펀드", "cash": "현금·예금", "pension": "연금·보험",
                      "other": "기타"}
# Outflows read as fixed or discretionary when the record does not say (`commitment`), for the spending split.
_FIXED_CATEGORIES = ("loan_payment", "insurance_premium", "housing_cost", "tax")
_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}
EMERGENCY_MONTHS = 6       # Assumption: emergency reserve of six months of outflow.
RATE_SHOCKS = (Decimal("0.005"), Decimal("0.01"), Decimal("0.02"))
SECURED_LOAN_CATEGORIES = ("mortgage",)  # Loans that count in a property's loan-to-value ratio.
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
    salary = (household or {}).get("annual_gross_salary")
    pay = future_income.inflows(view, salary=None if salary is None else Decimal(salary))
    expected = future_income.review(view, pay)
    # The stocks the client's pay arrives in are the employer's: holding them doubles the concentration
    # already carried by the salary (report design §17.4), so they always get an opinion.
    employer = frozenset(s["symbol"] for s in expected["same_stock"] if s["symbol"])
    review = holdings.review(view, employer)
    kpis, sectors = _kpis(view, change, pay, household), _sectors(view)
    # Official transactions matched to each property (local data; only derived figures leave this PC).
    market = realestate.analyze(ledger.load_deals(conn, since=as_of.date() - timedelta(days=400)), today=as_of.date())
    for prop in sectors["real_estate"]["properties"]:
        prop["market"] = market.get(prop["record_id"])
    # Move-target complexes (report design §18.3) share the official-deal store under "target:<key>" refs.
    targets_market = {ref: found for ref, found in market.items() if ref.startswith(realestate.TARGET_PREFIX)}
    # How each lease deposit due is actually returned (report design §17.2): the plans feed the proposals,
    # the stress test (reverse-jeonse gap, not the whole deposit) and the property day's section.
    lease_plans = lease.plans(view, sectors["real_estate"]["properties"], household, today=as_of.date(),
                              property_drop=stress.PROPERTY_DROP)
    advice = proposals.build(view, kpis, sectors, active=ledger.active_proposals(conn), household=household, pay=pay,
                             review=review, lease_plans=lease_plans)
    # The status badge stays household-wide: urgent proposals of every area, whatever today's theme.
    advice["household_urgent"] = [p.key for p in advice["proposals"] if p.priority == 1]
    # A daily edition carries only its own proposal rules and their pending judgements; each rule belongs
    # to one themed day (the overview carries all of them).
    advice["proposals"] = [p for p in advice["proposals"] if editions.carries(kind, p.key)]
    advice["undetermined"] = [u for u in advice["undetermined"] if editions.carries(kind, u.key)]
    advice["assumptions"] = advice["assumptions"] + future_income.assumptions(pay)
    # The products the review team chose for a proposal that moves money (report design §26), sized here.
    reviews = picks.load(conn, known_at)
    advice["proposals"] = picks.attach(advice["proposals"], reviews, view.rates)
    plan = tax.facts(view, as_of.date(), household)
    pension_stack = pension.stack(view, household, as_of.date())
    data = {"kind": kind, "as_of": as_of, "baseline": baseline, "kpis": kpis,
            "change": change, "timeline": _timeline(view, pay), "sectors": sectors, "holdings": review,
            "future_income": expected, "advice": advice, "lease_plans": lease_plans,
            # One bad year with several shocks at once (report design §16.2), household-wide like the badge.
            "stress": stress.test(view, kpis, sectors, lease_plans),
            # This year's pension credit room and overseas gains, for the tax day's headline numbers.
            "tax_plan": {**tax.pension_plan(plan), "us_gains": plan["us_gains"], "us_unknown": plan["us_unknown"],
                         "us_positions": plan["us_positions"], "pension_unconverted": plan["pension_unconverted"]},
            "requests": checklist.requests(view, household, pension_stack=pension_stack),
            "household_profile": _profile(household, as_of),
            # The balance sheet by kind and owner (hero bar), the four pension pipelines against the monthly
            # target (tax day), the move-target spread (property day) and the month's fixed/discretionary split.
            "composition": _composition(view),
            "pension_stack": pension_stack,
            # What each insurer's annuity is worth on its own illustration (report design §19, FR-24).
            "annuity_reviews": product_review.annuity_reviews(view, household, as_of.date()),
            "move_plan": move_plan.plan(view, sectors["real_estate"]["properties"], household, pay, today=as_of.date(),
                                        horizon=_move_horizon(view, lease_plans), market=targets_market),
            "spending_split": _spending_split(view),
            "product_reviews": reviews,
            "research_regions": sorted({t["region"] for t in ((household or {}).get("targets") or {}).values()
                                        if t.get("region")}),
            "completeness": {"complete": view.summary.complete, "areas": view.summary.incomplete_areas,
                             "unconverted": view.summary.unconverted},
            "warnings": view.summary.warnings, "freshness": view.summary.freshness, "sync": view.summary.sync,
            "view": view}
    data["policy"] = principles.policy(data)  # Investment policy checks (report design §13), from the data above.
    data["figures"] = _edition_figures(data)
    return data


def _edition_figures(data: dict[str, Any]) -> dict[str, Any]:
    """The few sums the daily editions' headline tiles show beyond the sector data (report design §14). A
    sum with an unknown part is unknown; the listed-holdings figures share one scope (not Hermes-managed)."""
    props = data["sectors"]["real_estate"]["properties"]
    listed = [p for p in data["holdings"]["positions"] if not p["hermes"]]
    gains = [p["unrealized_gain"] for p in listed]
    known = [g for g in gains if g is not None]
    summary = data["view"].summary
    net = data["kpis"]["monthly_net"]
    # The official-trade estimate of the whole property line is known only when every property has enough
    # recent trades; otherwise the gap to the ledger value cannot be stated (missing is not zero).
    estimates = [(p["market"] or {}).get("estimate") if p.get("market") else None for p in props]
    enough = all(p.get("market") and p["market"]["estimate_basis"] >= realestate.MIN_BASIS for p in props)
    # Every home traded at least once lately but one too rarely for a median: the sum is still stated, as 참고,
    # with the thin homes named (user review 2026-10-18 asked for the ledger value against the trade basis).
    thin = [p["label"] for p in props if p.get("market") and p["market"]["estimate"] is not None
            and p["market"]["estimate_basis"] < realestate.MIN_BASIS]
    return {
        "property_value": _total(p["value"] for p in props) if props else None,
        "property_debt": _total(p["debt"] for p in props) if props else None,
        "property_trades_6m": sum(p["market"]["estimate_basis"] for p in props if p.get("market")),
        "property_estimate": _total(estimates) if props and enough else None,
        "property_estimate_reference": _total(estimates) if props and not enough and thin else None,
        "property_estimate_thin": thin,
        "property_valuation": sorted({VALUATION_LABELS.get(p["valuation_method"], p["valuation_method"]) for p in props}),
        "listed_count": len(listed), "listed_value": _total(p["value"] for p in listed) if listed else None,
        # Like the holdings section: a partial sum over the holdings whose cost is known, with the counts.
        "listed_gain": sum(known, Decimal(0)) if known else None,
        "listed_gain_counted": len(known), "listed_gain_missing": len(gains) - len(known),
        # What is left after spending and the transfers to the client's own savings and investment accounts.
        "monthly_left": None if net is None or "cash_flow" in summary.incomplete_areas else net - summary.monthly_transfers,
    }


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
    # Two headline figures (report design §18.2): what the markets did to what the household already held
    # (prices, FX, revaluations) and what moved in or out (trades and deposits, cash saved, debt repaid, records
    # added). Summing them into one total hid a market loss behind new ledger entries (user review 2026-10-18).
    groups = None if not balanced else {"market_and_valuation": parts["market"] + parts["fx"] + parts["revaluation"],
                                        "net_inflow": parts["trades"] + parts["cash_savings"] + parts["debt"]}
    return {"start": start, "restated_start": restated_start, "end": end, "total": total,
            "correction": restated_start - start, "parts": parts, "balanced": balanced, "groups": groups,
            "from": t0.date(), "to": t1.date(),
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


GROUP_LABELS = {"market_and_valuation": "시장·평가 변동", "net_inflow": "입출금·원장 등록"}
VALUATION_LABELS = {"manual": "원장 수동 입력가", "statement": "기관 명세서 값", "market": "시세×수량",
                    "appraisal": "외부 평가(KB 시세 등)", "api": "API 조회값"}


def _composition(view: LedgerView) -> dict[str, Any]:
    """Assets by kind (property, securities, cash, pensions and insurance, other) and by owner, for the hero's
    composition bar. An asset without an FX rate is left out and the result says so (`complete`)."""
    groups: dict[str, dict[str, Any]] = {key: {"key": key, "label": label, "value": Decimal(0), "by_owner": {}}
                                         for key, label in COMPOSITION_LABELS.items()}
    complete = True
    for record in view.records:
        if record.kind is not Kind.ASSET:
            continue
        value = base_amount(view, record)
        if value is None:
            complete = False
            continue
        f = record.fields
        if f.get("account_type") in _PENSION_ACCOUNTS or f["category"] == "insurance_surrender_value":
            key = "pension"
        elif f["category"] == "real_estate":
            key = "real_estate"
        elif f["category"] in _SECURITIES:
            key = "securities"
        elif f["category"] in _CASH_LIKE:
            key = "cash"
        else:
            key = "other"
        group = groups[key]
        group["value"] += value
        group["by_owner"][record.owner_id] = group["by_owner"].get(record.owner_id, Decimal(0)) + value
    found = sorted((g for g in groups.values() if g["value"]), key=lambda g: -g["value"])
    total = sum((g["value"] for g in found), Decimal(0))
    for group in found:
        group["share"] = group["value"] / total if total else None
    return {"groups": found, "total": total, "complete": complete}


def _spending_split(view: LedgerView) -> dict[str, Any] | None:
    """The month as fixed costs, discretionary spending, transfers to the client's own accounts and what is
    left (report design §18.6). A flow says which it is (`commitment`); otherwise loan payments, premiums,
    housing costs and taxes are fixed and the rest discretionary. None when a flow could not be converted."""
    summary = view.summary
    if "cash_flow" in summary.incomplete_areas:
        return None
    today = view.as_of.date()
    fixed = discretionary = Decimal(0)
    for record in view.records:
        f = record.fields
        if record.kind is not Kind.CASHFLOW or f["direction"] != "outflow" or f["category"] in TRANSFER_CATEGORIES \
                or f["frequency"] not in _MONTHS or not recurring_active(f, today):
            continue
        amount = base_amount(view, record)
        if amount is None:
            return None
        monthly = amount / _MONTHS[f["frequency"]]
        commitment = f.get("commitment") or ("fixed" if f["category"] in _FIXED_CATEGORIES else "discretionary")
        if commitment == "fixed":
            fixed += monthly
        else:
            discretionary += monthly
    inflow = summary.monthly_inflow
    return {"inflow": inflow, "fixed": fixed, "discretionary": discretionary, "transfers": summary.monthly_transfers,
            "left": inflow - fixed - discretionary - summary.monthly_transfers,
            "fixed_share": fixed / inflow if inflow else None,
            "discretionary_share": discretionary / inflow if inflow else None}


def _move_horizon(view: LedgerView, lease_plans: list[dict[str, Any]]) -> date:
    """When the move is planned: the nearest home goal, else the nearest lease deposit due, else two years."""
    today = view.as_of.date()
    goals = [g["target_date"] for g in view.summary.goals if g["category"] == "home" and g["target_date"] > today]
    if goals:
        return min(goals)
    if lease_plans:
        return lease_plans[0]["due"]
    return date(today.year + 2, today.month, min(today.day, 28))


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


def _kpis(view: LedgerView, change: dict[str, Any] | None, pay: list[future_income.Inflow],
          household: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = view.summary
    immediate = _liquidity(view)["immediate"]
    monthly_net, outflow = _flows(view)
    months = immediate / outflow if immediate is not None and outflow else None
    home = _home_progress(view, pay, household)
    return {"net_worth": summary.net_worth, "net_worth_change": None if change is None else change["total"],
            "monthly_net": monthly_net, "immediate": immediate, "immediate_months": months,
            "home_progress": home}


def _home_progress(view: LedgerView, pay: list[future_income.Inflow],
                   household: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The nearest home goal in the base scenario, on three bases (report design §17.3):

    - conservative: what exists today, the client's own resources plus the partner's property equity
      (`available_household`); the client-only figure (`available_self`) stays beside it;
    - baseline: the conservative figure plus the confirmed and expected pay due before the goal, after the
      estimated final tax (`available_household_with_pay`); this is the headline (user decision 2026-10-17);
    - tax variants: the baseline less the estimated capital-gains tax on the partner's home when it is sold
      for the move, with and without the one-home exemption (`after_tax`), when its acquisition is known.

    The stock glide plan (`from_liquid`) uses the conservative household basis: a dated need is never planned
    on pay that is not yet certain, but the partner's equity is the household's money.
    """
    goals = [g for g in view.summary.goals if g["category"] == "home" and g["target"] is not None
             and g["target_date"] > view.as_of.date()]
    if not goals:
        return None
    goal = min(goals, key=lambda g: g["target_date"])
    facts = cases.home_facts(view, target_price=goal["target"], target_date=goal["target_date"], pay=pay)
    base = facts["scenarios"][0]
    if base["available_self"] is None:
        return None
    required, available = Decimal(base["required_with_costs"]), Decimal(base["available_self"])
    partner_equity = _dec(facts["partner_property_equity"])
    with_partner = None if partner_equity is None else available + partner_equity
    # What liquid money (grown at the scenario's return) and savings must cover once the home equity is
    # used: the holdings glide plan (§12.4) asks how much of that rests on stocks.
    grown = Decimal(facts["liquid_assets_self"]) * (1 + Decimal(base["assumptions"]["investment_return"])) ** (
        Decimal(facts["months"]) / 12)
    from_liquid = max(Decimal(0), required - (with_partner if with_partner is not None else available) + grown
                      + Decimal(facts["projected_savings_from_monthly_net"]))
    with_pay = _dec(base["available_self_with_expected_pay"])
    household_with_pay = _dec(base["available_household_with_expected_pay"])
    baseline, label = _home_baseline(available, with_partner, household_with_pay)
    after_tax = _partner_home_tax(view, household, baseline, view.as_of.date())
    return {"target_date": goal["target_date"], "required": required, "available_self": available,
            "available_with_partner": with_partner, "ratio_self": available / required,
            "available_household": with_partner, "partner_equity": partner_equity,
            "ratio_household": None if with_partner is None else with_partner / required,
            "available_household_with_pay": household_with_pay,
            "ratio_household_with_pay": None if household_with_pay is None else household_with_pay / required,
            "baseline": baseline, "baseline_label": label, "ratio_baseline": baseline / required,
            "gap_baseline": required - baseline, "after_tax": after_tax,
            "from_liquid": from_liquid, "scenarios": facts["scenarios"],
            # The plan's own monthly saving and horizon: the spending room (§16.3) must not spend them twice.
            "monthly_net_self": _dec(facts["monthly_net_cash_flow_self"]), "months": facts["months"],
            "pay_at_target": _dec(base["expected_pay_at_target"]), "pay_after_target": _dec(base["expected_pay_after_target"]),
            "available_with_pay": with_pay, "ratio_with_pay": None if with_pay is None else with_pay / required}


def _home_baseline(available_self: Decimal, household: Decimal | None, household_with_pay: Decimal | None) -> tuple[Decimal, str]:
    """The headline figure and what it counts: the household basis with expected pay when known, else the
    household basis, else the client alone (an unknown part never silently drops to zero)."""
    if household_with_pay is not None:
        return household_with_pay, "가구 통합 + 예정 소득(확정·예상, 세후)"
    if household is not None:
        return household, "가구 통합(지금 있는 자산)"
    return available_self, "본인 자원(배우자 자산 미상)"


def _partner_home_tax(view: LedgerView, household: dict[str, Any] | None, baseline: Decimal,
                      today: date) -> dict[str, Any] | None:
    """The baseline less the estimated capital-gains tax when the partner's home is sold for the move:
    the one-home exemption case (two years of residence) and the general case. None without the
    partner's home or its acquisition data in the local profile."""
    config = (household or {}).get("properties") or {}
    homes = [r for r in view.records if r.kind is Kind.ASSET and r.fields["category"] == "real_estate"
             and r.owner_id != "self"]
    if not homes:
        return None
    totals = {"exempt": Decimal(0), "general": Decimal(0)}
    for record in homes:
        spec = config.get(record.record_id) or {}
        value = base_amount(view, record)
        if value is None or not spec.get("acquired_year") or not spec.get("acquired_price"):
            return None
        held = max(0, today.year - int(spec["acquired_year"]) - 1)
        acquired = Decimal(spec["acquired_price"])
        totals["exempt"] += tax.capital_gains(value, acquired, held=held, lived=lease.RESIDENCE_YEARS_FOR_EXEMPTION,
                                              one_home=True)["total"]
        totals["general"] += tax.capital_gains(value, acquired, held=held, lived=0, one_home=False)["total"]
    return {"tax": totals, "available": {key: baseline - total for key, total in totals.items()}}


def _timeline(view: LedgerView, pay: list[future_income.Inflow]) -> list[dict[str, Any]]:
    """Dated cash needs, goals and expected pay (net, kind "inflow") within the next few years, oldest first."""
    today = view.as_of.date()
    horizon = date(today.year + TIMELINE_YEARS, today.month, min(today.day, 28))
    # Overdue pay is not a coming date: the expected-pay section lists it for the client to confirm.
    items = _pay_rows(pay, horizon)
    for record in view.records:
        f = record.fields
        if record.kind is Kind.LIABILITY and f.get("maturity"):
            due = date.fromisoformat(f["maturity"])
            amount = base_amount(view, record)
            # A credit line with nothing drawn has a maturity but no money to find (review 2026-10-18: the
            # nearest "big money date" read 마이너스통장 0원). An unknown amount stays: it is not zero.
            if today < due <= horizon and amount != 0:
                items.append({"date": due, "label": _liability_label(record), "amount": amount,
                              "kind": "cash_need", "category": f["category"]})
    for goal in view.summary.goals:
        if today < goal["target_date"] <= horizon:
            items.append({"date": goal["target_date"], "label": f"목표: {_goal_label(goal['category'])}",
                          "amount": goal["target"], "kind": "goal"})
    return sorted(items, key=lambda item: item["date"])


def _pay_rows(pay: list[future_income.Inflow], horizon: date) -> list[dict[str, Any]]:
    """Expected pay as timeline rows. The tranches of one payment (same day, category, certainty and owner,
    entered as separate records because each part becomes usable on its own date, data contract) are one row
    with the later usable dates: three rows of one day read as a duplicate (user review 2026-10-18). A sum
    with an unknown part is unknown."""
    rows: dict[tuple[date, str], dict[str, Any]] = {}
    for i in pay:
        if i.overdue or i.paid_on > horizon:
            continue
        label = (f"{PAY_LABELS.get(i.category, '예정 수입')}({CERTAINTY_LABELS[i.certainty]})"
                 + ("" if i.owner_id == "self" else "·배우자"))
        row = rows.setdefault((i.paid_on, label), {"date": i.paid_on, "label": label, "amount": Decimal(0), "gross": Decimal(0),
                                                   "kind": "inflow", "category": i.category, "count": 0, "usable": []})
        row["count"] += 1
        row["amount"] = None if row["amount"] is None or i.net is None else row["amount"] + i.net
        row["gross"] = None if row["gross"] is None or i.gross is None else row["gross"] + i.gross
        if i.usable_from != i.paid_on:
            row["usable"].append(i.usable_from)
    return list(rows.values())


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
    rows = [{"label": _liability_label(r), "category": r.fields["category"], "amount": base_amount(view, r),
             "rate": Decimal(r.fields["annual_rate"]), "rate_type": r.fields["rate_type"],
             "maturity": r.fields.get("maturity"), "owner": r.owner_id,
             "credit_limit": _dec(r.fields.get("credit_limit"))} for r in loans]
    # A credit line with nothing drawn costs nothing and is a liquidity backstop, not a debt to repay
    # (user review 2026-10-18): the unused room is reported apart. Unknown when a drawn amount is unknown.
    lines = [l for l in rows if l["credit_limit"] is not None]
    unused = _total(None if l["amount"] is None else max(Decimal(0), l["credit_limit"] - l["amount"]) for l in lines)
    return {"loans": rows, "variable_total": variable_total,
            "shocks": [{"shock": s, "monthly_increase": None if variable_total is None else variable_total * s / 12}
                       for s in RATE_SHOCKS],
            "monthly_interest_estimate": view.summary.monthly_loan_interest_estimate,
            "unused_credit": unused if lines else None,
            "credit_lines": [{"label": l["label"], "limit": l["credit_limit"], "drawn": l["amount"]} for l in lines]}


def _securities_sector(view: LedgerView) -> dict[str, Any]:
    facts = cases.portfolio_facts(view)
    labels = {view.aliases[r.record_id]: r.fields.get("instrument_name") or r.fields.get("symbol")
              for r in view.records if r.kind is Kind.ASSET}
    positions = [{"name": labels.get(p["ref"]) or p["symbol"], "value": Decimal(p["value"]),
                  "share": Decimal(p["share_of_securities"])} for p in facts["top_positions"]
                 if p["value"] is not None and p["share_of_securities"] is not None]
    gains = [unrealized_gain(view, r) for r in view.records if r.kind is Kind.ASSET and r.fields.get("symbol")]
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
        claims = [r for r in view.records if r.kind is Kind.LIABILITY
                  and r.fields.get("collateral_record_id") == record.record_id]
        # `debt` is every claim on the property; the loan-to-value ratio counts only loans secured on it.
        # A lease deposit owed to a tenant is a separate obligation (it is not a loan against the price).
        debt = _total(base_amount(view, r) for r in claims)
        loans = _total(base_amount(view, r) for r in claims if r.fields["category"] in SECURED_LOAN_CATEGORIES)
        deposits = _total(base_amount(view, r) for r in claims if r.fields["category"] == "lease_deposit_obligation")
        properties.append({"record_id": record.record_id, "label": _asset_label(record), "value": value, "debt": debt,
                           "loans": loans, "deposits": deposits,
                           "ltv": loans / value if value and loans is not None else None,
                           "valued_on": record.effective_at.date(), "owner": record.owner_id,
                           "occupancy": record.fields.get("occupancy"),
                           "valuation_method": record.fields["valuation_method"]})
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
