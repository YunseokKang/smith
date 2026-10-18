"""Retirement pension stack (report design §18.5): the household's pension pipelines side by side.

The retirement track used to count the pension accounts alone and call the result a readiness ratio. A
household with a national pension, an insurer's annuity, a pension-savings account and an IRP has four
pipelines, and the question the client asks is simpler: how much of the monthly retirement target do they
cover together, after tax, and from what age? This module answers it deterministically:

- a pipeline the client has stated (a `pension_income` cash flow with `pension_source`, amount in today's
  won, `start_date` the payout start) is used as entered;
- a pension account (pension_savings, IRP) without a stated payout is projected like the retirement track:
  balance plus contributions at the real return to the payout age, then the withdrawal rate;
- an insurer's annuity without a stated payout is projected from its surrender value plus the premiums still
  to be paid over the policy's payment term (the client keeps the policy to maturity, user statement
  2026-10-18), net of an assumed loading, against the product's guaranteed annuity base;
- the national pension cannot be estimated from the ledger: without a statement it is unknown and asked for.

Every tax factor is an assumption echoed in the result (현행 법령 확인 필요). An unknown pipeline makes the
total a lower bound, never zero.
"""
from datetime import date
from decimal import Decimal
from typing import Any

from smith.payload import LedgerView, base_amount
from smith.records import Kind
from smith.tax import REAL_RETURN, WITHDRAWAL_RATE

DEFAULT_PAYOUT_AGE = 60
PIPELINES = ("national", "insurance_annuity", "pension_savings", "irp")  # Always shown, in this order.
LABELS = {"national": "국민연금", "insurance_annuity": "변액·연금보험", "pension_savings": "연금저축", "irp": "IRP",
          "occupational": "퇴직연금", "other": "기타 연금"}
_ACCOUNTS = {"pension_savings": "pension_savings", "irp": "irp"}
# Assumed tax on the payout (share of the gross monthly figure). National pension income is taxed after the
# pension-income deduction and comes out small; private pensions paid as an annuity are taxed at 5.5% (55-69)
# while the year's private payouts stay within the separate-taxation limit, 16.5% above it; an insurer's
# annuity kept ten years within the premium limits is tax-free.
TAX_FACTORS = {"national": Decimal("0.03"), "insurance_annuity": Decimal(0), "pension_savings": Decimal("0.055"),
               "irp": Decimal("0.055"), "occupational": Decimal("0.055"), "other": Decimal("0.055")}
PRIVATE_SEPARATE_LIMIT = Decimal(15_000_000)   # Yearly private pension payouts above this lose the low rate.
# The insurer's annuity: a premium of at least this a month with a fixed payment
# term (end_date) is the policy's premium; ANNUITY_LOADING of each premium is assumed to go to charges and the
# rest to the fund; the product guarantees an annuity base of the premiums paid plus simple interest at
# GUARANTEE_SIMPLE_RATE a year until commencement (secondary sources, 2026-10-18; 약관 확인 필요), which is a
# nominal figure deflated at INFLATION into today's won before it is compared with the fund projection.
ANNUITY_PREMIUM_MIN = Decimal(500_000)
ANNUITY_LOADING = Decimal("0.10")
GUARANTEE_SIMPLE_RATE = Decimal("0.05")
INFLATION = Decimal("0.02")
PRIVATE_HIGH_TAX = Decimal("0.165")
_PRIVATE = ("pension_savings", "irp", "occupational", "other")
# National pension eligibility age by birth year (국민연금법 부칙): 61 for 1953-56 ... 65 from 1969.
_NATIONAL_AGES = ((1952, 60), (1956, 61), (1960, 62), (1964, 63), (1968, 64))
PRIVATE_EARLIEST_AGE = 55
_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}


def stack(view: LedgerView, household: dict[str, Any] | None, today: date) -> dict[str, Any] | None:
    """The pension stack against the monthly retirement target; None without a target (the track asks).

    Keys: target, payout_age, age, pipelines (key, label, start_age, gross_monthly, net_monthly, basis
    stated/estimated/unknown, basis_text, missing), total_net (known pipelines only), ratio, unknown (keys
    without a figure), complete, assumptions.
    """
    household = household or {}
    target = household.get("retirement_monthly_spend")
    if not target:
        return None
    target = Decimal(target)
    birth = household.get("birth_year")
    age = None if birth is None else today.year - int(birth)
    # Private pensions pay from 55 at the earliest: an earlier retirement age is still projected to 55, so the
    # growth period and the start age shown agree (review 2026-10-18).
    payout_age = max(int(household.get("retirement_age") or DEFAULT_PAYOUT_AGE), PRIVATE_EARLIEST_AGE)
    stated = _stated(view, today)
    rows = [_pipeline(view, key, stated.get(key, []), age=age, birth=birth, payout_age=payout_age, today=today)
            for key in PIPELINES]
    rows += [_pipeline(view, key, records, age=age, birth=birth, payout_age=payout_age, today=today)
             for key, records in sorted(stated.items()) if key not in PIPELINES]
    _apply_tax(rows)
    known = [r["net_monthly"] for r in rows if r["net_monthly"] is not None]
    total = sum(known, Decimal(0))
    unknown = [r["key"] for r in rows if r["net_monthly"] is None]
    return {"target": target, "payout_age": payout_age, "age": age, "pipelines": rows, "total_net": total,
            "ratio": total / target, "unknown": unknown, "complete": not unknown,
            "assumptions": assumptions()}


