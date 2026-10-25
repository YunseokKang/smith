"""Composite household stress test (report design §16.2).

A top private banker asks first how the household survives one year in which the named assumptions below
happen together, not what a single rate rise costs. This module answers that with deterministic figures: in one
scenario (variable-rate loans reprice higher, the household's largest earned income stops for a year,
property prices fall, stocks fall), how many of the scenario's months the bills can still be paid from
cash alone and after selling stocks at the lower prices, how far net worth falls, how the mortgages'
loan-to-value moves, and whether the nearest lease deposit due could still be returned.

Every assumption is a named constant and is reported with the result. Unknown inputs (an amount without
an FX rate, an unknown cash flow) make the dependent figure None, never zero. The verdict has two parts:
the liquidity verdict (how long cash lasts) and obligation flags (a dated payment the scenario cannot
meet); the overall verdict is at least "watch" when a flag is raised.
"""
from datetime import date
from decimal import Decimal
from typing import Any

from smith import cases
from smith.holdings import STRESS_DRAWDOWN
from smith.payload import LedgerView, base_amount
from smith.records import Kind
from smith.summary import Summary, recurring_active

RATE_SHOCK = Decimal("0.015")          # Assumption: variable and mixed-rate loans reprice 1.5%p higher.
INCOME_STOP_MONTHS = 12                # Assumption: the household's largest earned income stops for a year.
PROPERTY_DROP = Decimal("0.20")        # Assumption: property values fall 20%.
STOCK_DROP = STRESS_DRAWDOWN           # Assumption shared with the glide plan: stocks and funds fall 30%.
ROBUST_MONTHS = Decimal(INCOME_STOP_MONTHS)  # Verdict (assumptions): cash alone covers the whole scenario, ...
WATCH_MONTHS = Decimal(6)              # ... or at least half of it.
LEASE_HORIZON_MONTHS = 24              # The nearest lease deposit due within this horizon is tested.
LEASE_UNCOVERED = "lease_deposit_uncovered"
# The scenario's market half has happened (user review 2026-10-18: "is it even possible for these to come
# together?"): in 2022 the policy rate, stocks and apartment prices moved against the household in one year. The
# income stop is household-specific (leave, illness; a low-likelihood extreme, client 2026-10-24). Figures checked against the sources on 2026-10-18.
# The apartment index is the vintage published in February 2023 (December 2021 -> December 2022); the board later
# revised the series and rebased it in 2026, and recent citations read -21.78% (review 2026-10-18).
PRECEDENT = {"year": 2022, "base_rate": ("1.00%", "3.25%"), "kospi": "-24.9%", "seoul_apartments": "-22.09%",
             "seoul_apartments_vintage": "2023년 2월 공표치", "national_apartments": "-16.84%"}
PRECEDENT_SOURCES = (
    ("한국은행 기준금리 추이", "https://www.bok.or.kr/portal/singl/baseRate/list.do?dataSeCd=01&menuNo=200643"),
    ("뉴시스 2022-12-29(코스피 연간 24.9% 하락)", "https://www.newsis.com/view/NISX20221229_0002141083"),
    ("경향신문 2023-02-16(한국부동산원 실거래가지수)", "https://www.khan.co.kr/article/202302160721001"),
)

_EARNED = ("salary", "business_income")
_SECURITIES = ("stock", "fund", "unclassified")
_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}


