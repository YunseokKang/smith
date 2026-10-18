"""Deterministic proposals: calculators plus rules that turn the household's numbers into concrete,
dated suggestions (docs/report-design.md §10.2, §10.5).

Every amount here is computed in code from one ledger snapshot. The narrative stage (6c) may later
reorder and explain these proposals with policy and market briefs, but it never invents an amount.
Text is formal Korean addressed to the client; terms are explained where they first appear.
"""
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Any

from smith import future_income, holdings, tax
from smith.fmt import percent, short_won
from smith.payload import LedgerView, base_amount
from smith.realestate import MIN_BASIS
from smith.records import Kind
from smith.summary import recurring_active

# Assumptions and tax constants are explicit so the reader can question them.
INTEREST_TAX = Decimal("0.154")           # Interest income tax incl. local tax (Korea, standard rate).
PENSION_CREDIT_LIMIT = Decimal(9_000_000)  # Pension savings + IRP combined annual credit base (2023 rule).
PENSION_SAVINGS_LIMIT = Decimal(6_000_000)
PENSION_CREDIT_RATE = Decimal("0.132")     # Above 55M won gross salary; 16.5% at or below.
DECISION_LEAD_MONTHS = 6                    # Decide how to return a lease deposit this long before it is due.
LEASE_HORIZON_MONTHS = 48
CD_SERIES, BASE_RATE_SERIES = "817Y002/010502000", "722Y001/0101000"
# Differences smaller than this are within the error of approximating deposit rates by the CD rate.
DECISION_MARGIN = Decimal("0.005")
# Loans an extra payment can reduce (a lease deposit is returned, not prepaid).
REPAYABLE = ("mortgage", "jeonse_loan", "credit_loan", "credit_line", "policy_loan", "other")
_PENSION_ACCOUNTS = ("pension_savings", "irp")


@dataclass(frozen=True)
class Proposal:
    key: str           # Stable rule id; the proposal ledger (§10.6) tracks follow-up by it.
    priority: int      # 1 = act now, 2 = this month or quarter, 3 = keep or review.
    title: str         # One actionable sentence.
    why: str           # Why now: the outside change plus this household's numbers.
    effect: str        # Expected effect, with computed amounts.
    risks: str         # Risks and the alternative, including doing nothing.
    timing: str
    reconsider: str    # The condition that would change this judgement.
    certainty: str     # 계산 / 가정 / 세법 기준(확인 필요) / 자료 요청
    figures: dict[str, Decimal] = field(default_factory=dict)  # Inputs for verification (6d).
    # Follow-up lifecycle (§10.6): the identity names what a decision is about (for example the lease due
    # date or the tax year); a decision stands until the identity changes or a watched figure moves by more
    # than `tolerance` (relative) from the figures the proposal was raised on.
    identity: str = ""
    watch: tuple[str, ...] = ()
    tolerance: Decimal = Decimal("0.2")
    times_shown: int = 0  # Delivered reports that already carried this instance.
    renewed: bool = True  # New identity or materially changed figures: a new instance when delivered.
    context: str = ""     # Market and policy context added by the narrative stage (verified, sourced).
    context_memo: str = ""  # A verification caveat on `context`, shown in the footer's system notes only.
    links: tuple[tuple[str, str], ...] = ()  # (title, https URL) sources for `context`.


@dataclass(frozen=True)
class Track:
    """One long-range goal in the strategy view: on track, needs attention, or not assessable."""
    name: str
    status: str        # on_track / attention / unknown
    headline: str
    detail: str


@dataclass(frozen=True)
class Undetermined:
    """A judgement a rule could not make because an input it needs is unknown. This is not "nothing to
    do": the report must not reassure the client about it."""
    topic: str     # What could not be judged.
    missing: str   # Which input is unknown.
    key: str = ""  # The rule (proposal key) that could not judge; set by `build`, used by the daily editions.


# The proposal rules in `build`, in order. A rule's proposals and undetermined judgements carry its key.
RULE_KEYS = ("lease-return", "emergency-reserve", "goal-stocks-glide", "concentration-trim", "surplus-plan",
             "pension-credit", "overseas-harvest", "isa-open", "insurance-review", "prepayment", "variable-rate",
             "spending-room")
SPENDING_FILL_MONTHS = 12  # Assumption (§16.3): an emergency-reserve gap is filled within a year.
# Assumption (§17.1): when assets sellable within days are at least this many times the reserve gap, the gap
# is filled by moving money between assets once, not by diverting the monthly surplus for months.
SWAP_MULTIPLE = Decimal(3)
# Assumption (§17.5): premiums above this share of monthly income are a cash-flow target; a premium at or
# above the savings-type threshold whose tax-free maturity (10 years) falls after the home move is a mismatch.
INSURANCE_SHARE_LIMIT = Decimal("0.10")
SAVINGS_PREMIUM_MIN = Decimal(500_000)
SAVINGS_INSURANCE_YEARS = 10


