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

from smith import tax
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


def build(view: LedgerView, kpis: dict[str, Any], sectors: dict[str, Any],
          active: dict[str, dict[str, Any]] | None = None, household: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return proposals (most urgent first), the strategy tracks and the follow-up of earlier proposals.

    `active` maps proposal identities to their current instance (ledger.active_proposals). Unless the
    proposal is renewed, a declined or done instance is not raised again, an accepted one moves to
    `in_progress`, and an open one carries how often it was shown.
    """
    facts = _facts(view, kpis, sectors)
    facts["tax"] = tax.facts(view, facts["today"], household)
    rules = (_lease_return, _emergency_reserve, _surplus_plan, _pension_credit, _overseas_harvest, _isa, _prepayment,
             _variable_rate)
    outcomes = [(i, rule(facts)) for i, rule in enumerate(rules)]
    found = [(i, p) for i, p in outcomes if isinstance(p, Proposal)]
    undetermined = [p for _, p in outcomes if isinstance(p, Undetermined)]
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
            "strategy": _strategy(facts), "assumptions": _assumptions() + tax.assumptions(),
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

def _facts(view: LedgerView, kpis: dict[str, Any], sectors: dict[str, Any]) -> dict[str, Any]:
    summary = view.summary
    today = view.as_of.date()
    leases = []
    for record in view.records:
        f = record.fields
        if record.kind is Kind.LIABILITY and f["category"] == "lease_deposit_obligation" and f.get("maturity"):
            due = date.fromisoformat(f["maturity"])
            months = _months_between(today, due)
            if 0 < months <= LEASE_HORIZON_MONTHS:
                market = next((p.get("market") for p in sectors["real_estate"]["properties"]
                               if p["record_id"] == f.get("collateral_record_id")), None)
                leases.append({"due": due, "months": months, "amount": base_amount(view, record),
                               "jeonse": None if market is None else market["jeonse"],
                               "jeonse_basis": 0 if market is None else market["jeonse_basis"]})
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
            "base_rate": _series(view, BASE_RATE_SERIES)}


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
    if not facts["leases"]:
        return None
    lease = min(facts["leases"], key=lambda item: item["due"])
    amount, months, surplus = lease["amount"], lease["months"], facts["available"]
    if amount is None:
        return Undetermined("전세보증금 반환 계획", "보증금 금액을 원화로 환산하지 못했습니다(환율 없음).")
    accumulated = _accumulated(facts, months)
    coverage = accumulated / amount if accumulated is not None and amount else None
    deadline = _add_months(lease["due"], -DECISION_LEAD_MONTHS)
    priority = 1 if months <= 12 or (coverage is not None and coverage < 1 and months <= 24) else 2
    home = facts["kpis"]["home_progress"]
    overlap = (" 다만 같은 자산과 여유 자금을 주택 이전 목표 계산에도 쓰고 있어, 두 목표가 같은 돈을 나눠 써야 합니다."
               if home else "")
    if accumulated is None:
        readiness = (" 일부 자산이나 현금흐름을 원화로 환산하지 못해(환율 없음) 만기 때 준비할 수 있는 금액은 "
                     "계산하지 않았습니다.")
    else:
        readiness = (f" 지금 며칠 안에 현금으로 바꿀 수 있는 자산 {short_won(facts['liquid'])}에 매월 여유 "
                     f"{short_won(surplus)}이 그대로 쌓인다고 보면, 만기 때 약 {short_won(accumulated)}으로 필요액의 "
                     f"{percent(coverage, 0)}입니다.{overlap}")
    jeonse, gap = lease["jeonse"], None
    market = ""
    if jeonse is not None and lease["jeonse_basis"] >= MIN_BASIS:
        gap = amount - jeonse
        market = (f" 같은 단지·같은 면적의 최근 6개월 전세 계약 {lease['jeonse_basis']}건의 중간값은 {short_won(jeonse)}으로, "
                  + (f"지금 시세로 새 세입자를 받으면 약 {short_won(gap)}을 직접 마련해야 합니다(역전세)." if gap > 0
                     else "지금 시세라면 새 세입자의 보증금으로 돌려드릴 수 있는 수준입니다."))
    elif jeonse is not None:
        # One or two contracts: shown for reference, but too few to put a gap figure on.
        market = (f" 같은 단지·같은 면적의 최근 6개월 전세 계약은 {lease['jeonse_basis']}건뿐이라(중간값 "
                  f"{short_won(jeonse)}) 참고용으로만 보시고, 역전세 차액은 계산하지 않았습니다.")
    return Proposal(
        key="lease-return", priority=priority, identity=f"lease-return:{lease['due']}",
        watch=("amount", "accumulated"), tolerance=Decimal("0.15"),
        title=f"{lease['due']:%Y년 %m월} 돌려드려야 할 전세보증금 {short_won(amount)}의 반환 방법을 "
              f"{deadline:%Y년 %m월}까지 정해 두시길 권합니다.",
        why=(f"전세보증금은 계약이 끝나면 세입자에게 돌려줘야 하는, 날짜와 금액이 정해진 큰 지출입니다(남은 기간 {months}개월)."
             + readiness + market),
        effect=("반환 방법(재계약, 새 세입자의 보증금, 매도, 보유 자금)을 미리 정해 두면 만기 직전에 주식을 불리한 시세에 "
                "팔거나 급히 대출을 받는 일을 피할 수 있습니다."),
        risks=("새 세입자의 보증금이 지금보다 낮으면(이른바 역전세) 그 차액을 직접 마련해야 합니다. 아무것도 정하지 않으면 "
               "만기 무렵의 시세와 금리에 따라 선택지가 좁아집니다."),
        timing=f"{deadline:%Y.%m}까지 방법 결정(만기 {DECISION_LEAD_MONTHS}개월 전)",
        reconsider="주변 전세 시세가 크게 바뀌거나, 재계약·매도 여부가 정해지면 다시 계산합니다.",
        certainty="계산(현재 잔액과 현금흐름이 유지된다는 가정)" + (", 국토교통부 실거래가" if market else ""),
        figures={"amount": amount, "months": Decimal(months),
                 **({} if accumulated is None else {"accumulated": accumulated}),
                 **({} if gap is None else {"jeonse_gap": gap})})


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

def _strategy(facts: dict[str, Any]) -> list[Track]:
    tracks = []
    home = facts["kpis"]["home_progress"]
    if home:
        gap = home["required"] - home["available_self"]
        months = _months_between(facts["today"], home["target_date"])
        status = "on_track" if gap <= 0 else "attention"
        tracks.append(Track(
            "주택 이전", status,
            f"{home['target_date']:%Y년 %m월} 목표, 본인 자원 기준 필요액의 {percent(home['ratio_self'], 0)} 준비 가능",
            "기준 시나리오(집값 연 2%·투자 수익 연 4%)입니다." + (
                f" 부족분 {short_won(gap)}을 남은 {months}개월에 나누면 매월 약 {short_won(gap / months)}을 더 모으거나, "
                "대출·배우자 자산으로 메워야 합니다." if gap > 0 and months > 0 else "")))
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
    plan = tax.retirement(facts["tax"])
    if plan is None or not plan["paths"]:
        tracks.append(Track(
            "노후 준비", "unknown",
            f"연금 계좌 {short_won(pension)}" if pension is not None else "연금 계좌 정보 없음",
            "은퇴 시기와 은퇴 후 월 생활비 목표가 없어 궤도를 판단할 수 없습니다. 알려 주시면 필요한 적립액을 계산해 드리겠습니다."))
        return tracks
    mid = next((p for p in plan["paths"] if p["age"] == 60), plan["paths"][-1])
    paths = ", ".join(f"{p['age']}세 {percent(p['ratio'], 0)}" for p in plan["paths"])
    tracks.append(Track(
        "노후 준비", "on_track" if mid["ratio"] >= 1 else "attention",
        f"월 {short_won(plan['spend'])}(현재 가치) 생활에 필요한 자금 약 {short_won(plan['need'])}, 연금 계좌만으로는 {paths}",
        (f"지금 연금 계좌 {short_won(plan['assets'] if pension is None else pension)}에 매년 {short_won(plan['yearly'])}씩 "
         "더하고 실질 연 3%로 불린다고 가정했습니다(국민연금·퇴직금·부동산 제외). "
         + (f"환율이 없어 원화로 바꾸지 못한 연금 자산·납입 {plan['unconverted']}건은 빼고 계산했으므로, 위 비율은 확인된 "
            "금액만으로 본 하한입니다. " if plan["unconverted"] else "")
         + "은퇴 시기를 정하시면 부족분을 무엇으로 채울지(부동산 축소, 추가 적립, 국민연금) 구체적으로 계산해 드리겠습니다.")))
    return tracks


def _assumptions() -> list[str]:
    return [f"이자소득세 {percent(INTEREST_TAX, 1)}",
            f"연금계좌 세액공제 한도 연 {short_won(PENSION_CREDIT_LIMIT)}(연금저축 {short_won(PENSION_SAVINGS_LIMIT)}), "
            f"공제율 {percent(PENSION_CREDIT_RATE, 1)}(소득세 12%+지방소득세, 총급여 5,500만 원 초과 가정)",
            "예금 금리는 CD 91일물 유통수익률로 근사(실제 정기예금 금리와 다를 수 있음)"]
