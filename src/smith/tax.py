"""Tax view of the household (requirement FR-17): deterministic calculations and rule notes.

Smith reasons about after-tax outcomes: which account a won should go into, when gains should be
realized, and how housing, marriage registration and gifts interact with tax rules. Every rule below is
an explicit constant tagged with the tax year it reflects; reports add "현행 법령 확인 필요" because
the rules change (the 2026 tax bill was still pending when these were written). Amounts are computed
here; the narrative stage may explain them but never produces new ones.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from smith.fmt import percent, short_won
from smith.payload import LedgerView, base_amount
from smith.records import Kind

RULES_YEAR = 2026
RULES_CHECKED = "2026-10-05"
SOURCES = {
    "pension": ("소득세법 제59조의3(연금계좌세액공제)",
                "https://law.go.kr/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1030454707"),
    "overseas": ("국세청 국외주식 양도소득세 안내", "https://d.nts.go.kr/yeosu/na/ntt/selectNttInfo.do?mi=2201&nttSn=1350890"),
    "home": ("국세청 고가주택 양도차익 계산요령", "https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=8799&mi=12271"),
    "year_end": ("국세청 연말정산 안내(혼인세액공제 포함)",
                 "https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7875&mi=6467"),
}
MARRIAGE_CREDIT = Decimal(500_000)    # Per person, once, for marriages registered in 2024-2026.
# Pension accounts (소득세법 제59조의3): credit base 6M for pension savings, 9M with IRP; 13.2% effective
# (12% income tax + local tax) above 55M won gross pay, 16.5% at or below.
PENSION_SAVINGS_LIMIT = Decimal(6_000_000)
PENSION_TOTAL_LIMIT = Decimal(9_000_000)
CREDIT_RATE_HIGH, CREDIT_RATE_LOW = Decimal("0.132"), Decimal("0.165")
# Overseas listed shares: 22% (incl. local tax) on net gains above a 2.5M won yearly basic deduction.
OVERSEAS_DEDUCTION, OVERSEAS_RATE = Decimal(2_500_000), Decimal("0.22")
# ISA (general type): 2M won of net income tax-free after three years, 9.9% above; 20M won yearly payment
# limit; moving the matured balance to a pension account adds a 10% credit base up to 3M won.
ISA_TAX_FREE, ISA_TRANSFER_CREDIT_CAP = Decimal(2_000_000), Decimal(3_000_000)
INTEREST_TAX = Decimal("0.154")
FINANCIAL_INCOME_THRESHOLD = Decimal(20_000_000)  # Interest and dividends above this are taxed progressively.
HIGH_VALUE_HOME = Decimal(1_200_000_000)          # One-home exemption covers the price up to this.
MARRIAGE_MERGE_YEARS = 10                          # Sell either home within this many years of marriage.
MARRIAGE_GIFT_EXTRA = Decimal(100_000_000)         # Extra gift exemption within 2 years around registration.
SAVINGS_INSURANCE_MONTHLY_CAP = Decimal(1_500_000)  # Tax-free if <= this monthly, paid 5+ and kept 10+ years.
# Retirement: spending in today's won, real return and withdrawal rate are assumptions shown to the reader.
REAL_RETURN, WITHDRAWAL_RATE = Decimal("0.03"), Decimal("0.04")
RETIREMENT_AGES = (55, 60, 65)
# Capital gains on homes: progressive brackets (taxable base upper bound, rate, quick deduction), local tax
# 10% on top; one-home long-term deduction 4% a year each for holding (3+ years) and residence (2+ years),
# 40% caps; otherwise 2% a year from 3 years, 30% cap; 2.5M won basic deduction.
CGT_BRACKETS = ((14_000_000, "0.06", 0), (50_000_000, "0.15", 1_260_000), (88_000_000, "0.24", 5_760_000),
                (150_000_000, "0.35", 15_440_000), (300_000_000, "0.38", 19_940_000), (500_000_000, "0.40", 25_940_000),
                (1_000_000_000, "0.42", 35_940_000), (None, "0.45", 65_940_000))
CGT_BASIC_DEDUCTION = Decimal(2_500_000)
_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}


@dataclass(frozen=True)
class Note:
    """A tax strategy point that is not a single action: what the rule is and what it means here."""
    title: str
    body: str
    when: str
    certainty: str


def facts(view: LedgerView, today: date, household: dict[str, Any] | None) -> dict[str, Any]:
    """Tax-relevant facts computed from the ledger and the local household profile."""
    household = household or {}
    # Credits, limits and capital-gains netting are per taxpayer: only the client's own records count.
    mine = [r for r in view.records if r.owner_id == "self"]
    accounts = {r.record_id: r.fields.get("account_type") for r in mine if r.kind is Kind.ASSET}
    contributions: dict[str, Decimal] = {}
    for r in mine:
        f = r.fields
        if r.kind is not Kind.CASHFLOW or f["category"] != "internal_transfer" or not f.get("target_record_id"):
            continue
        kind = accounts.get(f["target_record_id"])
        amount = base_amount(view, r)
        if kind is None or amount is None:
            continue
        if f["frequency"] == "once":
            per_month = Decimal(0)
            total = amount if date.fromisoformat(f["start_date"]).year == today.year else Decimal(0)
        else:
            per_month = amount / _MONTHS[f["frequency"]]
            total = per_month * _months_in_year(f, today.year)  # Paid and still to be paid this calendar year.
        contributions[kind] = contributions.get(kind, Decimal(0)) + total
        contributions[f"{kind}:monthly"] = contributions.get(f"{kind}:monthly", Decimal(0)) + per_month
    us = [r for r in mine if r.kind is Kind.ASSET and r.fields.get("market") == "US" and r.fields.get("symbol")]
    gains = [_gain(view, r) for r in us]
    known = [g for g in gains if g is not None]
    return {
        "today": today, "year": today.year,
        "pension_savings_paid": contributions.get("pension_savings", Decimal(0)),
        "irp_paid": contributions.get("irp", Decimal(0)),
        "pension_monthly": contributions.get("pension_savings:monthly", Decimal(0)) + contributions.get("irp:monthly", Decimal(0)),
        "has_irp": "irp" in accounts.values(), "has_isa": "isa" in accounts.values(),
        "us_gains": sum((g for g in known if g > 0), Decimal(0)), "us_losses": sum((g for g in known if g < 0), Decimal(0)),
        "us_unknown": len(gains) - len(known), "us_positions": len(us),
        "homes": [(r.owner_id, base_amount(view, r), r.fields.get("occupancy")) for r in view.records
                  if r.kind is Kind.ASSET and r.fields["category"] == "real_estate"],
        "home_records": [{"record_id": r.record_id, "owner": r.owner_id, "value": base_amount(view, r),
                          "occupancy": r.fields.get("occupancy"),
                          **{k: (household.get("properties") or {}).get(r.record_id, {}).get(k)
                             for k in ("acquired_year", "acquired_price")}}
                         for r in view.records if r.kind is Kind.ASSET and r.fields["category"] == "real_estate"],
        "premiums": [(base_amount(view, r), r.fields["frequency"], r.fields["start_date"]) for r in view.records
                     if r.kind is Kind.CASHFLOW and r.fields["category"] == "insurance_premium"],
        "pension_assets": sum((base_amount(view, r) or Decimal(0)) for r in mine if r.kind is Kind.ASSET
                              and r.fields.get("account_type") in ("pension_savings", "irp")),
        "birth_year": household.get("birth_year"), "monthly_spend": household.get("retirement_monthly_spend"),
        "married": household.get("marriage_registered"), "cohabiting": household.get("cohabiting"),
    }


def _months_in_year(fields: dict[str, Any], year: int) -> int:
    start = max(date.fromisoformat(fields["start_date"]), date(year, 1, 1))
    end = min(date.fromisoformat(fields["end_date"]) if fields.get("end_date") else date(year, 12, 31), date(year, 12, 31))
    return 0 if end < start else (end.year - start.year) * 12 + end.month - start.month + 1


def _gain(view: LedgerView, record: Any) -> Decimal | None:
    f = record.fields
    if any(f.get(k) is None for k in ("quantity", "unit_price", "average_cost")) or f["currency"] not in view.rates:
        return None
    return (Decimal(f["unit_price"]) - Decimal(f["average_cost"])) * Decimal(f["quantity"]) * view.rates[f["currency"]]


# --- calculations behind proposals ------------------------------------------------------------------------

def pension_plan(t: dict[str, Any]) -> dict[str, Decimal]:
    """This year's credit room and the split that would use it fully."""
    savings, irp = t["pension_savings_paid"], t["irp_paid"]
    credited_savings = min(savings, PENSION_SAVINGS_LIMIT)
    credited = min(credited_savings + irp, PENSION_TOTAL_LIMIT)
    irp_room = max(Decimal(0), PENSION_TOTAL_LIMIT - credited_savings - irp)
    # Everything paid above what can be credited: 6M + 5M gives 11M paid, 9M credited, 2M excess.
    excess = savings + irp - credited
    monthly = t["pension_monthly"]
    return {"savings_paid": savings, "irp_paid": irp, "irp_room": irp_room, "excess": excess,
            "savings_excess": max(Decimal(0), savings - PENSION_SAVINGS_LIMIT), "credited": credited,
            "refund_gain": irp_room * CREDIT_RATE_HIGH, "max_refund": PENSION_TOTAL_LIMIT * CREDIT_RATE_HIGH,
            "monthly": monthly, "next_savings": PENSION_SAVINGS_LIMIT / 12,
            "next_irp": (PENSION_TOTAL_LIMIT - PENSION_SAVINGS_LIMIT) / 12,
            "next_rest": max(Decimal(0), monthly - PENSION_TOTAL_LIMIT / 12)}