def _stated(view: LedgerView, today: date) -> dict[str, list[Any]]:
    """The client's own pension_income flows by pipeline (a future start is a stated payout, an ended one is
    not); flows without a source count as "other"."""
    found: dict[str, list[Any]] = {}
    for record in view.records:
        f = record.fields
        if record.kind is not Kind.CASHFLOW or f["category"] != "pension_income" or record.owner_id != "self":
            continue
        if f.get("end_date") and f["end_date"] < today.isoformat():
            continue
        found.setdefault(f.get("pension_source") or "other", []).append(record)
    return found


def _pipeline(view: LedgerView, key: str, records: list[Any], *, age: int | None, birth: Any, payout_age: int,
              today: date) -> dict[str, Any]:
    label = LABELS.get(key, key)
    row = {"key": key, "label": label, "start_age": None, "gross_monthly": None, "net_monthly": None,
           "assets_now": None, "monthly_contribution": None, "basis": "unknown", "basis_text": "", "missing": ""}
    if records:
        amounts = [base_amount(view, r) for r in records]
        if any(a is None for a in amounts):
            row.update(basis_text="예상 수령액을 원화로 환산하지 못함", missing="그 통화의 환율")
            return row
        monthly = Decimal(0)
        nominal = False
        nominal_monthly = Decimal(0)   # The statement's own figure before deflation, for the table.
        starts = [date.fromisoformat(r.fields["start_date"]) for r in records]
        for amount, record in zip(amounts, records):
            part = amount / _MONTHS.get(record.fields["frequency"], 1)
            nominal_monthly += part
            if record.fields.get("price_basis") == "nominal":
                # An insurer's illustration is the amount of the first payment year: bring it to today's won so
                # it stacks with the national pension statement and the real-return projections.
                start = date.fromisoformat(record.fields["start_date"])
                years = Decimal(max(0, (start.year - today.year) * 12 + start.month - today.month)) / 12
                part = part / (1 + INFLATION) ** years
                nominal = True
            monthly += part
        start_year = min(starts).year
        row.update(basis="stated", start_age=None if birth is None else start_year - int(birth), start_year=start_year,
                   start_date=min(starts), nominal_deflated=nominal, nominal_monthly=nominal_monthly if nominal else None)
        value_note = f"명목 금액을 물가 {INFLATION * 100:.0f}%로 현재 가치 환산" if nominal else "현재 가치"
        # An after-tax statement figure (the fund's own estimate) is used as the net payout, untouched.
        if all(r.fields.get("amount_basis") == "net" for r in records):
            row.update(net_monthly=monthly, gross_monthly=None, tax_factor=None, stated_net=True,
                       basis_text=f"알려 주신 예상 수령액(세후, {value_note})")
        else:
            row.update(gross_monthly=monthly, basis_text=f"알려 주신 예상 수령액(세전, {value_note})")
        return row
    if key == "national":
        row.update(start_age=None if birth is None else national_pension_age(int(birth)),
                   basis_text="예상 수령액이 입력되지 않음",
                   missing="국민연금공단 '내 연금 알아보기'의 예상 월 수령액(현재 가치)과 수령 개시 연령")
        return row
    if key in _ACCOUNTS:
        assets = _assets(view, lambda f: f.get("account_type") == _ACCOUNTS[key])
        contribution = _contributions(view, _ACCOUNTS[key], today)
        row.update(assets_now=assets, monthly_contribution=contribution)
        if assets is None or contribution is None:
            row.update(basis_text="잔액이나 납입액을 원화로 환산하지 못함", missing="그 통화의 환율")
            return row
        if not _has_account(view, _ACCOUNTS[key]):
            row.update(basis_text="계좌가 없음", missing=f"{label} 계좌가 있다면 잔액과 월 납입액")
            return row
        return _project(row, assets, contribution, age=age, payout_age=payout_age)
    if key == "insurance_annuity":
        assets = _assets(view, lambda f: f["category"] == "insurance_surrender_value")
        if assets is None:
            row.update(basis_text="해약환급금을 원화로 환산하지 못함", missing="그 통화의 환율")
            return row
        if not any(r.kind is Kind.ASSET and r.owner_id == "self" and r.fields["category"] == "insurance_surrender_value"
                   for r in view.records):
            row.update(basis_text="연금보험 레코드가 없음", missing="연금보험이 있다면 해약환급금 또는 보험사 예시표의 예상 월 연금액")
            return row
        row.update(assets_now=assets)
        return _annuity(view, row, assets, age=age, payout_age=payout_age, today=today)
    row.update(basis_text="예상 수령액이 입력되지 않음", missing=f"{label}의 예상 월 수령액과 개시 시점")
    return row


