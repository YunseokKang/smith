"""Lease deposit return: the cash the household must actually find, by scenario (report design §17.2).

A deposit owed to a tenant is not a loan the household repays from its own pocket. When the lease ends
the usual outcome is a new tenant whose deposit replaces the old one; the household's own money is only
the gap when the market level has fallen (역전세), plus a safety buffer. Selling the home with the tenant in
place or at maturity turns the deposit into a deduction from the sale price, and a move-out loan can bridge
the rest within the debt-service limit. The old rule treated the whole deposit as cash out and judged a
sound household "fragile" on that basis (user review 2026-10-17).

Every figure here is deterministic, every assumption is a named constant echoed in the result, and an
unknown input (no recent jeonse contracts, no FX rate, no acquisition data, no income on record for the
owner) leaves that scenario's figure None, never zero. The re-let scenario is the default; the full deposit
remains the conservative figure when the market level is unknown.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from smith import tax
from smith.payload import LedgerView, base_amount
from smith.realestate import MIN_BASIS
from smith.records import Kind
from smith.summary import recurring_active

# Assumption: a new tenant's deposit may come in below the recent median, or after the old tenant leaves.
RELET_BUFFER_RATE = Decimal("0.05")
# Assumption: the bank debt-service-ratio cap for a move-out loan (현행 법령 확인 필요), and the loan terms
# used to turn yearly debt-service room into a loan amount. Eligibility is never certain.
DSR_LIMIT = Decimal("0.40")
LOAN_RATE = Decimal("0.045")
LOAN_YEARS = 30
# Assumption: homes acquired in a regulated area need two years of residence for the one-home exemption.
RESIDENCE_YEARS_FOR_EXEMPTION = 2   # Years held and lived in for the one-home exemption (regulated-area purchases).
TAX_CASES = ("exempt", "general")
HORIZON_MONTHS = 48
_EARNED = ("salary", "business_income", "rental_income")
_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}
_SECURED = ("mortgage",)


@dataclass(frozen=True)
class Scenario:
    """One way to return the deposit: the household's own cash out (negative: cash in), and what it rests on."""
    key: str           # relet / sale / loan
    label: str
    cash_out: Decimal | None   # Own money needed; negative when the scenario brings cash in.
    basis: str                 # What the figure rests on, in Korean, for the report.
    known: bool                # False when an input is unknown and the figure is None.


def plans(view: LedgerView, properties: list[dict[str, Any]], household: dict[str, Any] | None, *,
          today: date, property_drop: Decimal) -> list[dict[str, Any]]:
    """Every lease deposit due within the horizon, nearest first, each with its scenarios.

    `properties` is report_data's real-estate sector list (ledger value, secured loans, official market
    figures per property); `household` the local profile (acquisition year and price per property).
    `property_drop` is the stress scenario's price fall, applied to the jeonse level for the stress need.
    """
    by_id = {p["record_id"]: p for p in properties}
    config = (household or {}).get("properties") or {}
    found = []
    for record in view.records:
        f = record.fields
        if record.kind is not Kind.LIABILITY or f["category"] != "lease_deposit_obligation" or not f.get("maturity"):
            continue
        due = date.fromisoformat(f["maturity"])
        months = max(1, (due.year - today.year) * 12 + due.month - today.month)  # Due later this month: one month.
        if not today < due or months > HORIZON_MONTHS:
            continue
        prop = by_id.get(f.get("collateral_record_id"))
        found.append(_plan(view, record, prop, config.get(f.get("collateral_record_id")) or {}, due, months,
                           today, property_drop))
    return sorted(found, key=lambda plan: plan["due"])


def _plan(view: LedgerView, record: Any, prop: dict[str, Any] | None, acquisition: dict[str, Any], due: date,
          months: int, today: date, drop: Decimal) -> dict[str, Any]:
    amount = base_amount(view, record)
    market = (prop or {}).get("market") or {}
    jeonse, basis = market.get("jeonse"), market.get("jeonse_basis") or 0
    jeonse_known = jeonse is not None and basis >= MIN_BASIS
    relet = _relet(amount, jeonse if jeonse_known else None)
    sale = _sale(amount, prop, acquisition, record.owner_id, today)
    loan = _loan(view, record.owner_id, today)
    # The client's plan (`lease_exit = "sell"` in the local config, e-mail 2026-10-19): the home is sold when the
    # lease ends, so the sale returns the deposit and only a shortfall of the sale would be the household's own.
    # The plan holds even when the sale cannot be priced: the need is then unknown, never the re-let figure.
    planned_sale = acquisition.get("lease_exit") == "sell"
    stress = (_stress_sale(amount, sale, drop, acquisition, record.owner_id, (prop or {}).get("occupancy"), today)
              if planned_sale else _stress_need(amount, jeonse if jeonse_known else None, drop))
    return {"record_id": record.record_id, "owner": record.owner_id, "amount": amount, "due": due, "months": months,
            "jeonse": jeonse, "jeonse_basis": basis, "jeonse_known": jeonse_known,
            "relet": relet, "sale": sale, "loan": loan, "stress": stress,
            # The figure the plans use: the planned sale's shortfall (none when it brings money in), else the re-let
            # need when the market level is known, else the whole deposit.
            "default": "sale" if planned_sale else "relet" if relet["need"] is not None else "full",
            "need": ((None if sale["cash_out"] is None else max(Decimal(0), sale["cash_out"])) if planned_sale else
                     relet["need"] if relet["need"] is not None else amount),
            "scenarios": _scenarios(relet, sale, loan, planned_sale),
            "assumptions": {"relet_buffer_rate": RELET_BUFFER_RATE, "dsr_limit": DSR_LIMIT, "loan_rate": LOAN_RATE,
                            "loan_years": LOAN_YEARS, "property_drop": drop}}