def harvest_plan(t: dict[str, Any]) -> dict[str, Decimal] | None:
    """Realizing overseas gains up to the yearly basic deduction each year keeps them out of tax later."""
    if t["us_positions"] == 0 or t["us_gains"] <= 0:
        return None
    net = t["us_gains"] + t["us_losses"]
    # Selling only positions with gains: losses still held are not netted until they are sold too.
    realize = min(t["us_gains"], OVERSEAS_DEDUCTION)
    return {"gains": t["us_gains"], "losses": t["us_losses"], "net": net, "realize": realize,
            "saving": realize * OVERSEAS_RATE, "unknown": Decimal(t["us_unknown"]),
            "tax_if_all_sold": max(Decimal(0), net - OVERSEAS_DEDUCTION) * OVERSEAS_RATE}


def capital_gains(price: Decimal, acquired: Decimal, *, held: int, lived: int, one_home: bool) -> dict[str, Decimal]:
    """Estimated tax when selling a home at `price` (amounts in won). With the one-home exemption only
    the share of the gain above HIGH_VALUE_HOME is taxed, with the larger long-term deduction."""
    gain = max(Decimal(0), price - acquired)
    if one_home:
        taxable = Decimal(0) if price <= HIGH_VALUE_HOME else gain * (price - HIGH_VALUE_HOME) / price
        rate = (min(Decimal("0.04") * held, Decimal("0.4")) if held >= 3 else Decimal(0)) + \
            (min(Decimal("0.04") * lived, Decimal("0.4")) if lived >= 2 and held >= 3 else Decimal(0))
    else:
        taxable = gain
        rate = min(Decimal("0.02") * held, Decimal("0.3")) if held >= 3 else Decimal(0)
    base = max(Decimal(0), taxable * (1 - rate) - CGT_BASIC_DEDUCTION)
    tax = Decimal(0)
    for upper, bracket_rate, quick in CGT_BRACKETS:
        if upper is None or base <= upper:
            tax = max(Decimal(0), base * Decimal(bracket_rate) - quick)
            break
    return {"gain": gain, "taxable": taxable, "deduction_rate": rate, "base": base, "total": tax * Decimal("1.1")}