def _project(row: dict[str, Any], assets: Decimal, contribution: Decimal, *, age: int | None,
             payout_age: int) -> dict[str, Any]:
    """Balance plus contributions at the real return until the payout age, drawn at the withdrawal rate
    (the same arithmetic as the retirement track, so the two never disagree)."""
    if age is None:
        row.update(basis_text="출생 연도가 없어 수령 개시까지의 기간을 알 수 없음", missing="출생 연도(로컬 설정)")
        return row
    years = max(0, payout_age - age)
    growth = (1 + REAL_RETURN) ** years
    projected = assets * growth + contribution * 12 * (growth - 1) / REAL_RETURN
    row.update(gross_monthly=projected * WITHDRAWAL_RATE / 12, start_age=max(payout_age, PRIVATE_EARLIEST_AGE),
               basis="estimated",
               basis_text=f"지금 잔액과 월 납입을 {payout_age}세까지 실질 연 {REAL_RETURN * 100:.0f}%로 불려 연 "
                          f"{WITHDRAWAL_RATE * 100:.0f}%씩 꺼내 쓴다고 본 추정")
    return row


def _annuity(view: LedgerView, row: dict[str, Any], assets: Decimal, *, age: int | None, payout_age: int,
             today: date) -> dict[str, Any]:
    """The insurer's annuity kept to maturity: the surrender value plus the premiums still due (net of the
    loading) at the real return until the payout age, or the guaranteed base (premiums plus simple interest,
    deflated) when that is higher, drawn at the withdrawal rate. Without a qualifying premium the surrender
    value alone is projected (a lower bound)."""
    if age is None:
        row.update(basis_text="출생 연도가 없어 수령 개시까지의 기간을 알 수 없음", missing="출생 연도(로컬 설정)")
        return row
    years = max(0, payout_age - age)
    payout = date(today.year + years, today.month, 1)
    premiums = [r for r in view.records if r.kind is Kind.CASHFLOW and r.owner_id == "self"
                and r.fields["category"] == "insurance_premium" and r.fields.get("end_date")
                and r.fields["frequency"] == "monthly" and r.fields["end_date"] >= today.isoformat()]
    monthly = Decimal(0)
    floor = Decimal(0)   # Nominal guaranteed base at commencement.
    counted = False
    for record in premiums:
        amount = base_amount(view, record)
        if amount is None or amount < ANNUITY_PREMIUM_MIN:
            continue
        counted = True
        start, end = date.fromisoformat(record.fields["start_date"]), date.fromisoformat(record.fields["end_date"])
        month = date(start.year, start.month, 1)
        while month <= end and month < payout:
            held = Decimal((payout.year - month.year) * 12 + payout.month - month.month) / 12
            floor += amount * (1 + GUARANTEE_SIMPLE_RATE * held)
            month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        monthly += amount  # The policy's monthly premium (still being paid).
        row["pay_until"] = end
    growth = (1 + REAL_RETURN) ** years
    projected = assets * growth
    if counted and monthly:
        end = max(date.fromisoformat(r.fields["end_date"]) for r in premiums)
        pay_years = min(Decimal(max(0, (end.year - today.year) * 12 + end.month - today.month)) / 12, Decimal(years))
        invested = monthly * (1 - ANNUITY_LOADING) * 12
        projected += invested * ((1 + REAL_RETURN) ** pay_years - 1) / REAL_RETURN * (1 + REAL_RETURN) ** (years - pay_years)
    floor_real = floor / (1 + INFLATION) ** years
    base = max(projected, floor_real)
    row.update(gross_monthly=base * WITHDRAWAL_RATE / 12, start_age=max(payout_age, PRIVATE_EARLIEST_AGE), basis="estimated",
               premiums_counted=counted, projected_real=projected, guarantee_floor_real=floor_real,
               floor_applies=floor_real > projected, monthly_premium=monthly,
               missing="보험사 예시표의 예상 월 연금액과 개시 연령")
    if counted:
        row["basis_text"] = (f"납입 기간까지 월 보험료 {monthly / 10_000:,.0f}만 원을 다 내고 해지하지 않는다고 보고, 사업비 "
                             f"{ANNUITY_LOADING * 100:.0f}%를 뺀 적립금을 {payout_age}세까지 실질 연 {REAL_RETURN * 100:.0f}%로 불린 값과 "
                             f"납입보험료 연 {GUARANTEE_SIMPLE_RATE * 100:.0f}% 단리 최저보증(현재 가치 환산) 중 큰 쪽"
                             f"{'(최저보증 적용)' if floor_real > projected else '(적립금 적용)'}을 연 {WITHDRAWAL_RATE * 100:.0f}%씩 "
                             "연금으로 받는다고 본 추정. 보험사 예시표 연금액을 알려 주시면 그 값으로 바꿉니다")
    else:
        row["basis_text"] = ("지금 해약환급금만 실질 연 3%로 불린 추정(앞으로 낼 보험료는 넣지 않은 하한). 보험사 예시표의 예상 월 "
                             "연금액을 알려 주시면 그 값으로 바꿉니다")
    return row


