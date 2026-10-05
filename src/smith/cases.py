"""Deterministic calculations for the three advice use cases (requirements UC-01..03).

Every amount the adviser discusses is computed here. Assumptions (growth rates, costs, tax rates)
are explicit inputs echoed in the output so the model and the user can see and question them.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from smith.payload import LedgerView, base_amount, owner_alias, won
from smith.records import INFLOW_CATEGORIES, OUTFLOW_CATEGORIES, TRANSFER_CATEGORIES, Kind
from smith.summary import recurring_active

_SECURITIES = ("stock", "fund", "bond", "unclassified")
_RESTRICTED_ACCOUNTS = ("pension_savings", "irp", "dc")


def portfolio_facts(view: LedgerView) -> dict[str, Any]:
    """UC-01: allocation, concentration, liquidity runway and leverage."""
    summary = view.summary
    securities = [r for r in view.records if r.kind is Kind.ASSET and r.fields["category"] in _SECURITIES]
    securities_total = _sum_values(view, securities)
    positions = sorted(((r, base_amount(view, r)) for r in securities if r.fields.get("symbol")),
                       key=lambda item: (item[1] is None, -(item[1] or Decimal(0))))
    immediate_assets = [r for r in view.records if r.kind is Kind.ASSET and r.fields["liquidity"] == "immediate"]
    immediate = _sum_values(view, immediate_assets)
    variable_loans = [r for r in view.records if r.kind is Kind.LIABILITY
                      and r.fields["rate_type"] in ("variable", "mixed")]
    variable_debt = _sum_values(view, variable_loans)
    balance_complete = "balance_sheet" not in summary.incomplete_areas
    cash_flow_complete = "cash_flow" not in summary.incomplete_areas
    return {
        "use_case": "UC-01 keep or change the portfolio and cash flow",
        "securities_total": won(securities_total),
        "top_positions": [{"ref": view.aliases[r.record_id], "symbol": r.fields["symbol"], "market": r.fields.get("market"),
                           "value": won(value), "share_of_securities": _ratio(value, securities_total)}
                          for r, value in positions[:10]],
        "unclassified_securities": won(_sum_values(
            view, [r for r in securities if r.fields["category"] == "unclassified"])),
        "usd_share_of_assets": (_ratio(summary.by_currency.get("USD", Decimal(0)), summary.total_assets)
                                if balance_complete else None),
        "immediate_liquidity": won(immediate),
        "months_of_outflow_covered_by_immediate_liquidity": (
            _ratio(immediate, summary.monthly_outflow, places=1) if cash_flow_complete else None),
        "liabilities_to_assets": (_ratio(summary.total_liabilities, summary.total_assets)
                                   if balance_complete else None),
        "variable_rate_debt": won(variable_debt),
        "monthly_net_cash_flow": won(summary.monthly_net if cash_flow_complete else None),
        "monthly_insurance_premiums": won(_monthly_outflow(view, lambda f: f["category"] == "insurance_premium")),
        "monthly_discretionary_outflow": won(_monthly_outflow(view, lambda f: f.get("commitment") == "discretionary")),
        "unknown_amount_refs": _unknown_amount_refs(view),
    }


def funding_facts(view: LedgerView, *, amount: Decimal, us_tax_rate: Decimal = Decimal("0.22"),
                  us_tax_allowance: Decimal = Decimal(2_500_000)) -> dict[str, Any]:
    """UC-02: what can be raised, how fast, and at what known cost, against a target amount."""
    _require_positive_finite(amount, "amount")
    if not us_tax_rate.is_finite() or not Decimal(0) <= us_tax_rate <= Decimal(1):
        raise ValueError("us_tax_rate must be a finite fraction from 0 to 1")
    if not us_tax_allowance.is_finite() or us_tax_allowance < 0:
        raise ValueError("us_tax_allowance must be a finite non-negative amount")
    sources = []
    for record in view.records:
        if record.kind is not Kind.ASSET:
            continue
        value = base_amount(view, record)
        f = record.fields
        restricted = f["liquidity"] == "restricted" or f.get("account_type") in _RESTRICTED_ACCOUNTS
        sources.append({"ref": view.aliases[record.record_id], "category": f["category"],
                        "account_type": f.get("account_type"), "owner": owner_alias(view, record.owner_id),
                        "liquidity": f["liquidity"],
                        "value": won(value), "unrealized_gain": won(_unrealized_gain(view, record)),
                        "restricted": restricted})
    by_class: dict[str, list[Any]] = {}
    # Hermes-managed assets are not counted as reachable money; they appear only as their own tier.
    for record in (r for r in view.records if r.kind is Kind.ASSET and r.owner_id == "self" and not _hermes(r)):
        by_class.setdefault(record.fields["liquidity"], []).append(record)
    # Overseas stock gains are taxed on the year's net result, so losses offset gains.
    us_records = [r for r in view.records if r.kind is Kind.ASSET and r.owner_id == "self"
                  and r.fields.get("market") == "US"]
    us_gain = _sum_unrealized_gains(view, us_records)
    discretionary = _monthly_outflow(view, lambda f: f.get("commitment") == "discretionary")
    cumulative: Decimal | None = Decimal(0)
    reachable = {}
    for liquidity in ("immediate", "days"):
        class_total = _sum_values(view, by_class.get(liquidity, []))
        cumulative = cumulative + class_total if cumulative is not None and class_total is not None else None
        reachable[liquidity] = {"cumulative": won(cumulative),
                                "covers_target": None if cumulative is None else cumulative >= amount}
    return {
        "use_case": "UC-02 raise a sum urgently",
        "target_amount": won(amount),
        "owner_scope": "self-owned assets only; partner-owned assets are listed but not counted as available",
        "reachable_by_liquidity": reachable,
        "funding_tiers": _funding_tiers(view, amount, us_tax_rate, us_tax_allowance),
        "restricted_total_self": won(_sum_values(view, by_class.get("restricted", []))),
        "illiquid_months_total_self": won(_sum_values(view, by_class.get("months", []))),
        "sources": sources,
        "pausable_monthly_outflow": won(discretionary),
        "us_stock_net_unrealized_gain": won(us_gain),
        "us_stock_tax_estimate_if_all_sold": won(
            None if us_gain is None else max(Decimal(0), us_gain - us_tax_allowance) * us_tax_rate),
        "us_stock_cost_basis_complete": us_gain is not None,
        "unknown_amount_refs": _unknown_amount_refs(view),
        "assumptions": {"us_capital_gains_tax_rate": str(us_tax_rate),
                        "us_capital_gains_annual_allowance": won(us_tax_allowance),
                        "korean_listed_stock_gains": "not taxed for non-major shareholders; transaction tax ignored",
                        "borrowing": "loan availability and rates are unknown; any loan option is an assumption",
                        "note": "tax rules must be verified against current law before acting"},
    }


# Order in which self-owned assets are usually tapped: cheapest and fastest first. Each tier is
# (name, predicate over fields); the first matching tier claims an asset.
_TIERS = (
    ("cash", lambda f: f["category"] in ("cash", "deposit") and f["liquidity"] == "immediate"),
    ("restricted", lambda f: f["liquidity"] == "restricted" or f.get("account_type") in _RESTRICTED_ACCOUNTS),
    ("hermes_managed", lambda f: f.get("managed_by") == "hermes"),
    ("kr_listed_stocks", lambda f: f["category"] == "stock" and f.get("market") == "KR"),
    ("unclassified_securities", lambda f: f["category"] in _SECURITIES and f.get("market") != "US"),
    ("us_listed_stocks", lambda f: f.get("market") == "US"),
    ("illiquid", lambda f: f["liquidity"] == "months"),
    ("other", lambda f: True),
)
_TIER_ORDER = ("cash", "kr_listed_stocks", "unclassified_securities", "us_listed_stocks", "hermes_managed",
               "restricted", "illiquid", "other")


def _funding_tiers(view: LedgerView, amount: Decimal, us_tax_rate: Decimal,
                   us_tax_allowance: Decimal) -> list[dict[str, Any]]:
    """Self-owned assets grouped into tiers in tapping order, with running totals computed here so the
    adviser never adds amounts itself. Hermes-managed and restricted tiers need coordination or carry
    penalties; illiquid assets take months."""
    groups: dict[str, list] = {name: [] for name, _ in _TIERS}
    for record in view.records:
        if record.kind is Kind.ASSET and record.owner_id == "self":
            name = next(name for name, accepts in _TIERS if accepts(record.fields))
            groups[name].append(record)
    tiers: list[dict[str, Any]] = []
    cumulative: Decimal | None = Decimal(0)
    for name in _TIER_ORDER:
        records = sorted(groups[name], key=lambda record: view.aliases[record.record_id])
        if not records:
            continue
        total = _sum_values(view, records)
        gain = _sum_unrealized_gains(view, records)
        tax = (max(Decimal(0), gain - us_tax_allowance) * us_tax_rate
               if name == "us_listed_stocks" and gain is not None else None)
        available = total
        if name == "us_listed_stocks":
            available = max(Decimal(0), total - tax) if total is not None and tax is not None else None
        cumulative = cumulative + available if cumulative is not None and available is not None else None
        tiers.append({"tier": name, "refs": [view.aliases[r.record_id] for r in records], "amount": won(total),
                      "unrealized_gain_known": won(gain), "unrealized_gain_complete": gain is not None,
                      "estimated_tax": won(tax), "available_after_estimated_tax": won(available),
                      "cumulative": won(cumulative),
                      "covers_target": None if cumulative is None else cumulative >= amount,
                      "shortfall_after": won(None if cumulative is None else max(Decimal(0), amount - cumulative))})
    return tiers


@dataclass(frozen=True)
class Scenario:
    name: str
    target_price_growth: Decimal   # Annual.
    current_home_growth: Decimal   # Annual.
    investment_return: Decimal     # Annual, on liquid securities and cash.


SCENARIOS = (
    Scenario("base", Decimal("0.02"), Decimal("0.02"), Decimal("0.04")),
    Scenario("favorable", Decimal("0"), Decimal("0.04"), Decimal("0.08")),
    Scenario("adverse", Decimal("0.05"), Decimal("-0.05"), Decimal("-0.10")),
)


def home_facts(view: LedgerView, *, target_price: Decimal, target_date: date,
               buy_cost_rate: Decimal = Decimal("0.035"), sell_cost_rate: Decimal = Decimal("0.005")) -> dict[str, Any]:
    """UC-03: equity available at the target date under three scenarios versus the target cost."""
    _require_positive_finite(target_price, "target_price")
    for name, rate in (("buy_cost_rate", buy_cost_rate), ("sell_cost_rate", sell_cost_rate)):
        if not rate.is_finite() or not Decimal(0) <= rate <= Decimal(1):
            raise ValueError(f"{name} must be a finite fraction from 0 to 1")
    today = view.as_of.date()
    if target_date <= today:
        raise ValueError("target_date must be after the snapshot date")
    months = (target_date.year - today.year) * 12 + target_date.month - today.month
    years = Decimal(months) / 12
    homes = [r for r in view.records if r.kind is Kind.ASSET and r.fields.get("occupancy") == "owner_occupied"
             and r.owner_id == "self"]
    home_value = _sum_values(view, homes)
    home_ids = {r.record_id for r in homes}
    mortgage = [r for r in view.records if r.kind is Kind.LIABILITY and r.fields.get("collateral_record_id") in home_ids]
    mortgage_balance = _sum_values(view, mortgage)
    mortgage_ids = {r.record_id for r in mortgage}
    mortgage_payments = _monthly_outflow(
        view, lambda f: f["category"] == "loan_payment" and f.get("liability_record_id") in mortgage_ids)
    mortgage_interest = _monthly_interest(view, mortgage)
    principal_monthly = (max(Decimal(0), mortgage_payments - mortgage_interest)
                         if mortgage_payments is not None and mortgage_interest is not None else None)
    liquid_records = [r for r in view.records if r.kind is Kind.ASSET and r.owner_id == "self"
                      and r.fields["liquidity"] in ("immediate", "days") and not _hermes(r)]
    liquid = _sum_values(view, liquid_records)
    monthly_net = _monthly_net_cash_flow(view, owner_id="self")
    savings = monthly_net * months if monthly_net is not None else None
    partner_equity = _partner_property_equity(view)
    scenarios = []
    for s in SCENARIOS:
        required = target_price * _compound(s.target_price_growth, years) * (1 + buy_cost_rate)
        inputs = (home_value, mortgage_balance, principal_monthly, liquid, savings)
        available = None
        if all(value is not None for value in inputs):
            mortgage_left = max(Decimal(0), mortgage_balance - principal_monthly * months)
            available = (home_value * _compound(s.current_home_growth, years) * (1 - sell_cost_rate)
                         - mortgage_left + liquid * _compound(s.investment_return, years) + savings)
        scenarios.append({"name": s.name, "assumptions": {"target_price_growth": str(s.target_price_growth),
                                                          "current_home_growth": str(s.current_home_growth),
                                                          "investment_return": str(s.investment_return)},
                          "required_with_costs": won(required), "available_self": won(available),
                          "gap_self": won(None if available is None else required - available),
                          "gap_including_partner_property_equity": won(
                              None if available is None or partner_equity is None
                              else required - available - partner_equity)})
    return {
        "use_case": "UC-03 move to a target home",
        "target_price_today": won(target_price), "target_date": target_date.isoformat(), "months": months,
        "current_home_value": won(home_value), "mortgage_balance": won(mortgage_balance),
        "estimated_monthly_principal_repayment": won(principal_monthly),
        "liquid_assets_self": won(liquid), "projected_savings_from_monthly_net": won(savings),
        "monthly_net_cash_flow_self": won(monthly_net),
        "partner_property_equity": won(partner_equity),
        "excluded": "restricted pension and housing-subscription balances and Hermes-managed assets are not counted",
        "assumptions": {"buy_cost_rate": str(buy_cost_rate), "sell_cost_rate": str(sell_cost_rate),
                        "savings": "current monthly net cash flow continues unchanged",
                        "cash_flow_scope": "self-owned recurring cash flows only; future and ended flows are excluded",
                        "principal_repayment": "current linked mortgage payments and current interest rates continue",
                        "time_horizon": "whole calendar months from the snapshot date",
                        "financing": "loan limits (LTV/DSR) are not modelled; a positive gap must be financed",
                        "partner_property": "current net equity is shown separately without future growth or sale costs",
                        "ownership": "partner-owned property is shown separately; marriage registration may change "
                                     "household home-count tax treatment"},
        "unknown_amount_refs": _unknown_amount_refs(view),
        "scenarios": scenarios,
    }


_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}


def _monthly_outflow(view: LedgerView, accept: Any) -> Decimal | None:
    """Recurring outflows matching `accept`, as a monthly amount (one-time flows excluded)."""
    records = [r for r in view.records if r.kind is Kind.CASHFLOW and r.fields["direction"] == "outflow"
               and _is_active_recurring(view, r) and accept(r.fields)]
    return _monthly_total(view, records)


def _monthly_net_cash_flow(view: LedgerView, *, owner_id: str) -> Decimal | None:
    """Monthly income minus spending, less transfers into restricted accounts (pension contributions):
    that money cannot be used for a home or a deposit return."""
    restricted = {r.record_id for r in view.records if r.kind is Kind.ASSET and r.fields["liquidity"] == "restricted"}
    records = [r for r in view.records if r.kind is Kind.CASHFLOW and r.owner_id == owner_id
               and _is_active_recurring(view, r)]
    values = []
    for record in records:
        category = record.fields["category"]
        if category in TRANSFER_CATEGORIES and record.fields.get("target_record_id") not in restricted:
            continue
        amount = base_amount(view, record)
        if amount is None:
            return None
        monthly = amount / _MONTHS[record.fields["frequency"]]
        if category in INFLOW_CATEGORIES:
            values.append(monthly)
        elif category in OUTFLOW_CATEGORIES or category in TRANSFER_CATEGORIES:
            values.append(-monthly)
    return sum(values, Decimal(0))


def _monthly_total(view: LedgerView, records: list[Any]) -> Decimal | None:
    values = []
    for record in records:
        amount = base_amount(view, record)
        if amount is None:
            return None
        values.append(amount / _MONTHS[record.fields["frequency"]])
    return sum(values, Decimal(0))


def _is_active_recurring(view: LedgerView, record: Any) -> bool:
    return recurring_active(record.fields, view.as_of.date())


def _compound(rate: Decimal, years: Decimal) -> Decimal:
    # Decimal supports fractional exponents for a positive base, so no binary float is involved.
    return (1 + rate) ** years


def _unrealized_gain(view: LedgerView, record: Any) -> Decimal | None:
    f = record.fields
    if any(f.get(field) is None for field in ("quantity", "unit_price", "average_cost")) \
            or f["currency"] not in view.rates:
        return None
    return (Decimal(f["unit_price"]) - Decimal(f["average_cost"])) * Decimal(f["quantity"]) * view.rates[f["currency"]]


def _sum_unrealized_gains(view: LedgerView, records: list[Any]) -> Decimal | None:
    gains = [_unrealized_gain(view, record) for record in records]
    return None if any(gain is None for gain in gains) else sum(gains, Decimal(0))


def _monthly_interest(view: LedgerView, liabilities: list[Any]) -> Decimal | None:
    values = []
    for liability in liabilities:
        principal = base_amount(view, liability)
        if principal is None:
            return None
        values.append(principal * Decimal(liability.fields["annual_rate"]) / 12)
    return sum(values, Decimal(0))


def _partner_property_equity(view: LedgerView) -> Decimal | None:
    partner_assets = [r for r in view.records if r.kind is Kind.ASSET and r.owner_id != "self"
                      and r.fields["category"] == "real_estate"]
    ids = {r.record_id for r in partner_assets}
    liabilities = [r for r in view.records if r.kind is Kind.LIABILITY
                   and r.fields.get("collateral_record_id") in ids]
    assets, owed = _sum_values(view, partner_assets), _sum_values(view, liabilities)
    return assets - owed if assets is not None and owed is not None else None


def _sum_values(view: LedgerView, records: list[Any]) -> Decimal | None:
    values = [base_amount(view, record) for record in records]
    return None if any(value is None for value in values) else sum(values, Decimal(0))


def _hermes(record: Any) -> bool:
    """Managed by Hermes: never proposed for sale, so not counted as money the client can draw on."""
    return record.fields.get("managed_by") == "hermes"


def _unknown_amount_refs(view: LedgerView) -> list[str]:
    return [view.aliases[record.record_id] for record in view.records if base_amount(view, record) is None]


def _require_positive_finite(value: Decimal, name: str) -> None:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite amount")


def _ratio(numerator: Decimal | None, denominator: Decimal | None, places: int = 4) -> str | None:
    if numerator is None or not denominator:
        return None
    return str((numerator / denominator).quantize(Decimal(1).scaleb(-places)))
