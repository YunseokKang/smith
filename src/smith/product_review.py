"""Product reviews: what a financial product the household holds is actually worth (report design §19).

The client asked "이 상품 어떻게 생각해?" about the insurer's annuity and wanted a verdict with numbers rather
than a description (user decision 2026-10-18, FR-24). The first review is that annuity, computed from the
ledger and the insurer's own illustration: the money still to be paid against the lifetime payout, the forward
internal rate of return by lifespan, the same money in a broad index fund at plain return assumptions, the
return the alternative would need to match the annuity (the break-even), the payout in today's won now and
late in life, and a verdict with the condition that flips it.

Each review belongs to one policy: the payout (`pension_income`, `pension_source=insurance_annuity`), its
premium and its surrender value are matched by `policy_record_id` (the surrender-value asset), or, without
links, only when there is exactly one of each. Unrelated contracts are never added together; a payout that
cannot be matched is held and asked about (review 2026-10-18). Everything is deterministic; assumptions are
named constants echoed in the result; an unknown input leaves the review out rather than guessing.
"""
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

from smith.payload import LedgerView, base_amount
from smith.pension import ANNUITY_PREMIUM_MIN, INFLATION
from smith.records import Kind
from smith.tax import WITHDRAWAL_RATE

LIFESPANS = (80, 85, 90, 100)                                   # Ages the lifetime payout is valued to.
ALTERNATIVE_RETURNS = (Decimal("0.04"), Decimal("0.06"), Decimal("0.08"))  # Nominal, a broad index fund.
# The break-even is the return the alternative must earn to pay as much as the annuity. A high one means the
# annuity is hard to beat (keep); a low one means even safe assets match it (surrender is worth weighing).
BOND_LIKE_RETURN = Decimal("0.04")      # At or below: safe assets match the annuity.
EQUITY_RETURN = Decimal("0.065")        # Above: not even a plain index fund is expected to match it.
SEARCH_LOW, SEARCH_HIGH = Decimal("-0.2"), Decimal("0.3")       # Where rates are searched (a year, nominal).
REFERENCE_LIFESPAN = 85
LATE_AGE = 85                            # The payout's real value this late in life shows the inflation drag.
# A rate is a ratio, not money: solving for it at modest precision is exact enough and keeps a report build
# fast (the ledger's 60-digit context made one build take about a second).
_PRECISION = 28
_ITERATIONS = 48
_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}


def annuity_reviews(view: LedgerView, household: dict[str, Any] | None, today: date) -> list[dict[str, Any]]:
    """One review per insurer's annuity payout that matches its own policy and premium, largest first."""
    birth = (household or {}).get("birth_year")
    if birth is None:
        return []
    payouts, premiums, policies = _candidates(view, today)
    reviews = []
    for payout in payouts:
        match = _match(payout, payouts, premiums, policies)
        if match is None:
            continue
        review = _review(view, payout, *match, birth=int(birth), today=today)
        if review is not None:
            reviews.append(review)
    return sorted(reviews, key=lambda r: -r["annuity_monthly_nominal"])


def unmatched(view: LedgerView, today: date) -> int:
    """How many annuity payouts cannot be matched to one policy and one premium (asked for in the requests)."""
    payouts, premiums, policies = _candidates(view, today)
    return sum(1 for p in payouts if _match(p, payouts, premiums, policies) is None)


def verdict_text(review: dict[str, Any]) -> tuple[str, str]:
    """(verdict sentence, the condition that flips it) in the client's words."""
    be, bound = review["break_even_return"], review["break_even_bound"]
    be_text = "" if be is None else f"{be * 100:.1f}%"
    if review["verdict"] == "strong_keep":
        flip = (f"같은 돈을 연 {SEARCH_HIGH * 100:.0f}%로 굴려도 따라잡기 어려워, 해지가 나은 경우를 찾기 어렵습니다." if bound == "above"
                else f"남은 보험료를 연 {be_text} 넘게 굴릴 자신이 있을 때만 해지가 낫습니다.")
        return "안전자산 수익률로는 따라잡기 어려운 조건이라 유지하는 것이 유리합니다.", flip
    if review["verdict"] == "keep_as_floor":
        return ("투자 상품으로는 비싸지만, 평생 나오는 세후 바닥 연금으로 보면 지금 깨는 것이 꼭 이득은 아닙니다. 채권 자리로 "
                "두고 추가 납입은 하지 않는 것이 Smith의 판단입니다.",
                f"남은 보험료와 해지금을 연 {be_text} 넘게 꾸준히 굴릴 자신이 있고 평생 보증이 없어도 괜찮다면 해지가 기대값에서 앞섭니다.")
    flip = ("수익률이 마이너스여도 같은 돈을 직접 두는 쪽이 많아, 평생 보증 자체가 꼭 필요할 때만 유지가 맞습니다." if bound == "below"
            else f"지수펀드 수익률이 연 {be_text}에도 못 미친다고 보시거나 시장과 무관한 바닥 연금을 원하시면 유지가 맞습니다.")
    return ("같은 돈을 넓은 지수펀드에 두는 쪽의 기대값이 뚜렷이 커서, 평생 보증에 얼마나 값을 치를지의 문제입니다. 보증이 꼭 "
            "필요하지 않다면 해지가 Smith의 판단입니다.", flip)