def retirement(t: dict[str, Any]) -> dict[str, Any] | None:
    if not t["birth_year"] or not t["monthly_spend"]:
        return None
    age = t["year"] - int(t["birth_year"])
    spend = Decimal(t["monthly_spend"])
    need = spend * 12 / WITHDRAWAL_RATE
    yearly = t["pension_monthly"] * 12
    paths = []
    for target in RETIREMENT_AGES:
        years = target - age
        if years <= 0:
            continue
        growth = (1 + REAL_RETURN) ** years
        projected = t["pension_assets"] * growth + yearly * (growth - 1) / REAL_RETURN
        paths.append({"age": target, "years": years, "projected": projected, "ratio": projected / need})
    return {"age": age, "need": need, "spend": spend, "paths": paths, "yearly": yearly}


# --- strategy notes and year-end checklist -----------------------------------------------------------------

def notes(t: dict[str, Any]) -> list[Note]:
    found = []
    homes = t["homes"]
    own = [h for h in homes if h[0] == "self"]
    other = [h for h in homes if h[0] != "self"]
    if own and other and t["married"] is False:
        found.append(Note(
            "혼인신고 시점이 주택 세금과 대출을 바꿉니다",
            ("지금은 혼인신고 전이라 세법상 고객님과 배우자분은 대개 각자 1주택자로 봅니다(같은 집에 살아도 법률상 배우자가 "
             "아니면 한 세대로 묶이지 않는 것이 일반적입니다). 혼인신고를 하면 한 세대 2주택이 되며, 혼인일부터 "
             f"{MARRIAGE_MERGE_YEARS}년 안에 먼저 파는 집은 요건을 갖추면 1세대 1주택처럼 비과세 특례를 받을 수 있습니다. "
             "반면 규제지역에서는 다주택 세대의 신규 주택담보대출이 막혀 있어, 새 집 매수·기존 집 매도·혼인신고의 순서에 따라 "
             "대출 가능 여부와 취득세가 달라질 수 있습니다. 또 2024~2026년에 혼인신고를 하면 두 분 각각 "
             f"{short_won(MARRIAGE_CREDIT)}의 혼인 세액공제(생애 1회)를 받을 수 있어, 올해 안에 신고하실지도 함께 따져 볼 만합니다."),
            "주택 이전 계획을 정하기 전", "세법 기준(현행 법령 확인 필요, 실행 전 세무사·은행 상담 권장)"))
    year = t["year"]
    for home in t["home_records"]:
        if home["value"] is None or not home["acquired_price"] or not home["acquired_year"]:
            continue
        # Only the year is known, so count full years conservatively (the anniversary may not have passed).
        held = max(0, year - int(home["acquired_year"]) - 1)
        price, acquired = home["value"], Decimal(home["acquired_price"])
        if home["owner"] == "self" and home["occupancy"] == "owner_occupied":
            exempt = capital_gains(price, acquired, held=held, lived=held, one_home=True)
            general = capital_gains(price, acquired, held=held, lived=0, one_home=False)
            found.append(Note(
                "거주 주택을 지금 판다면 예상 양도세",
                (f"평가액 {short_won(price)}에 팔면 차익은 약 {short_won(exempt['gain'])}입니다. 1세대 1주택 비과세 요건을 "
                 f"갖추면 {short_won(HIGH_VALUE_HOME)}을 넘는 비율만큼만 과세되어 약 {short_won(exempt['total'])}"
                 f"(보유·거주 {held}년 가정, 장기보유특별공제 {percent(exempt['deduction_rate'], 0)}), 요건을 못 갖추면 약 "
                 f"{short_won(general['total'])}(지방소득세 포함)입니다. 세대의 주택 수, 정확한 취득일과 실제 거주 기간에 따라 "
                 "이 범위 안에서 달라지며, 보유·거주가 1년 늘 때마다 1주택 공제가 8%p씩 커집니다."),
                "매도 시점을 정할 때", "계산(취득가·평가액 기준, 범위 추정) + 세법 기준(현행 법령 확인 필요)"))
        elif home["owner"] != "self":
            lived = capital_gains(price, acquired, held=held, lived=2, one_home=True)  # The partner's own tax.
            not_lived = capital_gains(price, acquired, held=held, lived=0, one_home=False)
            found.append(Note(
                "배우자분 주택은 2년 거주 여부에 따라 양도세가 크게 달라집니다",
                (f"평가액 {short_won(price)}에 팔면 차익은 약 {short_won(not_lived['gain'])}입니다. 취득 당시({home['acquired_year']}년) "
                 "그 지역이 조정대상지역이었다면 1세대 1주택 비과세에 2년 거주가 필요합니다. 2년 이상 사셨다면 세금은 약 "
                 f"{short_won(lived['total'])}이지만, 거주하지 않아 비과세가 안 되면 약 {short_won(not_lived['total'])}"
                 "(지방소득세 포함)으로 커집니다. 어느 집을 먼저 팔지, 혼인신고를 언제 할지 정하기 전에 거주 이력을 확인해 두시는 것이 "
                 "중요합니다."),
                "주택 이전 순서를 정하기 전", "계산(취득가·평가액 기준) + 세법 기준(현행 법령 확인 필요, 세무사 확인 권장)"))
    for owner, value, occupancy in own:
        if value is not None and value > HIGH_VALUE_HOME and not any(
                h["owner"] == "self" and h["acquired_price"] for h in t["home_records"]):
            found.append(Note(
                "거주 주택은 고가주택이라 매도 시 일부에 양도세가 붙습니다",
                (f"1세대 1주택 비과세는 양도가액 {short_won(HIGH_VALUE_HOME)}까지이고, 넘는 부분 비율만큼 양도차익에 세금이 "
                 f"붙습니다(현재 평가액 {short_won(value)}). 이때 보유·거주 기간에 따라 장기보유특별공제가 크게 달라지므로, "
                 "취득일과 취득가를 알려 주시면 이전 시점별 예상 세금을 계산해 드리겠습니다."),
                "매도 시점을 정하기 전", "세법 기준(현행 법령 확인 필요) + 자료 요청"))
    if t["married"] is False:
        found.append(Note(
            "혼인 전후 2년은 부모님 증여 공제가 커집니다",
            (f"혼인신고일 전후 2년 안에 부모님께 받는 증여는 기본 공제에 더해 {short_won(MARRIAGE_GIFT_EXTRA)}을 추가로 "
             "공제받을 수 있습니다. 주택 이전에 가족의 도움이 있다면, 그 시기를 혼인신고 일정과 맞추는 것이 세금상 유리합니다."),
            "가족 지원 계획이 있을 때", "세법 기준(현행 법령 확인 필요)"))
    for amount, frequency, start in t["premiums"]:
        monthly = None if amount is None or frequency not in _MONTHS else amount / _MONTHS[frequency]
        if monthly is not None and Decimal(500_000) <= monthly <= SAVINGS_INSURANCE_MONTHLY_CAP:
            found.append(Note(
                "저축성 보험은 10년 유지가 비과세 조건입니다",
                (f"월 {short_won(monthly)}을 내는 보험이 저축성이라면, 월 납입 {short_won(SAVINGS_INSURANCE_MONTHLY_CAP)} "
                 "이하로 5년 이상 내고 10년 이상 유지해야 이자(보험차익)가 비과세입니다. 중간에 해지하면 세금과 해지 손실이 "
                 f"함께 생기므로, 비상금은 따로 마련해 두시는 것이 안전합니다({start[:4]}년 가입 기준)."),
                "가입 기간 내내", "세법 기준(현행 법령 확인 필요, 보험 종류 확인 필요)"))
    found.append(Note(
        "이자·배당이 연 2천만 원을 넘으면 세율이 올라갑니다",
        (f"이자와 배당을 합해 연 {short_won(FINANCIAL_INCOME_THRESHOLD)}을 넘으면 다른 소득과 합쳐 높은 세율로 과세됩니다. "
         "배당이 큰 국내 주식이나 예금이 늘수록, 연금계좌·ISA처럼 과세를 미루거나 줄이는 계좌에 두는 효과가 커집니다."),
        "자산 배치를 바꿀 때", "세법 기준(현행 법령 확인 필요)"))
    return found