def _apply_tax(rows: list[dict[str, Any]]) -> None:
    """Net monthly figures by the assumed tax factors; private payouts above the separate-taxation limit in a
    year are taxed at the high rate instead of the low pension-income rate."""
    private_yearly = sum((r["gross_monthly"] for r in rows if r["key"] in _PRIVATE and r["gross_monthly"] is not None),
                         Decimal(0)) * 12
    high = private_yearly > PRIVATE_SEPARATE_LIMIT
    for row in rows:
        gross = row["gross_monthly"]
        if gross is None or row.get("stated_net"):
            continue
        factor = PRIVATE_HIGH_TAX if high and row["key"] in _PRIVATE else TAX_FACTORS.get(row["key"], Decimal("0.055"))
        row.update(tax_factor=factor, net_monthly=gross * (1 - factor), high_private_tax=high and row["key"] in _PRIVATE)


def national_pension_age(birth_year: int) -> int:
    for upto, age in _NATIONAL_AGES:
        if birth_year <= upto:
            return age
    return 65


def _assets(view: LedgerView, accept: Any) -> Decimal | None:
    total = Decimal(0)
    for record in view.records:
        if record.kind is Kind.ASSET and record.owner_id == "self" and accept(record.fields):
            value = base_amount(view, record)
            if value is None:
                return None
            total += value
    return total


def _has_account(view: LedgerView, account_type: str) -> bool:
    return any(r.kind is Kind.ASSET and r.owner_id == "self" and r.fields.get("account_type") == account_type
               for r in view.records)


def _contributions(view: LedgerView, account_type: str, today: date) -> Decimal | None:
    """Monthly transfers into the client's accounts of this type (the same reading as the tax rules)."""
    accounts = {r.record_id for r in view.records if r.kind is Kind.ASSET and r.owner_id == "self"
                and r.fields.get("account_type") == account_type}
    total = Decimal(0)
    for record in view.records:
        f = record.fields
        if record.kind is not Kind.CASHFLOW or f["category"] != "internal_transfer" or f.get("target_record_id") not in accounts \
                or f["frequency"] not in _MONTHS or (f.get("end_date") and f["end_date"] < today.isoformat()):
            continue
        amount = base_amount(view, record)
        if amount is None:
            return None
        total += amount / _MONTHS[f["frequency"]]
    return total


def assumptions() -> list[str]:
    return [f"연금 스택: 국민연금 세후 {TAX_FACTORS['national'] * 100:.0f}% 차감, 연금저축·IRP 연금소득세 "
            f"{TAX_FACTORS['irp'] * 100:.1f}%(사적연금 연 {PRIVATE_SEPARATE_LIMIT / 10_000_000:.0f}천만 원 초과 시 "
            f"{PRIVATE_HIGH_TAX * 100:.1f}%), 연금보험 비과세 요건 충족 가정(현행 법령 확인 필요)",
            f"연금 계좌 수령액: 지금 잔액과 월 납입을 수령 개시 연령(기본 {DEFAULT_PAYOUT_AGE}세)까지 실질 연 "
            f"{REAL_RETURN * 100:.0f}%로 불려 연 {WITHDRAWAL_RATE * 100:.0f}%씩 인출한다고 가정",
            f"연금보험: 납입 기간 끝까지 보험료를 내고 해지하지 않으며 사업비 {ANNUITY_LOADING * 100:.0f}%, 납입보험료 연 "
            f"{GUARANTEE_SIMPLE_RATE * 100:.0f}% 단리 최저보증(물가 {INFLATION * 100:.0f}%로 현재 가치 환산, 약관 확인 필요) 가정"]