def verdict_for(break_even: Decimal | None, bound: str | None) -> str:
    """The verdict from the break-even: the higher the return the alternative needs, the stronger the keep. A
    break-even outside the search range keeps its direction ("above": keep, "below": weigh surrender)."""
    if bound == "above":
        return "strong_keep"
    if bound == "below" or break_even is None:
        return "surrender_candidate"
    if break_even <= BOND_LIKE_RETURN:
        return "surrender_candidate"
    return "keep_as_floor" if break_even <= EQUITY_RETURN else "strong_keep"


def _candidates(view: LedgerView, today: date) -> tuple[list[Any], list[Any], dict[str, Any]]:
    mine = [r for r in view.records if r.owner_id == "self"]
    payouts = [r for r in mine if r.kind is Kind.CASHFLOW and r.fields["category"] == "pension_income"
               and r.fields.get("pension_source") == "insurance_annuity"]
    premiums = [r for r in mine if r.kind is Kind.CASHFLOW and r.fields["category"] == "insurance_premium"
                and r.fields["frequency"] == "monthly" and r.fields.get("end_date")
                and r.fields["end_date"] >= today.isoformat()
                and (base_amount(view, r) is None or base_amount(view, r) >= ANNUITY_PREMIUM_MIN)]
    policies = {r.record_id: r for r in mine if r.kind is Kind.ASSET and r.fields["category"] == "insurance_surrender_value"}
    return payouts, premiums, policies


def _match(payout: Any, payouts: list[Any], premiums: list[Any], policies: dict[str, Any]) -> tuple[Any, Any] | None:
    """(premium, policy) of one payout: by `policy_record_id`, or the single candidates when nothing is linked."""
    policy_id = payout.fields.get("policy_record_id")
    if policy_id is None:
        if len(payouts) != 1 or len(policies) != 1:
            return None
        policy_id = next(iter(policies))
    policy = policies.get(policy_id)
    if policy is None:
        return None
    linked = [p for p in premiums if p.fields.get("policy_record_id") == policy_id]
    if not linked:
        unlinked = [p for p in premiums if not p.fields.get("policy_record_id")]
        if len(payouts) == 1 and len(policies) == 1 and len(unlinked) == 1:
            linked = unlinked
    return (linked[0], policy) if len(linked) == 1 else None