def _scenarios(relet: dict[str, Any], sale: dict[str, Any], loan: dict[str, Any], planned_sale: bool) -> list[Scenario]:
    """The ways to return the deposit, the default first."""
    relet_row = Scenario("relet", "새 세입자 승계" + ("" if planned_sale else "(기본)"), relet["need"],
                         "최근 6개월 같은 단지·면적 전세 중간값 + 안전 버퍼" if relet["need"] is not None
                         else "전세 실거래 3건 미만: 보증금 전액을 보수적으로 둠", relet["need"] is not None)
    sale_row = Scenario("sale", "만기에 맞춰 매도(계획)" if planned_sale else "전세 끼고 매도 또는 만기 맞춰 매도",
                        sale["cash_out"], sale["basis"], sale["cash_out"] is not None)
    loan_row = Scenario("loan", "퇴거자금대출", loan["cash_out"], loan["basis"], loan["amount"] is not None)
    return [sale_row, relet_row, loan_row] if planned_sale else [relet_row, sale_row, loan_row]


def _relet(amount: Decimal | None, jeonse: Decimal | None) -> dict[str, Any]:
    """Own money when a new tenant replaces the old one: the reverse-jeonse gap plus the buffer."""
    if amount is None or jeonse is None:
        return {"gap": None, "buffer": None, "need": None}
    gap = max(Decimal(0), amount - jeonse)
    buffer = amount * RELET_BUFFER_RATE
    return {"gap": gap, "buffer": buffer, "need": gap + buffer}


def _sale(amount: Decimal | None, prop: dict[str, Any] | None, acquisition: dict[str, Any], owner: str,
          today: date) -> dict[str, Any]:
    """Selling the home: the price (recent trade median, else the ledger value) less the deposit, the loans
    secured on it and the estimated capital-gains tax (two variants when the acquisition is known). The
    cash_out is negative: the sale brings money in. Sale costs are not modelled (assumption)."""
    if prop is None or amount is None:
        return {"price": None, "price_basis": None, "loans": None, "tax": None, "net_inflow": None, "cash_out": None,
                "basis": "담보 주택을 찾지 못해 계산하지 않음"}
    market = prop.get("market") or {}
    estimate, trades = market.get("estimate"), market.get("estimate_basis") or 0
    if estimate is not None and trades >= MIN_BASIS:
        price, price_basis = estimate, f"최근 6개월 실거래 {trades}건 중간값"
    else:
        price, price_basis = prop.get("value"), "원장 평가액(실거래 3건 미만)"
    loans = prop.get("loans")
    if price is None or loans is None:
        return {"price": price, "price_basis": price_basis, "loans": loans, "tax": None, "net_inflow": None,
                "cash_out": None, "basis": "평가액이나 담보대출을 원화로 환산하지 못함"}
    taxes = _capital_gains(price, acquisition, owner, prop.get("occupancy"), today)
    before_tax = price - amount - loans
    net = {key: before_tax - taxes[key]["total"] for key in TAX_CASES} if taxes else None
    # The headline figure is the exemption case when its requirements are known to be met (client e-mail
    # 2026-10-21), else the general-taxation case (the smaller inflow); the other case is shown beside it.
    case = None if taxes is None else taxes["headline"]
    inflow = before_tax if net is None else net[case]
    basis = f"{price_basis} − 보증금 − 담보대출" + (
        " − 양도세(1세대 1주택 비과세 기준: 2년 보유·거주 확인, 12억 원 초과분만 과세)" if case == "exempt" else
        " − 양도세(일반 과세 기준, 비과세 요건 충족 시 별도)" if taxes else " (취득가를 몰라 양도세는 빼지 않음)")
    return {"price": price, "price_basis": price_basis, "loans": loans, "tax": taxes, "tax_case": case, "net_inflow": net,
            "before_tax": before_tax, "cash_out": -inflow, "basis": basis}