def build(view: LedgerView, kpis: dict[str, Any], sectors: dict[str, Any],
          active: dict[str, dict[str, Any]] | None = None, household: dict[str, Any] | None = None,
          pay: list[future_income.Inflow] | None = None, review: dict[str, Any] | None = None,
          lease_plans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return proposals (most urgent first), the strategy tracks and the follow-up of earlier proposals.

    `active` maps proposal identities to their current instance (ledger.active_proposals). Unless the
    proposal is renewed, a declined or done instance is not raised again, an accepted one moves to
    `in_progress`, and an open one carries how often it was shown. `review` is holdings.review (computed
    here when absent) and `lease_plans` smith.lease.plans (none when absent: the lease rule then falls back
    to the whole deposit).
    """
    facts = _facts(view, kpis, sectors, review, lease_plans or [])
    facts["tax"] = tax.facts(view, facts["today"], household)
    facts["pay"] = pay or []  # Expected pay (future_income.inflows): shown beside the long-range tracks only.
    facts["glide"] = _glide(facts)
    facts["reserve_swap"] = reserve_swap(facts)
    facts["spending_room"] = spending_room(facts)
    rules = (_lease_return, _emergency_reserve, _goal_stocks_glide, _concentration_trim, _surplus_plan, _pension_credit,
             _overseas_harvest, _isa, _insurance_review, _prepayment, _variable_rate, _spending_room)
    outcomes = [(i, rule(facts)) for i, rule in enumerate(rules)]
    found = [(i, p) for i, p in outcomes if isinstance(p, Proposal)]
    undetermined = [replace(p, key=RULE_KEYS[i]) for i, p in outcomes if isinstance(p, Undetermined)]
    ranked = [p for _, p in sorted(found, key=lambda item: (item[1].priority, item[0]))]
    active = active or {}
    proposals, in_progress = [], []
    for proposal in ranked:
        proposal = replace(proposal, identity=proposal.identity or proposal.key)
        current = active.get(proposal.identity)
        if current is None or materially_changed(proposal, current["figures"]):
            proposals.append(replace(proposal, renewed=True))
        elif current["status"] == "accepted":
            in_progress.append(proposal)
        elif current["status"] == "open":
            proposals.append(replace(proposal, times_shown=current["times_shown"], renewed=False))
    decided = [item for item in active.values() if item["status"] in ("declined", "done")]
    return {"proposals": proposals, "in_progress": in_progress, "decided": decided, "undetermined": undetermined,
            "strategy": _strategy(facts), "glide": facts["glide"], "spending_room": facts["spending_room"],
            "reserve_swap": facts["reserve_swap"], "concentration": facts["concentration"],
            "insurance_review": insurance_review(facts),
            "assumptions": _assumptions() + tax.assumptions(),
            "tax_notes": tax.notes(facts["tax"]), "tax_checklist": tax.checklist(facts["tax"])}


def materially_changed(proposal: Proposal, anchor: dict[str, str]) -> bool:
    """True when a watched figure moved by more than the tolerance from the anchor (the figures the
    instance was raised on). Comparing with the anchor, not the last report, adds hysteresis."""
    for name in proposal.watch:
        if name not in proposal.figures:
            continue
        if name not in anchor:
            return True
        base, now = Decimal(anchor[name]), proposal.figures[name]
        if base == 0:
            if now != 0:
                return True
        elif abs(now - base) / abs(base) > proposal.tolerance:
            return True
    return False


def snapshot(proposal: Proposal) -> dict[str, Any]:
    """What the ledger keeps about a delivered proposal."""
    return {"key": proposal.key, "identity": proposal.identity or proposal.key, "renewed": proposal.renewed,
            "figures": {name: str(value) for name, value in proposal.figures.items()}}


# --- facts ------------------------------------------------------------------------------------------------

def _facts(view: LedgerView, kpis: dict[str, Any], sectors: dict[str, Any], review: dict[str, Any] | None,
           lease_plans: list[dict[str, Any]]) -> dict[str, Any]:
    summary = view.summary
    today = view.as_of.date()
    plans = {p["record_id"]: p for p in lease_plans}
    leases = []
    for record in view.records:
        f = record.fields
        if record.kind is Kind.LIABILITY and f["category"] == "lease_deposit_obligation" and f.get("maturity"):
            due = date.fromisoformat(f["maturity"])
            months = max(1, _months_between(today, due))  # Due later this month: one month away, never dropped.
            if today < due and months <= LEASE_HORIZON_MONTHS:
                market = next((p.get("market") for p in sectors["real_estate"]["properties"]
                               if p["record_id"] == f.get("collateral_record_id")), None)
                amount = base_amount(view, record)
                plan = plans.get(record.record_id)
                leases.append({"due": due, "months": months, "amount": amount, "owner": record.owner_id,
                               "jeonse": None if market is None else market["jeonse"],
                               "jeonse_basis": 0 if market is None else market["jeonse_basis"],
                               # The household's own cash in the default (re-let) scenario; the whole deposit
                               # when the market level is unknown or no plan was computed.
                               "need": amount if plan is None else plan["need"], "plan": plan})
    review = holdings.review(view) if review is None else review
    # Inputs come from report_data, where a total that includes an unconverted amount is None.
    # Rules skip or say "unknown" rather than turn a partial total into a confident figure.
    liquidity = sectors["cash"]["liquidity"]
    immediate, days = liquidity.get("immediate"), liquidity.get("days")
    liquid = None if immediate is None or days is None else immediate + days
    pension = [base_amount(view, r) for r in view.records
               if r.kind is Kind.ASSET and r.fields.get("account_type") in _PENSION_ACCOUNTS]
    pension_total = None if not pension or any(v is None for v in pension) else sum(pension, Decimal(0))
    return {"today": today, "summary": summary, "kpis": kpis, "sectors": sectors, "leases": leases,
            "liquid": liquid, "surplus": kpis["monthly_net"],
            # What can actually pile up as cash: the surplus minus standing transfers (for example pension
            # contributions, which become restricted money).
            # Money moved to restricted accounts (pensions) cannot fund a deposit return or a home; money
            # moved to liquid accounts still can. Unallocated surplus subtracts every standing transfer.
            "available": None if kpis["monthly_net"] is None else kpis["monthly_net"] - _restricted_transfers(view),
            "unallocated": None if kpis["monthly_net"] is None else kpis["monthly_net"] - summary.monthly_transfers,
            "pension_accounts": len(pension),
            "pension_total": pension_total, "cd": _series(view, CD_SERIES),
            "base_rate": _series(view, BASE_RATE_SERIES),
            "resources": holdings.owner_resources(view),
            "review": review, "concentration": holdings.concentration_plan(review)}


def _restricted_transfers(view: LedgerView) -> Decimal:
    """Monthly transfers into restricted (for example pension) accounts."""
    restricted = {r.record_id for r in view.records if r.kind is Kind.ASSET and r.fields["liquidity"] == "restricted"}
    total = Decimal(0)
    for r in view.records:
        f = r.fields
        if r.kind is Kind.CASHFLOW and f["category"] == "internal_transfer" and f.get("target_record_id") in restricted \
                and f["frequency"] in ("monthly", "quarterly", "annual") and recurring_active(f, view.as_of.date()):
            amount = base_amount(view, r)
            total += Decimal(0) if amount is None else amount / {"monthly": 1, "quarterly": 3, "annual": 12}[f["frequency"]]
    return total


def _series(view: LedgerView, series_id: str) -> dict[str, Any] | None:
    for row in view.evidence:
        if row["spec"].series_id == series_id and row["value"] is not None:
            return row
    return None


def _months_between(start: date, end: date) -> int:
    # Calendar-month difference, the same count the timeline and the home calculator show.
    return (end.year - start.year) * 12 + end.month - start.month


def _accumulated(facts: dict[str, Any], months: int) -> Decimal | None:
    """Liquid assets plus the monthly surplus saved until then; unknown if either input is unknown."""
    if facts["liquid"] is None or facts["available"] is None:
        return None
    return facts["liquid"] + max(facts["available"], Decimal(0)) * months


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, min(day.day, 28))


# --- rules ------------------------------------------------------------------------------------------------

def _lease_return(facts: dict[str, Any]) -> Proposal | Undetermined | None:
    """The deposit due is returned mostly by the next tenant's deposit (report design §17.2): the proposal
    sizes the household's own cash in that default scenario, and shows the sale and the loan beside it."""
    if not facts["leases"]:
        return None
    lease = min(facts["leases"], key=lambda item: item["due"])
    amount, months, surplus, need = lease["amount"], lease["months"], facts["available"], lease["need"]
    if amount is None or need is None:
        return Undetermined("전세보증금 반환 계획", "보증금 금액을 원화로 환산하지 못했습니다(환율 없음).")
    plan = lease["plan"]
    relet = plan is not None and plan["default"] == "relet"
    accumulated = _accumulated(facts, months)
    coverage = accumulated / need if accumulated is not None and need else None
    deadline = _add_months(lease["due"], -DECISION_LEAD_MONTHS)
    short = coverage is not None and coverage < 1
    priority = 1 if (months <= 12 and short) or (short and months <= 24 and not relet) else 2
    home = facts["kpis"]["home_progress"]
    overlap = (" 같은 자산과 여유 자금을 주택 이전 목표 계산에도 쓰고 있어, 두 목표가 같은 돈을 나눠 써야 합니다."
               if home else "")
    if accumulated is None:
        readiness = (" 일부 자산이나 현금흐름을 원화로 환산하지 못해 만기 때 준비할 수 있는 금액은 계산하지 않았습니다.")
    elif need == 0:
        readiness = " 지금 시세라면 직접 마련하실 돈은 없고, 안전 버퍼만 준비하시면 됩니다."
    else:
        readiness = (f" 지금 며칠 안에 현금으로 바꿀 수 있는 자산 {short_won(facts['liquid'])}에 매월 여유 "
                     f"{short_won(surplus)}이 쌓인다고 보면 만기 때 약 {short_won(accumulated)}이 되어, 준비하실 돈의 "
                     f"{percent(coverage, 0)}입니다.{overlap}")
    jeonse, gap = lease["jeonse"], None
    market = ""
    if relet:
        gap = plan["relet"]["gap"]
        market = (f" 같은 단지·같은 면적의 최근 6개월 전세 계약 {lease['jeonse_basis']}건의 중간값은 {short_won(jeonse)}입니다. "
                  f"새 세입자를 받으면 그 보증금이 기존 보증금을 대신하므로, 고객님이 직접 준비하실 돈은 차액 "
                  f"{short_won(gap)}에 안전 버퍼 {short_won(plan['relet']['buffer'])}을 더한 약 {short_won(need)}입니다."
                  + _lease_alternatives(plan))
    elif jeonse is not None:
        # One or two contracts: shown for reference, but too few to put a gap figure on.
        market = (f" 같은 단지·같은 면적의 최근 6개월 전세 계약은 {lease['jeonse_basis']}건뿐이라 중간값 {short_won(jeonse)}은 "
                  "참고용으로만 보시고, 보증금 전액을 보수적으로 잡았습니다." + ("" if plan is None else _lease_alternatives(plan)))
    title = (f"{lease['due']:%Y년 %m월} 돌려드릴 전세보증금 {short_won(amount)}은 새 세입자 승계가 기본이고, 직접 준비하실 돈은 "
             f"약 {short_won(need)}입니다. 반환 방법을 {deadline:%Y년 %m월}까지 정해 두시길 권합니다." if relet else
             f"{lease['due']:%Y년 %m월} 돌려드려야 할 전세보증금 {short_won(amount)}의 반환 방법을 "
             f"{deadline:%Y년 %m월}까지 정해 두시길 권합니다.")
    figures = {"amount": amount, "months": Decimal(months), "need": need,
               **({} if accumulated is None else {"accumulated": accumulated}),
               **({} if gap is None else {"jeonse_gap": gap})}
    if plan is not None:
        sale, loan = plan["sale"], plan["loan"]
        if sale["cash_out"] is not None:
            figures["sale_net_inflow"] = -sale["cash_out"]
        if loan["amount"] is not None:
            figures["loan_capacity"] = loan["amount"]
    return Proposal(
        key="lease-return", priority=priority, identity=f"lease-return:{lease['due']}",
        watch=("need", "accumulated"), tolerance=Decimal("0.15"),
        title=title,
        why=(f"전세보증금은 계약이 끝나면 세입자에게 돌려줘야 하는, 날짜가 정해진 큰 돈입니다(남은 기간 {months}개월). 다만 보증금 "
             "전액이 고객님 주머니에서 나가는 것은 아닙니다." + market + readiness),
        effect=("반환 방법(새 세입자 승계, 재계약, 매도, 퇴거자금대출)을 미리 정해 두면 만기 직전에 주식을 불리한 시세에 팔거나 "
                "급히 대출을 받는 일을 피하고, 월 여유 자금을 보증금에 묶지 않을 수 있습니다."),
        risks=("새 세입자의 보증금이 지금 시세보다 낮으면 차액이 커지고, 새 세입자가 늦게 들어오면 그 사이 보증금을 먼저 돌려줘야 "
               "할 수 있습니다. 아무것도 정하지 않으면 만기 무렵의 시세와 금리에 따라 선택지가 좁아집니다."),
        timing=f"{deadline:%Y.%m}까지 방법 결정(만기 {DECISION_LEAD_MONTHS}개월 전)",
        reconsider="주변 전세 시세가 크게 바뀌거나, 재계약·매도 여부가 정해지면 다시 계산합니다.",
        certainty=("계산(현재 잔액과 현금흐름이 유지된다는 가정) + 국토교통부 실거래가 + 가정(안전 버퍼 "
                   f"{percent(plan['assumptions']['relet_buffer_rate'], 0)})" if relet else
                   "계산(현재 잔액과 현금흐름이 유지된다는 가정, 보증금 전액 기준)"),
        figures=figures)


def _lease_alternatives(plan: dict[str, Any]) -> str:
    """The sale and the loan scenarios in one or two sentences, with their figures when known."""
    sale, loan = plan["sale"], plan["loan"]
    parts = []
    if sale["cash_out"] is not None:
        inflow = -sale["cash_out"]
        taxed = sale["net_inflow"]
        parts.append(f" 전세를 끼고 팔거나 만기에 맞춰 파는 경우에는 돈이 나가지 않고, {sale['price_basis']}에서 보증금과 담보대출"
                     + (f", 양도세를 빼면 약 {short_won(inflow)}이 들어옵니다(1세대 1주택 비과세 요건을 갖추면 약 "
                        f"{short_won(taxed['exempt'])}, 현행 법령 확인 필요)." if taxed else
                        f"을 빼면 약 {short_won(inflow)}이 들어옵니다(취득가를 몰라 양도세는 빼지 않았습니다)."))
    if loan["amount"] is not None:
        parts.append(f" 퇴거자금대출은 소득 대비 상환 비율 기준으로 약 {short_won(loan['amount'])}까지 가능할 수 있지만, "
                     "확정 승인이 아니라 가정입니다(현행 규정 확인 필요).")
    elif loan["basis"].startswith("이 명의"):
        parts.append(" 퇴거자금대출 한도는 이 명의의 소득 기록이 없어 계산하지 않았습니다.")
    return "".join(parts)


def reserve_swap(facts: dict[str, Any]) -> dict[str, Any] | None:
    """Whether the emergency-reserve gap is filled in one step by moving money between assets (report design
    §17.1): assets sellable within days (Hermes-managed ones never count) are at least SWAP_MULTIPLE times the
    gap. The natural source is the excess of any individual stock above the single-company limit; the rest
    comes from holdings with an exit or trim opinion. None when there is no gap or an input is unknown."""
    cash = facts["sectors"]["cash"]
    gap, days = cash["reserve_gap"], cash["liquidity"].get("days")
    if gap is None or gap == 0 or days is None:
        return None
    plan = facts["concentration"]
    excess = Decimal(0) if plan is None or plan.get("unknown") else plan["excess_total"]
    return {"gap": gap, "sellable_within_days": days, "concentration_excess": excess,
            "multiple": days / gap, "one_step": days >= gap * SWAP_MULTIPLE}


def _emergency_reserve(facts: dict[str, Any]) -> Proposal | Undetermined | None:
    cash = facts["sectors"]["cash"]
    gap, surplus = cash["reserve_gap"], facts["available"]
    if gap == 0:  # Met.
        return None
    if gap is None or surplus is None:
        return Undetermined("비상금", "현금이나 월 현금흐름 가운데 원화로 환산하지 못한 금액이 있습니다.")
    months_cover = facts["kpis"]["immediate_months"]
    fill_months = None if surplus <= 0 else (gap / surplus).to_integral_value(rounding=ROUND_CEILING)
    priority = 1 if months_cover is not None and months_cover < 1 else 2
    days = cash["liquidity"].get("days")  # Hermes-managed assets are not counted (never proposed for sale).
    swap = facts["reserve_swap"]
    if swap is not None and swap["one_step"]:
        return _reserve_by_swap(facts, swap, priority, months_cover)
    return Proposal(
        key="emergency-reserve", priority=priority, watch=("gap", "target"), tolerance=Decimal("0.25"),
        title=f"바로 꺼내 쓸 수 있는 비상금을 {short_won(cash['reserve_target'])}까지 {short_won(gap)} 더 마련하시길 권합니다.",
        why=(f"비상금은 실직·질병처럼 예상하지 못한 일에 대비해 손해 없이 바로 꺼낼 수 있는 돈입니다. 지금은 월 지출의 "
             f"{'미상' if months_cover is None else f'{months_cover:.1f}개월'}분({short_won(cash['immediate'])})입니다. "
             f"주식 등 며칠 안에 팔 수 있는 자산({short_won(days)})도 있지만, 시세가 떨어진 때 팔아야 할 수 있습니다."),
        effect=(f"매월 여유 {short_won(surplus)}을 먼저 비상금에 넣으면 약 {fill_months}개월이면 채워집니다."
                if fill_months else "매월 여유 자금이 없어 다른 자산을 옮겨 채워야 합니다."),
        risks=("비상금은 수익이 낮습니다. 수시로 넣고 빼면서 이자가 붙는 계좌(CMA·파킹통장)에 두면 손해를 줄일 수 있습니다. "
               "기준을 6개월보다 낮추면 마련할 금액도 줄어듭니다."),
        timing="다음 월급일부터",
        reconsider="월 지출이 바뀌거나, 소득이 더 안정적이라 기준 개월 수를 낮추기로 하시면 다시 계산합니다.",
        certainty=f"가정(비상금 기준 월 지출 {cash['emergency_months_assumption']}개월)",
        figures={"gap": gap, "target": cash["reserve_target"]})


def _reserve_by_swap(facts: dict[str, Any], swap: dict[str, Any], priority: int, months_cover: Decimal | None) -> Proposal:
    """The one-step fill: sell the excess above the single-company limit (or holdings with an exit opinion) for
    the gap, and leave the monthly surplus to its investing and spending. A total-balance-sheet view, not a
    mental-accounting one (report design §17.1)."""
    cash, surplus = facts["sectors"]["cash"], facts["available"]
    gap, excess = swap["gap"], swap["concentration_excess"]
    source = (f" 한 종목 한도({percent(holdings.CONCENTRATION_LIMIT, 0)})를 넘는 금액이 {short_won(excess)}이라, 그 초과분부터 "
              "정리하면 비상금 마련과 집중 완화를 한 번에 해결합니다." if excess >= gap else
              (f" 한 종목 한도를 넘는 초과분 {short_won(excess)}을 먼저 쓰고, 나머지는 정리 의견이 있는 종목에서 채웁니다."
               if excess > 0 else " 정리 의견이 있는 종목(손실 종목, 역할이 겹치는 종목)부터 정리해 채웁니다."))
    return Proposal(
        key="emergency-reserve", priority=priority, watch=("gap", "target"), tolerance=Decimal("0.25"),
        title=(f"비상금 부족분 {short_won(gap)}은 월급에서 떼지 말고, 보유 주식 가운데 비중이 과한 종목이나 정리할 종목을 "
               f"{short_won(gap)}만큼 정리해 한 번에 채우시길 권합니다."),
        why=(f"비상금은 실직·질병처럼 예상하지 못한 일에 대비해 손해 없이 바로 꺼낼 수 있는 돈입니다. 지금은 월 지출의 "
             f"{'미상' if months_cover is None else f'{months_cover:.1f}개월'}분({short_won(cash['immediate'])})으로 기준 "
             f"{short_won(cash['reserve_target'])}에 {short_won(gap)}이 모자랍니다. 그런데 며칠 안에 팔 수 있는 주식·펀드가 "
             f"{short_won(swap['sellable_within_days'])}으로 부족분의 {swap['multiple']:.0f}배가 넘습니다. 통장 하나만 보고 월 여유 자금을 "
             "몇 달씩 비상금에 묶는 것은 자산 전체를 보지 않는 계산입니다." + source),
        effect=(f"비상금은 이번 달 안에 채워지고, 매월 여유 {short_won(surplus)}은 비상금에 묶이지 않은 채 정기 투자 적립과 "
                "안심 소비 한도로 그대로 남습니다."),
        risks=("주식을 팔 때는 시세와 세금(해외 종목은 양도세와 연 기본공제, 현행 법령 확인 필요), 수수료가 따릅니다. 시세가 낮을 때 "
               "파는 것이 아깝다면 두세 번에 나눠 옮기셔도 되지만, 비상금은 수익을 내는 돈이 아니라 보험입니다. 비상금은 수시로 "
               "넣고 빼면서 이자가 붙는 계좌(CMA·파킹통장)에 두십시오."),
        timing="이번 달 안에 한 번",
        reconsider="월 지출이 바뀌거나, 팔 수 있는 자산이 크게 줄거나, 기준 개월 수를 바꾸기로 하시면 다시 계산합니다.",
        certainty=(f"계산(현재 평가액) + 가정(비상금 기준 월 지출 {cash['emergency_months_assumption']}개월, 매도 가능 자산이 부족분의 "
                   f"{SWAP_MULTIPLE}배 이상이면 일시 충당)"),
        figures={"gap": gap, "target": cash["reserve_target"], "sellable_within_days": swap["sellable_within_days"],
                 "concentration_excess": excess})


def _glide(facts: dict[str, Any]) -> dict[str, Any] | None:
    """Dated needs within the horizon that an owner pays from their own money, and how much of them rests on
    that owner's stocks (holdings.glide_plan). A lease deposit belongs to the owner of the obligation and is
    net of a new tenant's deposit when enough recent contracts show the market level; the home goal is the
    client's (the home calculation is self-only) and is what liquid money must add to the home equity.
    Another member's money is never assumed to pay the client's goal, nor the other way round."""
    needs = []
    for lease in facts["leases"]:
        if lease["amount"] is None:
            continue
        own = lease["amount"]
        if lease["jeonse"] is not None and lease["jeonse_basis"] >= MIN_BASIS:
            own = max(Decimal(0), own - lease["jeonse"])
        label = "전세보증금 반환" + ("" if lease["owner"] == "self" else "(가족 명의)")
        needs.append(holdings.Need(label, lease["due"], own, lease["owner"]))
    home = facts["kpis"]["home_progress"]
    if home and home.get("from_liquid") is not None:
        needs.append(holdings.Need("주택 이전", home["target_date"], home["from_liquid"], "self"))
    return holdings.glide_plan(today=facts["today"], needs=needs, resources=facts["resources"])


_GLIDE_RECONSIDER = {"전세보증금 반환": "보증금을 새 세입자 보증금이나 재계약으로 돌려드리기로 정하시면 그 부분은 빠집니다. ",
                     "주택 이전": "주택 이전 시기·금액이나 자금 조달 방법(대출, 배우자 부동산)이 정해지면 다시 계산합니다. "}


def _goal_stocks_glide(facts: dict[str, Any]) -> Proposal | Undetermined | None:
    """Money needed at a known date within three years should not depend on the stock price that day:
    move the part that rests on stocks to safe assets in quarterly steps (report design §12.4)."""
    plan = facts["glide"]
    if plan is None:
        return None
    if not plan["steps"]:
        return Undetermined("목표 자금 안의 주식", "주식·현금·월 현금흐름 가운데 원화로 환산하지 못한 금액이 있습니다.")
    first, rest = plan["steps"][0], plan["steps"][1:]
    later = "".join(f" {s['due']:%Y년 %m월} {s['label']}에도 주식 {short_won(s['from_stocks'])}이 필요하며, "
                    f"{s['start']:%Y년 %m월}부터 분기마다 약 {short_won(s['per_quarter'])}씩 옮기는 계획입니다." for s in rest)
    begin = "부터 분기마다" if first["started"] else "부터(아직 시작 전) 분기마다"
    return Proposal(
        key="goal-stocks-glide", priority=2 if first["started"] else 3, identity=f"goal-stocks-glide:{first['due']}",
        watch=("earmarked",), tolerance=Decimal("0.25"),
        title=(f"{first['due']:%Y년 %m월} {first['label']}에 쓸 돈 가운데 약 {short_won(first['from_stocks'])}이 주식에 "
               f"있습니다. {first['start']:%Y년 %m월}{begin} 약 {short_won(first['per_quarter'])}씩 예금·단기채 같은 안전 자산으로 "
               "옮기시길 권합니다."),
        why=(f"Smith는 {first['label']} 자금 계획에 주식을 지금 평가액 그대로 넣어 계산합니다. 지금 현금과 그때까지 모일 여유 "
             f"자금만으로는 모자라, 그 날짜에 주식을 팔아야 하는 금액이 약 {short_won(first['from_stocks'])}입니다. 주식은 한 해에 "
             f"{percent(plan['drawdown'], 0)} 넘게 떨어지는 해도 있어, 그때 팔게 되면 계획보다 약 {short_won(plan['stress'])}이 "
             "모자랍니다. 날짜가 정해진 돈은 날짜가 가까워질수록 값이 덜 흔들리는 곳에 두는 것이 원칙입니다." + later),
        effect=(f"목표 {holdings.GLIDE_END_MONTHS}개월 전까지 나눠 옮겨 두면, 그 사이 주가가 떨어져도 계획이 흔들리지 않습니다. "
                + (f"나머지 주식 {short_won(plan['free'])}은 장기 성장 자산으로 그대로 둡니다. " if plan["free"] > 0 else
                   "지금 보유한 주식 전부가 이 목표 자금에 해당합니다. 장기 성장 투자는 목표 이후에 쓸 돈(연금 계좌 등)에서 "
                   "이어 가시는 것이 맞습니다. ")
                + "한 번에 팔지 않고 나눠 옮기므로 특정 시점의 주가에 덜 좌우됩니다."),
        risks=("옮긴 뒤 주가가 오르면 그만큼의 이익을 놓칩니다. 공격형 성향을 우선하신다면 옮기는 금액을 줄이는 대신, 부족할 때 쓸 "
               "다른 재원(재계약, 대출 한도)을 미리 확인해 두는 방법도 있습니다. 미국 주식은 한 해 이익이 "
               f"{short_won(tax.OVERSEAS_DEDUCTION)}을 넘으면 넘는 부분에 양도세 {percent(tax.OVERSEAS_RATE, 0)}가 붙으므로, 손실 종목이나 국내 종목부터 "
               "팔거나 해마다 공제 한도 안에서 나눠 파는 편이 세금에 유리합니다(현행 법령 확인 필요)."),
        timing=f"{first['start']:%Y.%m}~{first['end']:%Y.%m}, 분기마다 {first['quarters']}번",
        reconsider=("".join(dict.fromkeys(_GLIDE_RECONSIDER[s["label"]] for s in plan["steps"]))
                    + "월 여유 자금이 크게 달라지거나 주식 평가액이 크게 바뀌면 다시 계산합니다."),
        certainty=(f"계산(현재 평가액·현금흐름 유지 가정) + 가정(주가 {percent(plan['drawdown'], 0)} 하락 스트레스, "
                   "주택은 기준 시나리오)"),
        figures={"earmarked": plan["earmarked"], "from_stocks": first["from_stocks"],
                 "per_quarter": first["per_quarter"], "stress": plan["stress"]})


def _concentration_trim(facts: dict[str, Any]) -> Proposal | Undetermined | None:
    """An individual stock above the single-company limit is sold down to it in quarterly steps into the
    broad-index core (report design §17.4). When the stock is the employer's, the client's salary and share
    grants already ride on that company: human capital and financial assets are concentrated twice, so this
    is the first investment proposal."""
    plan = facts["concentration"]
    if plan is None:
        return None
    if plan["unknown"]:
        return Undetermined("집중 종목 정리", "증권 가운데 원화로 환산하지 못한 금액이 있어 종목 비중을 계산하지 못했습니다.")
    first = plan["positions"][0]
    limit, quarters = percent(plan["limit"], 0), plan["quarters"]
    employer = first["employer"]
    grants = any(i.in_stock and i.owner_id == "self" and i.symbol == first["symbol"] for i in facts["pay"])
    double = ("" if not employer else
              " 월급" + ("과 예정된 주식 보상" if grants else "") + "도 같은 회사에서 나오므로, 인적 자본(앞으로 벌 소득)과 금융 자산이 "
              "한 회사에 겹쳐 있습니다(이중 집중). 회사 실적이 꺾이면 소득과 자산이 같은 날 함께 흔들립니다.")
    others = "".join(f" {p['name']}도 한도를 {short_won(p['excess'])} 넘어 같은 방식으로 분기마다 약 {short_won(p['per_quarter'])}씩 "
                     "정리합니다." for p in plan["positions"][1:])
    grant_rule = (" 앞으로 받는 자사주 보상은 비중이 한도 안으로 내려올 때까지 받는 즉시 전부 지수펀드로 분산하는 원칙을 권합니다."
                  if employer else "")
    return Proposal(
        key="concentration-trim", priority=1 if employer else 2, identity=f"concentration-trim:{first['key']}",
        watch=("excess",), tolerance=Decimal("0.25"),
        title=(f"{first['name']} 비중이 주식의 {percent(first['share'], 0)}로 한 종목 한도 {limit}를 넘습니다. 초과분 약 "
               f"{short_won(first['excess'])}을 {quarters}분기에 걸쳐 분기마다 약 {short_won(first['per_quarter'])}씩 정리해 넓은 "
               "지수펀드(코어)로 옮기시길 권합니다."),
        why=(f"한 종목이 주식의 {limit}를 넘으면 그 회사 한 곳의 소식에 자산 전체가 크게 흔들립니다(집중). 지금 {first['name']}은 "
             f"{short_won(first['value'])}로 주식의 {percent(first['share'], 0)}입니다." + double + others),
        effect=(f"한도 안으로 들어오면 이 회사의 악재가 주식 자산에 미치는 영향이 {percent(first['share'], 0)}에서 {limit} 수준으로 "
                "줄고, 옮긴 돈은 넓은 지수펀드에서 시장 전체의 성장을 따릅니다. 실적 발표 뒤 주가가 좋은 구간에 나눠 팔면 한 시점의 "
                "주가에 덜 좌우됩니다." + grant_rule),
        risks=("정리한 뒤 그 종목이 더 오르면 그만큼의 이익을 놓칩니다. 세금은 종목의 시장에 따라 다르니(국내 상장 주식은 대주주가 아니면 "
               "양도세가 없고, 해외 종목은 연 기본공제를 넘는 이익에 양도세) 팔기 전에 확인하십시오(현행 법령 확인 필요). 회사를 깊이 "
               "이해하셔서 집중을 유지하시겠다면, 그 판단의 근거를 기록해 두시길 권합니다."),
        timing=f"{facts['today']:%Y.%m}부터 {quarters}분기, 분기마다 한 번(실적 발표 뒤)",
        reconsider="비중이 한도 아래로 내려오거나, 회사 주식 보상 계획이 바뀌거나, 한 종목 한도를 바꾸기로 하시면 다시 계산합니다.",
        certainty=f"계산(현재 평가액) + 가정(한 종목 한도 {limit}, {quarters}분기 분할)",
        figures={"excess": first["excess"], "per_quarter": first["per_quarter"], "share": first["share"],
                 "value": first["value"]})


def insurance_review(facts: dict[str, Any]) -> dict[str, Any] | None:
    """Premiums against income and the home date (report design §17.5): the share of monthly income, the
    premiums large enough to be savings-type (SAVINGS_PREMIUM_MIN a month) with their tax-free maturity year,
    and whether that maturity falls after the home move (a duration mismatch). None without premiums."""
    pi = facts["sectors"]["pension_insurance"]
    premiums, share = pi["monthly_premiums"], pi["premium_to_income"]
    if not premiums:
        return None
    home = facts["kpis"]["home_progress"]
    goal_year = None if home is None else home["target_date"].year
    savings = []
    for amount, frequency, start in facts["tax"]["premiums"]:
        monthly = None if amount is None or frequency not in ("monthly", "quarterly", "annual") else amount / {
            "monthly": 1, "quarterly": 3, "annual": 12}[frequency]
        if monthly is not None and monthly >= SAVINGS_PREMIUM_MIN:
            maturity = int(start[:4]) + SAVINGS_INSURANCE_YEARS
            savings.append({"monthly": monthly, "maturity_year": maturity,
                            "mismatch": goal_year is not None and maturity > goal_year})
    savings_total = sum((s["monthly"] for s in savings), Decimal(0))
    return {"monthly_premiums": premiums, "share_of_income": share, "limit": INSURANCE_SHARE_LIMIT,
            "over_limit": share is not None and share > INSURANCE_SHARE_LIMIT,
            "savings_type": savings, "savings_monthly": savings_total, "home_year": goal_year,
            "mismatch": any(s["mismatch"] for s in savings)}


def _insurance_review(facts: dict[str, Any]) -> Proposal | None:
    review = insurance_review(facts)
    if review is None or not (review["over_limit"] or review["mismatch"]):
        return None
    share, premiums = review["share_of_income"], review["monthly_premiums"]
    savings = review["savings_monthly"]
    over = (f"월 보험료 {short_won(premiums)}이 월 소득의 {percent(share, 0)}로 기준 {percent(INSURANCE_SHARE_LIMIT, 0)}를 넘습니다."
            if review["over_limit"] else f"월 보험료는 {short_won(premiums)}입니다.")
    mismatch = "".join(
        f" 월 {short_won(s['monthly'])}을 내는 보험이 저축성이라면 비과세 만기(가입 {SAVINGS_INSURANCE_YEARS}년, {s['maturity_year']}년)가 "
        f"주택 이전 시점({review['home_year']}년)보다 늦어, 그 돈은 가장 필요한 때 쓸 수 없습니다(기간 불일치)."
        for s in review["savings_type"] if s["mismatch"])
    title = (f"{over.rstrip('.')} 저축성 보험의 감액완납·중도인출·추가 납입 중단을 검토해 매월 현금흐름을 되찾으시길 권합니다."
             if review["over_limit"] else
             "저축성 보험의 만기가 주택 이전보다 늦습니다. 감액완납이나 추가 납입 중단으로 그 돈을 필요한 시점에 맞추시길 권합니다.")
    return Proposal(
        key="insurance-review", priority=2, watch=("premiums",), tolerance=Decimal("0.2"),
        title=title,
        why=(over + mismatch + " 보장성 보험(사고·질병에 대비하는 보험)은 유지하되, 저축성 보험(목돈을 모으는 보험)은 수익이 낮고 "
             "중간에 꺼내기 어려워 현금흐름 개선의 첫 후보입니다."),
        effect=(f"저축성으로 보이는 납입 {short_won(savings)}을 멈추면 매월 그만큼 여유가 늘어 1년에 {short_won(savings * 12)}입니다. "
                if savings else "저축성 납입을 줄이면 그만큼 매월 여유가 늘어납니다. ")
               + "감액완납(더 내지 않고 보장·적립을 줄여 유지)은 계약을 지키면서 납입을 멈추고, 중도인출은 계약을 유지한 채 일부를 꺼냅니다.",
        risks=("중도 해지는 환급금 손실과 비과세 혜택 상실이 함께 생기므로 가장 마지막 선택입니다. 보험 종류(보장성·저축성)와 "
               "해약환급금을 알려 주시면 어느 계약부터 손볼지 정확히 계산해 드립니다. 보장 공백이 생기지 않는지 함께 확인하십시오."),
        timing="다음 보험료 납입일 전까지 보험사 앱에서 계약 종류와 환급금 확인",
        reconsider="보험료가 바뀌거나, 보험 종류와 해약환급금이 확인되거나, 주택 이전 시점이 바뀌면 다시 계산합니다.",
        certainty=(f"계산(기록된 보험료) + 가정(보험료 기준 소득의 {percent(INSURANCE_SHARE_LIMIT, 0)}, 월 "
                   f"{short_won(SAVINGS_PREMIUM_MIN)} 이상이면 저축성 가능, 비과세 만기 {SAVINGS_INSURANCE_YEARS}년) + 세법 기준(현행 법령 확인 필요)"),
        figures={"premiums": premiums, **({} if share is None else {"share": share}), "savings_monthly": savings})


def _surplus_plan(facts: dict[str, Any]) -> Proposal | Undetermined | None:
    summary, surplus = facts["summary"], facts["surplus"]
    if surplus is None:
        return Undetermined("매월 남는 돈의 쓰임새", "월 현금흐름 가운데 원화로 환산하지 못한 항목이 있습니다.")
    allocated = summary.monthly_transfers
    surplus = facts["unallocated"]  # Transfers (for example pension contributions) are already allocated.
    if surplus < Decimal(500_000):
        return None
    already = (f" 이 가운데 {short_won(allocated)}은 연금 등으로 이미 매월 이체되고 있고, 나머지는" if allocated
               else " 그런데 이 돈이")
    return Proposal(
        key="surplus-plan", priority=2, watch=("surplus",),
        title=f"매월 남는 약 {short_won(surplus)}의 쓰임새를 정해 자동이체로 묶어 두시길 권합니다.",
        why=(f"수입 {short_won(summary.monthly_inflow)}에서 생활비·대출 상환·보험료 {short_won(summary.monthly_outflow)}을 "
             f"빼면 매월 {short_won(surplus + allocated)}이 남습니다.{already} 어디로 가는지(저축·투자 이체)가 기록되어 있지 않습니다. "
             "정해 두지 않은 돈은 이자가 거의 없는 통장에 머물거나 소비로 흘러가기 쉽습니다."),
        effect=(f"1년이면 {short_won(surplus * 12)}입니다. 순서는 비상금 → 전세보증금 반환 자금 → 주택 이전 자금처럼 "
                "날짜가 가까운 목표부터 채우는 것이 일반적입니다."),
        risks="자동이체 금액을 너무 크게 잡으면 생활비가 모자랄 수 있으니, 여유의 80~90% 수준에서 시작하시길 권합니다.",
        timing="다음 월급일부터",
        reconsider="실제로 이미 자동 저축·투자를 하고 계시면 그 이체를 원장에 기록해 주십시오. 이 제안은 사라집니다.",
        certainty="계산(기록된 현금흐름 기준)",
        figures={"surplus": surplus})


def _pension_credit(facts: dict[str, Any]) -> Proposal | None:
    """Fill this year's pension credit room (IRP) and split future contributions to use the limits."""
    if not facts["pension_accounts"]:
        return None
    today, plan = facts["today"], tax.pension_plan(facts["tax"])
    if not plan["irp_room"] and not plan["excess"]:
        return None
    title = (f"{today.year}년 12월 31일까지 IRP에 {short_won(plan['irp_room'])}을 넣어 세액공제 한도를 채우시길 권합니다."
             if plan["irp_room"] else "연금 납입을 연금저축과 IRP로 나눠 공제 한도를 넘는 납입을 줄이시길 권합니다.")
    excess = (f" 올해 납입 예상은 연금저축 {short_won(plan['savings_paid'])}, IRP {short_won(plan['irp_paid'])}으로, "
              f"공제 한도를 넘는 {short_won(plan['excess'])}은 올해 공제를 받지 못합니다." if plan["excess"] else "")
    split = (f" 내년부터는 월 {short_won(plan['monthly'])}을 연금저축 {short_won(plan['next_savings'])}, IRP "
             f"{short_won(plan['next_irp'])}으로 나누고, 나머지 {short_won(plan['next_rest'])}은 ISA처럼 다른 절세 계좌에 넣으면 "
             "공제를 빠짐없이 받습니다." if plan["monthly"] else "")
    return Proposal(
        key="pension-credit", priority=1 if today.month >= 10 else 2, identity=f"pension-credit:{today.year}",
        watch=("irp_room", "excess"), tolerance=Decimal("0.2"),
        title=title,
        why=("연금저축과 IRP(개인형 퇴직연금)에 넣은 돈은 합해서 연 900만 원(연금저축만은 600만 원)까지 세금을 돌려받습니다"
             "(세액공제, 공제율 13.2%: 소득세 12%와 지방소득세 1.2%를 합한 실효율)." + excess
             + (f" IRP에는 올해 {short_won(plan['irp_paid'])}이 들어가 공제 한도가 {short_won(plan['irp_room'])} 남아 있습니다."
                if plan["irp_room"] else "")),
        effect=((f"남은 한도를 채우면 세금이 최대 약 {short_won(plan['refund_gain'])} 줄어듭니다(실제 감소액은 내실 세금 "
                 "범위 안에서 정해집니다)." if plan["irp_room"] else "") + split),
        risks=("연금 계좌의 돈은 55세 전에 꺼내면 공제받은 세금을 다시 내야 합니다. 가까운 큰 지출(보증금 반환)에 쓸 돈은 넣지 "
               "마십시오. 한도를 넘게 낸 연금저축 납입분은 다음 해 공제로 돌려 신청할 수 있는지 금융회사에 확인하십시오."),
        timing=f"{today.year}.12.31까지(올해 공제분)",
        reconsider="올해 IRP에 이미 넣은 금액이 있거나 납입 계획이 바뀌면 다시 계산합니다.",
        certainty=(f"계산(정기 이체를 공제 대상 납입으로 가정) + 세법 기준 {tax.RULES_YEAR}년(현행 법령 확인 필요)"),
        figures={"irp_room": plan["irp_room"], "excess": plan["excess"], "refund_gain": plan["refund_gain"]})


def _overseas_harvest(facts: dict[str, Any]) -> Proposal | Undetermined | None:
    """Realize overseas gains up to the yearly basic deduction, so they never reach the 22% tax.
    Hermes-managed positions are left out (Smith never proposes selling them)."""
    t = facts["tax"]
    plan = tax.harvest_plan(t)
    today = facts["today"]
    if plan is None and t["us_positions"] and t["us_unknown"] == t["us_positions"]:
        return Undetermined("해외주식 양도세 기본공제 활용", "미국 주식의 매입 단가가 없어 평가이익을 계산하지 못했습니다.")
    if plan is None or plan["realize"] <= 0:
        return None
    return Proposal(
        key="overseas-harvest", priority=1 if today.month >= 11 else 3, identity=f"overseas-harvest:{today.year}",
        watch=("realize",), tolerance=Decimal("0.3"),
        title=(f"올해 주식을 팔아 이익을 낸 적이 없다면, 연말 전에 미국 주식 이익 {short_won(plan['realize'])}만큼을 "
               "팔았다가 다시 사서 해외주식 양도세 기본공제를 쓰시는 방안을 검토해 보십시오."),
        why=(f"해외 주식(과세 대상 국내 주식 포함)은 한 해 이익 {short_won(tax.OVERSEAS_DEDUCTION)}까지 세금이 없고, 넘는 부분에 "
             f"22%가 붙습니다. 지금 이익이 난 미국 주식의 평가이익은 약 {short_won(plan['gains'])}입니다"
             + (f"(손실 중인 종목 {short_won(-plan['losses'])}은 팔기 전까지 상계되지 않습니다)" if plan["losses"] else "")
             + ". 공제는 해마다 새로 주어지고 이월되지 않습니다."),
        effect=(f"이익이 난 종목만 {short_won(plan['realize'])}만큼 실현하고 다시 사면, 나중에 낼 세금이 최대 약 "
                f"{short_won(plan['saving'])} 줄어드는 시나리오입니다."),
        risks=("올해 이미 실현한 주식 손익을 Smith는 모릅니다. 그 손익과 합쳐 공제가 한 번만 적용되므로, 증권사 양도소득 내역을 먼저 "
               "확인하십시오. 원화 손익은 현재 환율로 계산한 근사값이며, 실제 세금은 사고판 날의 환율로 계산됩니다. 매매 수수료·환전 "
               "비용과 다시 사는 사이의 가격 변동도 있습니다."
               + (f" 매입 원가를 모르는 종목 {int(plan['unknown'])}개는 계산에서 빠졌습니다." if plan["unknown"] else "")
               + (" Hermes가 운용하는 종목은 매도 대상에서 뺐지만, 그 계좌에서 올해 실현한 이익도 같은 기본공제를 함께 "
                  "쓰므로 Hermes와 상의해 확인하십시오." if t.get("hermes_managed") else "")),
        timing=f"{today.year}년 12월 결제일 기준 마감 전(증권사 해외주식 연말 결제 일정 확인)",
        reconsider="올해 해외 주식 매도 이익이 이미 있거나, 장기 보유 종목의 비중을 바꿀 계획이면 다시 계산합니다.",
        certainty=f"조건부 시나리오(올해 실현 손익 없음·현재 환율 가정) + 세법 기준 {tax.RULES_YEAR}년(현행 법령 확인 필요)",
        figures={"realize": plan["realize"], "saving": plan["saving"], "gains": plan["gains"]})


def _isa(facts: dict[str, Any]) -> Proposal | None:
    """Without an ISA, the tax-free allowance and the extra pension credit at maturity go unused."""
    t = facts["tax"]
    if t["has_isa"] or facts["surplus"] is None or facts["surplus"] <= 0:
        return None
    return Proposal(
        key="isa-open", priority=3, identity="isa-open",
        title="ISA(개인종합자산관리계좌)를 열어 연금 한도를 넘는 투자금을 그곳에서 굴리시길 권합니다.",
        why=("ISA는 3년 이상 유지하면 계좌 안에서 난 이익 중 200만 원까지 세금이 없고, 넘는 부분도 9.9%로 낮게 과세되는 "
             "계좌입니다(일반 계좌 이자·배당은 15.4%). 만기 때 연금계좌로 옮기면 옮긴 금액의 10%, 최대 300만 원까지 "
             "추가 세액공제도 받습니다. 지금은 ISA가 없습니다."),
        effect=(f"연금 한도를 넘는 투자금이나 매월 남는 여유 자금을 ISA로 돌리면 배당·이자 세금을 줄이고, 만기 후 연금 이전으로 "
                f"최대 약 {short_won(tax.ISA_TRANSFER_CREDIT_CAP * tax.CREDIT_RATE_HIGH)}의 세금을 더 돌려받을 수 있습니다."),
        risks="3년 안에 깨면 혜택이 사라집니다. 2026년 세법개정안에 ISA 개편 내용이 있어, 가입 전 확정 내용을 확인하십시오.",
        timing="개편 확정 후 또는 연내",
        reconsider="ISA 제도 개편 내용이 확정되면 다시 계산합니다.",
        certainty=f"세법 기준 {tax.RULES_YEAR}년(현행 법령 확인 필요)",
        figures={})


def _prepayment(facts: dict[str, Any]) -> Proposal | Undetermined | None:
    """The marginal effect of prepaying is set by the highest-rate loan that can be repaid, fixed or not."""
    loans = [l for l in facts["sectors"]["debt"]["loans"] if l["category"] in REPAYABLE and l["rate"] > 0]
    cd = facts["cd"]
    if not loans:
        return None
    if cd is None:
        return Undetermined("대출 추가 상환과 예금 비교", "비교 기준인 CD 금리 자료가 없습니다(금리 동기화를 확인하십시오).")
    loan = max(loans, key=lambda l: l["rate"])
    rate, label = loan["rate"], loan["label"]
    deposit = cd["value"] / 100 * (1 - INTEREST_TAX)
    example = Decimal(10_000_000)
    gap = rate - deposit
    verdict = "keep" if gap >= DECISION_MARGIN else "deposit" if gap <= -DECISION_MARGIN else "close"
    titles = {"keep": f"{label} 원금을 미리 갚는 지금의 방식을 이어가시는 것이 합리적입니다.",
              "deposit": f"{label}을 미리 갚기보다 예금·채권에 두는 편이 나은지 점검하시길 권합니다.",
              "close": f"{label} 추가 상환과 예금의 차이가 작으니, 꺼내 쓸 수 있는 쪽을 우선하셔도 됩니다."}
    effects = {"keep": "확실한 이자 절감이 예금 이자보다 뚜렷하게 큽니다. 주식 투자는 기대수익이 더 높을 수 있지만 손실 "
                       "가능성이 있어 확실한 절감과 직접 비교할 수는 없습니다.",
               "deposit": "예금 이자가 대출 이자 절감보다 뚜렷하게 커졌습니다. 다만 대출 금리도 곧 다시 정해질 수 있습니다.",
               "close": (f"차이가 {percent(DECISION_MARGIN, 1)}p보다 작아 어느 쪽도 뚜렷이 낫지 않습니다. 이럴 때는 필요할 때 "
                         "다시 꺼내 쓸 수 있는 예금이 더 유연합니다.")}
    return Proposal(
        key="prepayment", priority=2 if verdict == "deposit" else 3, identity=f"prepayment:{verdict}",
        watch=("loan_rate", "deposit_after_tax"), tolerance=Decimal("0.15"),
        title=titles[verdict],
        why=(f"갚을 수 있는 대출 중 금리가 가장 높은 것은 {label}({percent(rate)})입니다. 대출을 갚으면 그만큼의 이자를 확실히 "
             f"아끼는 '세금 없는 수익'과 같습니다. 같은 1,000만 원을 단기 예금에 두면 연 {short_won(example * deposit)}"
             f"(CD 91일물 {cd['value']}%에서 이자소득세 15.4%를 뗀 {percent(deposit)}로 근사)을 받고, 대출을 갚으면 연 "
             f"{short_won(example * rate)}의 이자를 아낍니다."),
        effect=effects[verdict],
        risks=("CD 91일물은 은행 간 단기 금리로, 실제 정기예금 금리와 다를 수 있습니다. 갚은 돈은 다시 꺼내 쓰기 어렵고, "
               "중도상환수수료는 대출 약정서를 확인하셔야 합니다. 비상금과 보증금 반환 자금을 먼저 확보하십시오."),
        timing="매월(현행 유지)" if verdict == "keep" else "다음 대출 금리 재산정 전",
        reconsider=(f"세후 예금 금리와 대출 금리({percent(rate)})의 차이가 {percent(DECISION_MARGIN, 1)}p 안팎으로 바뀌거나, "
                    "보증금 반환 자금이 부족해 보이면 다시 판단합니다."),
        certainty="계산(현재 금리가 유지된다는 가정, 예금 금리는 근사)",
        figures={"loan_rate": rate, "deposit_after_tax": deposit})


def _variable_rate(facts: dict[str, Any]) -> Proposal | Undetermined | None:
    debt, cd = facts["sectors"]["debt"], facts["cd"]
    total = debt["variable_total"]
    topic = "변동금리 대출 이자 변화"
    if total is None:
        return Undetermined(topic, "변동금리 대출 가운데 원화로 환산하지 못한 대출이 있습니다.")
    if not total:
        return None
    if cd is None or cd["change_12m"] is None:
        return Undetermined(topic, "CD 금리의 1년 변화 자료가 없습니다(금리 동기화를 확인하십시오).")
    change = cd["change_12m"] / 100
    monthly = total * change / 12
    one = next(s["monthly_increase"] for s in debt["shocks"] if s["shock"] == Decimal("0.01"))
    direction = "내렸습니다" if change < 0 else "올랐습니다" if change > 0 else "그대로입니다"
    return Proposal(
        key="variable-rate", priority=3, watch=("variable_total", "monthly_change"), tolerance=Decimal("0.3"),
        title="다음 대출 금리 재산정 때 월 이자가 얼마나 바뀔지 미리 확인해 두시길 권합니다.",
        why=(f"변동금리 대출 {short_won(total)}은 몇 달마다 시장 금리에 맞춰 이자율이 다시 정해집니다. 지난 1년 단기 시장 금리"
             f"(CD 91일물)는 {abs(cd['change_12m']):.2f}%p {direction}. 대출 금리가 같은 폭으로 바뀌면 월 이자는 약 "
             f"{short_won(abs(monthly))} {'줄어듭니다' if monthly < 0 else '늘어납니다'}."),
        effect=f"반대로 금리가 1%p 오르면 월 이자가 약 {short_won(one)} 늘어납니다. 매월 여유 자금으로 감당 가능한 범위인지 함께 보십시오.",
        risks="대출 금리가 어떤 기준(COFIX·금융채 등)을 따르는지에 따라 실제 변화 폭은 다릅니다. 대출 약정서에서 확인하실 수 있습니다.",
        timing="다음 금리 재산정일 전",
        reconsider="기준금리 결정이나 시장 금리가 0.5%p 이상 움직이면 다시 계산합니다.",
        certainty="계산(대출 금리가 시장 금리와 같은 폭으로 움직인다는 가정)",
        figures={"variable_total": total, "monthly_change": monthly})


# --- strategy ---------------------------------------------------------------------------------------------

GOAL_LABELS = {"education": "교육", "major_purchase": "큰 지출", "debt_repayment": "부채 상환", "other": "기타"}
NEAR_COMMITMENT_MONTHS = 12  # A contractual payment due within this long that the surplus cannot reach: act now.


def spending_room(facts: dict[str, Any]) -> dict[str, Any] | None:
    """Guilt-free spending room (report design §16.3), in two figures.

    `room` is the committed basis: the unallocated monthly surplus minus what the emergency reserve
    (filled within SPENDING_FILL_MONTHS) and contractual payments (the nearest lease deposit, net of a new
    tenant's deposit at the market level and of liquid money) need every month. `room_with_goals` also
    keeps the planned dated goals at their current pace: the home goal (what the base scenario's plan
    already saves every month plus its gap spread over the months left, plus the liquid money the lease
    consumed first) and every other dated goal (its whole target spread over the months left, since no
    money is recorded as set aside for it). Each need is floored at zero. None when an input is unknown,
    a goal could not be converted, or a home goal exists but its plan could not be computed.
    """
    surplus, liquid, cash, summary = facts["unallocated"], facts["liquid"], facts["sectors"]["cash"], facts["summary"]
    if surplus is None or cash["reserve_gap"] is None:
        return None
    goals = [g for g in summary.goals if g["target_date"] > facts["today"]]
    if any(g["target"] is None for g in goals):
        return None
    # A gap the sellable assets fill in one step (reserve_swap) takes nothing from the monthly surplus.
    swap = facts["reserve_swap"]
    one_step = swap is not None and swap["one_step"]
    committed = [("비상금 채우기", Decimal(0) if one_step else cash["reserve_gap"] / SPENDING_FILL_MONTHS)]
    used, near_short = Decimal(0), False
    if facts["leases"]:
        lease = min(facts["leases"], key=lambda item: item["due"])
        if lease["amount"] is None or liquid is None:
            return None
        # The household's own cash in the default scenario (lease.plans): the re-let gap plus the buffer, or
        # the whole deposit when the market level is unknown.
        net = lease["need"]
        used = min(liquid, net)
        need = max(Decimal(0), (net - liquid) / lease["months"])
        committed.append(("보증금 반환 준비", need))
        near_short = need > 0 and lease["months"] <= NEAR_COMMITMENT_MONTHS
    planned = []
    home = facts["kpis"]["home_progress"]
    if home is None and any(g["category"] == "home" for g in goals):
        return None
    if home:
        # The home gap on the household baseline (§17.3), not the client alone: the partner's equity and the
        # expected pay are part of the plan, so the monthly need is the realistic one.
        gap = home["gap_baseline"]
        planned.append(("주택 이전 목표", max(Decimal(0), home["monthly_net_self"] + (gap + used) / max(1, home["months"]))))
    for goal in goals:
        # The home goal is above; the emergency fund is the reserve; retirement keeps its current transfers.
        if goal["category"] in ("home", "emergency_fund", "retirement"):
            continue
        label = f"{GOAL_LABELS.get(goal['category'], goal['category'])} 목표({goal['target_date']:%Y.%m})"
        planned.append((label, goal["target"] / max(1, goal["months_left"])))
    committed_total = sum((amount for _, amount in committed), Decimal(0))
    planned_total = sum((amount for _, amount in planned), Decimal(0))
    return {"surplus": surplus, "committed": committed, "planned": planned, "committed_total": committed_total,
            "planned_total": planned_total, "room": surplus - committed_total,
            "room_with_goals": surplus - committed_total - planned_total, "near_commitment_short": near_short,
            "reserve_swap": cash["reserve_gap"] if one_step else None}


def _spending_room(facts: dict[str, Any]) -> Proposal | Undetermined | None:
    """What the client may spend every month without touching the reserve or a contractual payment, the
    smaller figure when the planned goals are kept too, and where a private banker would point it:
    experiences that keep their value rather than goods that lose it (§16.3)."""
    room = facts["spending_room"]
    if room is None:
        return Undetermined("안심 소비 한도", "월 현금흐름·현금·보증금·목표 금액 가운데 원화로 환산하지 못했거나 계획을 "
                                       "계산하지 못한 것이 있습니다.")

    def parts(items: list[tuple[str, Decimal]]) -> str:
        return ", ".join(f"{label} {short_won(amount)}" for label, amount in items if amount)
    basis = (f"남는 돈 {short_won(room['surplus'])}은 수입에서 지출과 본인 계좌로의 이체를 뺀 금액입니다. 여기서 비상금과 약정에 "
             f"매월 필요한 적립 {short_won(room['committed_total'])}"
             + (f", 곧 {parts(room['committed'])}" if parts(room["committed"]) else "") + f"을 빼면 {short_won(room['room'])}입니다."
             + (f" 비상금 부족분 {short_won(room['reserve_swap'])}은 주식 일부를 정리해 한 번에 채우는 방안을 따로 제안했으므로 "
                "매월 적립에서 뺐습니다." if room.get("reserve_swap") else ""))
    with_goals = room["room_with_goals"]
    goals_note = ""
    if room["planned"]:
        goals_note = (f" 계획한 목표({parts(room['planned'])})까지 지금 속도로 지키려면 "
                      + (f"{short_won(with_goals)}이 남습니다." if with_goals >= 0 else
                         f"매월 {short_won(-with_goals)}이 모자라, 그 목표의 시점·금액을 조정하지 않는 한 여유가 없습니다."))
    certainty = (f"계산(기록된 현금흐름) + 가정(비상금은 {SPENDING_FILL_MONTHS}개월 안에 채움, 주택은 기준 시나리오, 다른 목표는 "
                 "배정된 자금 없이 전액 새로 모음)")
    figures = {"room": room["room"], "room_with_goals": with_goals, "surplus": room["surplus"],
               "needs": room["committed_total"] + room["planned_total"]}
    reconsider = "월 현금흐름, 목표 금액·시점, 전세 시세가 바뀌면 다시 계산합니다."
    if room["room"] >= 0:
        tail = ("" if not room["planned"] else
                f" 계획한 목표까지 지키면 약 {short_won(with_goals)}입니다." if with_goals >= 0 else
                " 다만 계획한 목표까지 지금 속도로 지키려면 여유가 없습니다.")
        return Proposal(
            key="spending-room", priority=3, watch=("room",), tolerance=Decimal("0.25"),
            title=f"매월 약 {short_won(room['room'])}은 비상금과 약정을 지키고도 쓰실 수 있는 돈입니다.{tail}",
            why=basis + goals_note + " 약정 기준 한도 안의 소비는 비상금과 보증금 반환을 늦추지 않습니다. 돈을 끝까지 쌓아 두는 "
                                     "것은 계획의 목적이 아니며, 제때 삶으로 바꾸는 것도 자산 관리의 일부입니다.",
            effect=(("두 숫자 가운데 작은 쪽은 어떤 계획도 건드리지 않는 돈입니다. " if room["planned"] and with_goals >= 0 else "")
                    + "값이 떨어지는 물건보다 시간이 지나도 남는 경험(건강, 배움, 여행, 전시·공연, 가족과 보내는 시간)에 먼저 "
                      "배정하시길 권합니다. 어떤 경험을 좋아하시는지 알려 주시면 그에 맞춰 제안드리겠습니다."),
            risks=("한도는 기록된 현금흐름과 목표 가정 위의 계산값입니다. 수입이 줄거나 큰 지출이 생기면 줄어들고, 노후 적립을 "
                   "지금보다 늘리기로 하시면 그만큼 줄어듭니다. 한도를 넘겨 쓰면 가장 가까운 약정부터 늦어집니다."),
            timing="이번 달부터 매월", reconsider=reconsider, certainty=certainty, figures=figures)
    return Proposal(
        key="spending-room", priority=1 if room["near_commitment_short"] else 2, watch=("room",), tolerance=Decimal("0.25"),
        title=f"지금은 비상금과 약정에 필요한 적립이 남는 돈보다 약 {short_won(-room['room'])} 많아, 마음 편히 쓸 여유가 없습니다.",
        why=basis + goals_note + (f" {NEAR_COMMITMENT_MONTHS}개월 안에 돌려드려야 할 보증금을 지금 속도로는 마련하지 못하므로 "
                                  "먼저 손봐야 합니다." if room["near_commitment_short"] else
                                  " 남는 돈을 모두 넣어도 모자라므로, 소비를 늘리기 전에 약정 쪽을 먼저 손봐야 합니다."),
        effect=("멈출 수 있는 지출을 찾아 적립으로 돌리거나, 반환 방법(재계약·새 세입자 보증금·매도·대출)을 앞당겨 정하면 "
                "한도가 생깁니다. 어느 쪽이 나은지는 고객님의 판단이 필요합니다."),
        risks="그대로 두면 가장 가까운 약정(보증금 반환)부터 늦어지고, 만기 직전에 불리한 선택을 하게 될 수 있습니다.",
        timing=("이번 달 안에 반환 방법 결정" if room["near_commitment_short"] else "다음 생활·목표 보고 전까지 우선순위 결정"),
        reconsider=reconsider, certainty=certainty, figures=figures)

def _strategy(facts: dict[str, Any]) -> list[Track]:
    tracks = []
    home = facts["kpis"]["home_progress"]
    if home:
        gap = home["gap_baseline"]
        months = _months_between(facts["today"], home["target_date"])
        # The headline is the household baseline (§17.3): the client's and the partner's resources plus the
        # confirmed and expected pay due before the goal. The conservative tiers are written beside it.
        status = "on_track" if gap <= 0 else "attention"
        tracks.append(Track(
            "주택 이전", status,
            f"{home['target_date']:%Y년 %m월} 목표, 가구 통합 기준 필요액의 {percent(home['ratio_baseline'], 0)} 준비 가능",
            f"기준 시나리오(집값 연 2%·투자 수익 연 4%)에서 {home['baseline_label']} 기준입니다." + (
                f" 부족분 {short_won(gap)}을 남은 {months}개월에 나누면 매월 약 {short_won(gap / months)}이며, 대출 한도와 "
                "주택 매도 순서로 메우는 것이 현실적입니다." if gap > 0 and months > 0 else
                f" 필요액을 넘어 약 {short_won(-gap)}이 남습니다." if gap < 0 else "") + _home_tiers(home) + _pay_note(home)))
    for lease in facts["leases"]:
        if lease["amount"] is None:
            continue
        accumulated = _accumulated(facts, lease["months"])
        status = "unknown" if accumulated is None else "on_track" if accumulated >= lease["amount"] else "attention"
        tracks.append(Track(
            "전세보증금 반환", status,
            f"{lease['due']:%Y년 %m월} {short_won(lease['amount'])}, 만기 때 예상 가용 자금 {short_won(accumulated)}",
            "주택 이전 목표와 같은 자금을 나눠 써야 하므로, 두 목표를 합친 자금 계획이 필요합니다." if home else ""))
    pension = facts["pension_total"]
    home_goal = any(g["category"] == "home" and g["target_date"] > facts["today"] for g in facts["summary"].goals)
    share = future_income.retirement_share(facts["pay"], home=home, home_goal=home_goal)
    plan = tax.retirement(facts["tax"], extra=share)
    if plan is None or not plan["paths"]:
        tracks.append(Track(
            "노후 준비", "unknown",
            f"연금 계좌 {short_won(pension)}" if pension is not None else "연금 계좌 정보 없음",
            "은퇴 시기와 은퇴 후 월 생활비 목표가 없어 궤도를 판단할 수 없습니다. 알려 주시면 필요한 적립액을 계산해 드리겠습니다."))
        return tracks
    mid = next((p for p in plan["paths"] if p["age"] == 60), plan["paths"][-1])
    paths = ", ".join(f"{p['age']}세 {percent(p['ratio'], 0)}" for p in plan["paths"])
    # The status stays on the pension accounts alone; the plan with expected pay is shown beside it.
    tracks.append(Track(
        "노후 준비", "on_track" if mid["ratio"] >= 1 else "attention",
        f"월 {short_won(plan['spend'])}(현재 가치) 생활에 필요한 자금 약 {short_won(plan['need'])}, 연금 계좌만으로는 {paths}"
        + (f"(예정 소득 포함 시 {mid['age']}세 {percent(mid['ratio_with_pay'], 0)})"
           if share and mid["ratio_with_pay"] is not None else ""),
        (f"지금 연금 계좌 {short_won(plan['assets'] if pension is None else pension)}에 매년 {short_won(plan['yearly'])}씩 "
         "더하고 실질 연 3%로 불린다고 가정했습니다(국민연금·퇴직금·부동산 제외). "
         + (f"환율이 없어 원화로 바꾸지 못한 연금 자산·납입 {plan['unconverted']}건은 빼고 계산했으므로, 위 비율은 확인된 "
            "금액만으로 본 하한입니다. " if plan["unconverted"] else "")
         + _retirement_pay_note(share, plan, home_goal)
         + "은퇴 시기를 정하시면 부족분을 무엇으로 채울지(부동산 축소, 추가 적립, 국민연금) 구체적으로 계산해 드리겠습니다.")))
    return tracks


def _retirement_pay_note(share: list[Any] | None, plan: dict[str, Any], home_goal: bool) -> str:
    """How expected pay enters the retirement plan beside it (never in the status)."""
    if share is None:
        return "예정 소득을 더한 노후 계산은 주택 목표 계산에 미상 금액이 있어 하지 않았습니다. "
    if not share:
        return ""
    used = ("주택 목표에 필요한 만큼을 먼저 쓰고 남는 몫과 주택 목표일 이후에 받는 몫" if home_goal
            else "기준 시나리오의 예정 소득")
    if not plan["pay_set_aside"]:
        return f"예정 소득은 모두 주택 목표에 필요해, 노후 계산에 더할 몫이 없습니다({used} 기준). "
    return (f"{used}(확정·예상분, 세후 추정 약 {short_won(plan['pay_set_aside'])})을 노후 자금으로 모아 실질 연 3%로 "
            "불린다고 보면 괄호 안 비율이 됩니다. 받기 전까지는 확정이 아니어서 상태 판단에는 넣지 않았습니다. ")


def _home_tiers(home: dict[str, Any]) -> str:
    """The conservative tiers beside the baseline: the client alone, the household's existing assets, and the
    baseline after the partner's home-sale tax in both cases (report design §17.3)."""
    text = f" 보수적으로 지금 있는 자산만 보면 본인 자원 {percent(home['ratio_self'], 0)}"
    if home.get("ratio_household") is not None:
        text += f", 배우자 부동산 순자산을 더해 {percent(home['ratio_household'], 0)}"
    text += "입니다."
    after = home.get("after_tax")
    if after:
        text += (f" 배우자 주택을 팔아 쓰는 경우 양도세는 1세대 1주택 비과세 요건을 갖추면 약 {short_won(after['tax']['exempt'])}, 일반 "
                 f"과세면 약 {short_won(after['tax']['general'])}이어서, 준비 가능액은 각각 약 {short_won(after['available']['exempt'])}과 "
                 f"{short_won(after['available']['general'])}입니다(혼인신고 시점과 2년 거주 여부에 따름, 현행 법령 확인 필요).")
    return text


def _pay_note(home: dict[str, Any]) -> str:
    """What expected pay (confirmed and expected, after estimated tax) is in the home plan."""
    if not home.get("pay_at_target") and not home.get("pay_after_target"):
        return ""
    note = ""
    if home.get("pay_at_target"):
        note = (f" 기준에 넣은 목표일 전 성과급·주식 보상(확정·예상분, 세후 추정)은 그때 가치로 약 {short_won(home['pay_at_target'])}이며, "
                "받기 전까지는 확정이 아닙니다.")
    if home.get("pay_after_target"):
        note += f" 목표일 이후에 받을 예정분 약 {short_won(home['pay_after_target'])}은 주택 계산에 넣지 않았습니다."
    return note


def _assumptions() -> list[str]:
    return [f"이자소득세 {percent(INTEREST_TAX, 1)}",
            f"비상금 부족분은 며칠 안에 팔 수 있는 자산이 부족분의 {SWAP_MULTIPLE}배 이상이면 자산 간 이동으로 한 번에 채운다고 봄",
            f"주택 목표 머리 숫자는 가구 통합(본인 + 배우자 부동산 순자산 + 목표일 전 확정·예상 소득의 세후 가치) 기준, 보수 기준을 병기",
            f"보험료 기준 월 소득의 {percent(INSURANCE_SHARE_LIMIT, 0)}, 월 {short_won(SAVINGS_PREMIUM_MIN)} 이상 보험료는 저축성 가능으로 보고 "
            f"비과세 만기 {SAVINGS_INSURANCE_YEARS}년과 주택 이전 시점을 비교",
            f"연금계좌 세액공제 한도 연 {short_won(PENSION_CREDIT_LIMIT)}(연금저축 {short_won(PENSION_SAVINGS_LIMIT)}), "
            f"공제율 {percent(PENSION_CREDIT_RATE, 1)}(소득세 12%+지방소득세, 총급여 5,500만 원 초과 가정)",
            "예금 금리는 CD 91일물 유통수익률로 근사(실제 정기예금 금리와 다를 수 있음)"]