def _review(view: LedgerView, payout: Any, premium: Any, policy: Any, *, birth: int, today: date) -> dict[str, Any] | None:
    monthly_premium, surrender, stated = base_amount(view, premium), base_amount(view, policy), base_amount(view, payout)
    if monthly_premium is None or surrender is None or stated is None:
        return None
    pay_start, pay_end = date.fromisoformat(premium.fields["start_date"]), date.fromisoformat(premium.fields["end_date"])
    annuity_start = date.fromisoformat(payout.fields["start_date"])
    this_month = date(today.year, today.month, 1)
    total_months = _months(pay_start, pay_end) + 1
    paid_months = max(0, min(total_months, _months(pay_start, this_month) + (1 if pay_start.day <= today.day else 0)))
    remaining = total_months - paid_months
    to_start = max(0, _months(this_month, annuity_start))
    years_to_start = Decimal(to_start) / 12
    annuity = stated / _MONTHS.get(payout.fields["frequency"], 1)
    if payout.fields.get("price_basis") != "nominal":   # A today's-won figure is grown to the money of that year.
        annuity = annuity * (1 + INFLATION) ** years_to_start
    start_age = annuity_start.year - birth
    age = today.year - birth
    with localcontext() as context:
        context.prec = _PRECISION
        outflows = [(0, -surrender)] + [(t, -monthly_premium) for t in range(1, remaining + 1)]
        by_lifespan = []
        for lifespan in LIFESPANS:
            years = lifespan - start_age
            if years > 0:
                flows = outflows + [(to_start + k, annuity) for k in range(years * 12)]
                by_lifespan.append({"to_age": lifespan, "irr": _irr(flows), "received": annuity * 12 * years})
        alternatives = []
        for rate in ALTERNATIVE_RETURNS:
            value = _grown(surrender, monthly_premium, remaining, to_start, rate)
            alternatives.append({"rate": rate, "value_at_start": value, "monthly": value * WITHDRAWAL_RATE / 12,
                                 "monthly_real": value * WITHDRAWAL_RATE / 12 / (1 + INFLATION) ** years_to_start})
        break_even, bound = _break_even(surrender, monthly_premium, remaining, to_start, annuity)
    verdict = verdict_for(break_even, bound)
    paid_so_far = monthly_premium * paid_months
    return {
        "policy_record_id": policy.record_id, "policy_name": policy.fields.get("instrument_name"),
        "monthly_premium": monthly_premium, "pay_start": pay_start, "pay_end": pay_end, "total_months": total_months,
        "paid_months": paid_months, "remaining_months": remaining, "total_premiums": monthly_premium * total_months,
        "paid_so_far": paid_so_far, "remaining_premiums": monthly_premium * remaining,
        "surrender_now": surrender, "loss_if_surrendered": paid_so_far - surrender,
        "annuity_monthly_nominal": annuity, "annuity_yearly_nominal": annuity * 12, "annuity_start": annuity_start,
        "start_age": start_age, "annuity_real_now": annuity / (1 + INFLATION) ** years_to_start,
        "annuity_real_late": annuity / (1 + INFLATION) ** Decimal(max(0, LATE_AGE - age)),
        "late_age": LATE_AGE, "by_lifespan": by_lifespan,
        "irr_reference": next((b["irr"] for b in by_lifespan if b["to_age"] == REFERENCE_LIFESPAN), None),
        "alternatives": alternatives, "break_even_return": break_even, "break_even_bound": bound, "verdict": verdict,
        "tax_free": True,
        "assumptions": {"inflation": str(INFLATION), "withdrawal_rate": str(WITHDRAWAL_RATE),
                        "search_range": f"{SEARCH_LOW}~{SEARCH_HIGH}",
                        "alternative": "해지금과 남은 보험료를 넓은 지수펀드에 넣어 연금 개시까지 굴린 뒤 연 4%씩 인출(세금 전)",
                        "lifetime": "연금은 사망 시까지 같은 명목 금액(물가 연동 없음), 보험사 예시표의 최저보증액 기준",
                        "payout_tax": "저축성 보험 비과세 요건 충족 가정(현행 법령 확인 필요)"},
    }


def _months(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def _growth(rate: Decimal) -> Decimal:
    """Monthly growth factor of an annual rate."""
    return (1 + rate) ** (Decimal(1) / 12)


def _npv(flows: list[tuple[int, Decimal]], rate: Decimal) -> Decimal:
    """Present value of time-ordered monthly flows; discount factors are carried forward, not recomputed."""
    v = 1 / _growth(rate)
    total, factor, previous = Decimal(0), Decimal(1), 0
    for t, cf in flows:
        if t != previous:
            factor *= v ** (t - previous)
            previous = t
        total += cf * factor
    return total


def _irr(flows: list[tuple[int, Decimal]]) -> Decimal | None:
    """The annual rate at which the flows' present value is zero, by bisection; None without a sign change."""
    low, high = SEARCH_LOW, SEARCH_HIGH
    if _npv(flows, low) * _npv(flows, high) > 0:
        return None
    for _ in range(_ITERATIONS):
        mid = (low + high) / 2
        if _npv(flows, mid) > 0:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def _grown(surrender: Decimal, premium: Decimal, remaining: int, to_start: int, rate: Decimal) -> Decimal:
    """The surrender value and the remaining premiums (paid at the end of each month) invested at `rate`
    until the annuity start; a premium due after the start is counted at the start."""
    g = _growth(rate)
    value = surrender * g ** to_start
    paid_before = min(remaining, to_start)
    if paid_before:
        # Premiums 1..k grow for to_start-1 .. to_start-k months: a geometric series.
        series = Decimal(paid_before) if g == 1 else (g ** paid_before - 1) / (g - 1)
        value += premium * g ** (to_start - paid_before) * series
    return value + premium * (remaining - paid_before)


def _break_even(surrender: Decimal, premium: Decimal, remaining: int, to_start: int,
                annuity: Decimal) -> tuple[Decimal | None, str | None]:
    """(rate, bound): the alternative's return at which its 4% withdrawal equals the annuity. Outside the search
    range the bound says which side: "above" (no rate in range catches the annuity: keep) or "below" (even the
    lowest rate beats it)."""
    def gap(rate: Decimal) -> Decimal:
        return _grown(surrender, premium, remaining, to_start, rate) * WITHDRAWAL_RATE / 12 - annuity
    low, high = SEARCH_LOW, SEARCH_HIGH
    if gap(high) < 0:
        return None, "above"
    if gap(low) > 0:
        return None, "below"
    for _ in range(_ITERATIONS):
        mid = (low + high) / 2
        if gap(mid) < 0:
            low = mid
        else:
            high = mid
    return (low + high) / 2, None