def _capital_gains(price: Decimal, acquisition: dict[str, Any], owner: str, occupancy: str | None,
                   today: date) -> dict[str, Any] | None:
    """The one-home exemption case and the general case (TAX_CASES), as tax.notes estimates them, and which one
    leads (`headline`): the exemption when the home is known to have been held and lived in for two years (the
    client's own home, or a `residence_years` entry in the local config; client e-mail 2026-10-21), else the
    general case. None without the acquisition year and price."""
    year, acquired = acquisition.get("acquired_year"), acquisition.get("acquired_price")
    if not year or not acquired:
        return None
    held = max(0, today.year - int(year) - 1)  # Only the year is known: count full years conservatively.
    residence = acquisition.get("residence_years")
    if owner == "self" and occupancy == "owner_occupied":
        lived, residence_known = held, True
    elif residence is not None:
        lived, residence_known = int(residence), True
    else:
        lived, residence_known = RESIDENCE_YEARS_FOR_EXEMPTION, False
    exempt_known = residence_known and min(held, lived) >= RESIDENCE_YEARS_FOR_EXEMPTION
    return {"exempt": tax.capital_gains(price, Decimal(acquired), held=held, lived=lived, one_home=True),
            "general": tax.capital_gains(price, Decimal(acquired), held=held, lived=0, one_home=False),
            "headline": "exempt" if exempt_known else "general", "residence_known": residence_known, "held": held,
            "lived": lived}


def _loan(view: LedgerView, owner: str, today: date) -> dict[str, Any]:
    """A move-out loan within the debt-service cap: the owner's yearly earned income times DSR_LIMIT, less the
    owner's current yearly loan payments, turned into a loan amount at the assumed rate and term. Unknown
    when the owner has no earned income on record (the ledger often lacks a partner's income)."""
    income = debt_service = Decimal(0)
    seen_income = False
    for record in view.records:
        f = record.fields
        if record.kind is not Kind.CASHFLOW or record.owner_id != owner or f["frequency"] not in _MONTHS \
                or not recurring_active(f, today):
            continue
        amount = base_amount(view, record)
        if f["category"] in _EARNED:
            if amount is None:
                return _no_loan("소득을 원화로 환산하지 못함")
            income += amount / _MONTHS[f["frequency"]] * 12
            seen_income = True
        elif f["category"] == "loan_payment":
            if amount is None:
                return _no_loan("대출 상환액을 원화로 환산하지 못함")
            debt_service += amount / _MONTHS[f["frequency"]] * 12
    if not seen_income:
        return _no_loan("이 명의의 소득 기록이 없어 대출 한도를 계산하지 않음")
    room = max(Decimal(0), income * DSR_LIMIT - debt_service)
    amount = room / _yearly_payment_per_won(LOAN_RATE, LOAN_YEARS)
    return {"income_annual": income, "debt_service_annual": debt_service, "room_annual": room, "amount": amount,
            "cash_out": Decimal(0), "basis": f"DSR {DSR_LIMIT * 100:.0f}% 가정, 연 {LOAN_RATE * 100:.1f}% {LOAN_YEARS}년 "
                                           "원리금균등 가정(확정 승인 아님, 현행 규정 확인 필요)"}


def _no_loan(reason: str) -> dict[str, Any]:
    return {"income_annual": None, "debt_service_annual": None, "room_annual": None, "amount": None, "cash_out": None,
            "basis": reason}


def _yearly_payment_per_won(rate: Decimal, years: int) -> Decimal:
    """Yearly payment on a one-won amortizing loan (monthly payments)."""
    monthly = rate / 12
    months = years * 12
    return monthly / (1 - (1 + monthly) ** -months) * 12


def _stress_sale(amount: Decimal | None, sale: dict[str, Any], drop: Decimal, acquisition: dict[str, Any], owner: str,
                 occupancy: str | None, today: date) -> dict[str, Any]:
    """What the stress scenario must still find when the home is sold at the lease's end: the deposit and the
    secured loans less the price fallen by `drop` and the capital-gains tax at that lower price (general case).
    Unknown without the acquisition year and price: the tax cannot be estimated (review 2026-10-20)."""
    if amount is None or sale["price"] is None or sale["loans"] is None:
        return {"need": None, "basis": "unknown", "jeonse_dropped": None}
    stressed = sale["price"] * (1 - drop)
    taxes = _capital_gains(stressed, acquisition, owner, occupancy, today)
    if taxes is None:
        return {"need": None, "basis": "unknown", "jeonse_dropped": None}
    shortfall = amount + sale["loans"] - (stressed - taxes[taxes["headline"]]["total"])
    return {"need": max(Decimal(0), shortfall), "basis": "sale", "jeonse_dropped": None}


def _stress_need(amount: Decimal | None, jeonse: Decimal | None, drop: Decimal) -> dict[str, Any]:
    """What the stress scenario must still find for this deposit: the reverse-jeonse gap once the market
    level has fallen by `drop`, or the whole deposit when the market level is unknown."""
    if amount is None:
        return {"need": None, "basis": "unknown", "jeonse_dropped": None}
    if jeonse is None:
        return {"need": amount, "basis": "full_deposit", "jeonse_dropped": None}
    dropped = jeonse * (1 - drop)
    return {"need": max(Decimal(0), amount - dropped), "basis": "reverse_jeonse", "jeonse_dropped": dropped}