def test(view: LedgerView, kpis: dict[str, Any], sectors: dict[str, Any],
         lease_plans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The composite scenario applied to this household. All amounts are KRW Decimals or None.

    Keys: monthly (inflow, lost_income, outflow, extra_interest, burn: what the household loses every
    month under stress, negative when it still saves), months_cash and months_with_sales (capped at the
    scenario length), covers_scenario, cash_after_scenario and after_sales (what is left, or the gap, once
    the scenario's months are paid), sellable_stressed, pausable (discretionary outflows that could be
    stopped), net_worth_drop and net_worth_drop_ratio, properties (label, ltv now and stressed, on secured
    loans only), lease (amount, need, due, covered under stress), liquidity_verdict, flags, verdict (robust,
    watch, fragile, unknown) and assumptions.

    `lease_plans` (smith.lease.plans) gives each deposit's stress need: the reverse-jeonse gap once the
    market level has fallen with property prices, or the whole deposit when that level is unknown. Without
    plans (older callers) the whole deposit is tested.
    """
    summary = view.summary
    today = view.as_of.date()
    complete_flows = "cash_flow" not in summary.incomplete_areas
    inflow = summary.monthly_inflow if complete_flows else None
    outflow = summary.monthly_outflow if complete_flows else None
    lost, lost_owner = _largest_earned_income(view, today)
    variable = sectors["debt"]["variable_total"]
    extra_interest = None if variable is None else variable * RATE_SHOCK / 12
    burn = (outflow + extra_interest - (inflow - lost)
            if None not in (inflow, outflow, extra_interest, lost) else None)
    drained = None if burn is None else max(burn, Decimal(0)) * INCOME_STOP_MONTHS
    immediate = kpis["immediate"]
    sellable = _sellable_after_drop(view)
    months_cash = _months(immediate, burn)
    months_sales = _months(None if immediate is None or sellable is None else immediate + sellable, burn)
    cash_after = None if immediate is None or drained is None else immediate - drained
    after_sales = None if cash_after is None or sellable is None else cash_after + sellable
    drop = _net_worth_drop(view)
    lease = _lease(view, today, immediate, sellable, burn, lease_plans)
    liquidity = _verdict(months_cash, burn)
    flags = [LEASE_UNCOVERED] if lease is not None and lease["covered"] is False else []
    return {
        "monthly": {"inflow": inflow, "lost_income": lost, "lost_income_owner": lost_owner, "outflow": outflow,
                    "extra_interest": extra_interest, "burn": burn},
        "immediate": immediate, "sellable_stressed": sellable,
        "months_cash": months_cash, "months_with_sales": months_sales,
        "covers_scenario": _covers(months_cash, burn),
        "cash_after_scenario": cash_after, "after_sales": after_sales,
        "pausable": cases.monthly_outflow(view, lambda f: f.get("commitment") == "discretionary"),
        "net_worth_drop": drop, "net_worth_drop_ratio": _drop_ratio(drop, summary),
        "properties": [_property(p) for p in sectors["real_estate"]["properties"]],
        "lease": lease,
        "liquidity_verdict": liquidity, "flags": flags,
        "verdict": "watch" if liquidity == "robust" and flags else liquidity,
        "assumptions": {"rate_shock": RATE_SHOCK, "income_stop_months": INCOME_STOP_MONTHS,
                        "property_drop": PROPERTY_DROP, "stock_drop": STOCK_DROP,
                        "robust_months": ROBUST_MONTHS, "watch_months": WATCH_MONTHS},
    }


def _largest_earned_income(view: LedgerView, today: date) -> tuple[Decimal | None, str | None]:
    """(monthly amount, owner) of the owner whose recurring salary and business income is largest: the
    income that stops in the scenario. Unknown when any earned income could not be converted."""
    by_owner: dict[str, Decimal] = {}
    for record in view.records:
        f = record.fields
        if record.kind is not Kind.CASHFLOW or f["category"] not in _EARNED or f["frequency"] not in _MONTHS \
                or not recurring_active(f, today):
            continue
        amount = base_amount(view, record)
        if amount is None:
            return None, None
        by_owner[record.owner_id] = by_owner.get(record.owner_id, Decimal(0)) + amount / _MONTHS[f["frequency"]]
    if not by_owner:
        return Decimal(0), None
    owner = max(by_owner, key=lambda key: (by_owner[key], key == "self"))
    return by_owner[owner], owner


def _sellable_after_drop(view: LedgerView) -> Decimal | None:
    """Assets that turn into cash within days, with stocks and funds at the lower scenario price.
    Hermes-managed assets are left out (Smith never counts them as money to sell)."""
    total = Decimal(0)
    for record in view.records:
        f = record.fields
        if record.kind is not Kind.ASSET or f["liquidity"] != "days" or f.get("managed_by") == "hermes":
            continue
        value = base_amount(view, record)
        if value is None:
            return None
        total += value * (1 - STOCK_DROP) if f["category"] in _SECURITIES else value
    return total


def _months(money: Decimal | None, burn: Decimal | None) -> Decimal | None:
    """How many of the scenario's months `money` pays for `burn`, capped at the scenario length (income
    returns afterwards, so a longer figure would describe a different scenario). None when unknown or
    when the household still saves under stress (burn <= 0): the verdict says so."""
    if money is None or burn is None or burn <= 0:
        return None
    return min(money / burn, Decimal(INCOME_STOP_MONTHS))


def _covers(months_cash: Decimal | None, burn: Decimal | None) -> bool | None:
    """Whether cash alone pays every month of the scenario; None when unknown."""
    if burn is None:
        return None
    if burn <= 0:
        return True
    return None if months_cash is None else months_cash >= ROBUST_MONTHS


def _property(prop: dict[str, Any]) -> dict[str, Any]:
    """Loan-to-value on the loans secured on the property (report_data), now and after the price drop. A
    lease deposit owed on the property is a separate obligation, not part of the ratio."""
    value, loans = prop["value"], prop["loans"]
    stressed = None if value is None or loans is None or not value else loans / (value * (1 - PROPERTY_DROP))
    return {"label": prop["label"], "ltv": prop["ltv"], "ltv_stressed": stressed, "deposits": prop["deposits"]}


def _net_worth_drop(view: LedgerView) -> Decimal | None:
    """Property times its drop plus every stock and fund (Hermes included: it is still the household's
    wealth) times its drop. Unknown when any such asset could not be converted."""
    total = Decimal(0)
    for record in view.records:
        f = record.fields
        if record.kind is not Kind.ASSET or f["category"] not in _SECURITIES + ("real_estate",):
            continue
        value = base_amount(view, record)
        if value is None:
            return None
        total += value * (PROPERTY_DROP if f["category"] == "real_estate" else STOCK_DROP)
    return total


def _drop_ratio(drop: Decimal | None, summary: Summary) -> Decimal | None:
    """The fall as a share of net worth: only on a complete balance sheet with positive net worth (a
    partial or non-positive denominator gives a figure that means nothing)."""
    if drop is None or "balance_sheet" in summary.incomplete_areas or summary.net_worth is None \
            or summary.net_worth <= 0:
        return None
    return drop / summary.net_worth


def _lease(view: LedgerView, today: date, immediate: Decimal | None, sellable: Decimal | None,
           burn: Decimal | None, plans: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The nearest lease deposit due within the horizon (a deposit due later this month counts as one
    month away): can cash plus stressed sales, after the months of stress until the due date (at most the
    income-stop months), still find what the deposit needs? With a lease plan the need is the reverse-jeonse
    gap after the price fall (a new tenant's deposit replaces most of the old one); without one, or when the
    market level is unknown, it is the whole deposit."""
    leases = []
    for record in view.records:
        f = record.fields
        if record.kind is Kind.LIABILITY and f["category"] == "lease_deposit_obligation" and f.get("maturity"):
            due = date.fromisoformat(f["maturity"])
            months = max(1, (due.year - today.year) * 12 + due.month - today.month)
            if today < due and months <= LEASE_HORIZON_MONTHS:
                leases.append((due, months, record.record_id, base_amount(view, record)))
    if not leases:
        return None
    due, months, record_id, amount = min(leases)
    plan = next((p for p in plans or [] if p["record_id"] == record_id), None)
    need, basis = (amount, "full_deposit") if plan is None else (plan["stress"]["need"], plan["stress"]["basis"])
    found = {"due": due, "months": months, "amount": amount, "need": need, "basis": basis,
             "jeonse_dropped": None if plan is None else plan["stress"]["jeonse_dropped"],
             "from_deals": False if plan is None else plan["stress"].get("from_deals", False)}
    if need is None or immediate is None or sellable is None or burn is None:
        return {**found, "available": None, "covered": None}
    drained = max(burn, Decimal(0)) * min(months, INCOME_STOP_MONTHS)
    available = max(Decimal(0), immediate + sellable - drained)
    return {**found, "available": available, "covered": available >= need}


def _verdict(months_cash: Decimal | None, burn: Decimal | None) -> str:
    """The liquidity verdict. Unknown cash or an unknown shortfall is "unknown", never a confident verdict
    either way."""
    if burn is None:
        return "unknown"
    if burn <= 0:
        return "robust"
    if months_cash is None:
        return "unknown"
    if months_cash >= ROBUST_MONTHS:
        return "robust"
    return "watch" if months_cash >= WATCH_MONTHS else "fragile"


def uses_deals(st: dict[str, Any]) -> bool:
    """The lease result rests on the official deals: the reverse-jeonse gap (jeonse level) or a sale price
    taken from recent trades (not the ledger value)."""
    return bool((st.get("lease") or {}).get("from_deals"))