def checklist(t: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Year-end settlement items: (item, this year's status, what to do)."""
    plan = pension_plan(t)
    rows = [("연금저축", f"{short_won(plan['savings_paid'])} 납입 예상 / 공제 한도 {short_won(PENSION_SAVINGS_LIMIT)}",
             f"연금계좌 전체로 {short_won(plan['excess'])} 초과, 올해 공제 없음(다음 해 공제 전환 신청 가능 여부 확인)"
             if plan["excess"] else "한도 안"),
            ("IRP", f"{short_won(plan['irp_paid'])} 납입 / 남은 한도 {short_won(plan['irp_room'])}",
             f"12월 31일까지 {short_won(plan['irp_room'])} 납입 시 최대 약 {short_won(plan['refund_gain'])} 세액 감소"
             if plan["irp_room"] else "한도 채움")]
    rows.append(("보장성 보험료", "연 100만 원까지 12% 세액공제", "회사 연말정산에 자동 반영되는지 확인"))
    rows.append(("신용·체크카드", "총급여의 25% 초과분부터 소득공제", "연말까지는 공제율이 높은 체크카드·현금영수증 사용이 유리"))
    return rows


def sources() -> list[tuple[str, str]]:
    """(title, URL) of the official pages the rules were checked against, for the report."""
    return list(SOURCES.values())


def assumptions() -> list[str]:
    return [f"세법 기준 {RULES_YEAR}년(개정안 반영 전, {RULES_CHECKED} 공식 자료로 확인, 현행 법령 확인 필요)",
            "연금계좌 납입: 원장의 정기 이체를 공제 대상 납입으로 가정(계좌 간 이전·ISA 만기 이전액은 제외 대상)",
            f"연금계좌 공제율 {percent(CREDIT_RATE_HIGH, 1)}(총급여 5,500만 원 초과 가정)",
            f"해외주식 양도세 {percent(OVERSEAS_RATE, 0)}, 기본공제 연 {short_won(OVERSEAS_DEDUCTION)}, 원화 손익은 현재 환율 기준 근사",
            f"은퇴 필요 자금: 현재 가치 월 생활비, 실질 수익률 연 {percent(REAL_RETURN, 0)}, 인출률 연 {percent(WITHDRAWAL_RATE, 0)}, 국민연금 제외",
            "양도세 추정: 원장 평가액에 판다고 가정, 누진세율·장기보유특별공제·기본공제 250만 원 반영, 필요경비 제외"]
