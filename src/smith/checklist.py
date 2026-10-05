"""Data requests: what the client should still provide so the report's judgements are complete.

Each rule looks at the ledger snapshot and the local household profile and asks only for information
that has a place to go (an existing JSON field or config key) and that a calculation actually uses.
Priorities: "must" when a current judgement is empty or likely wrong without it (it also turns the
report status to 주의), "should" when a figure becomes materially more exact, "nice" otherwise.
"""
from dataclasses import dataclass
from datetime import date
from typing import Any

from smith.payload import LedgerView
from smith.records import Kind

PRIORITY_ORDER = {"must": 0, "should": 1, "nice": 2}
_PENSION_ACCOUNTS = ("pension_savings", "irp")
_CASH = ("cash", "deposit", "installment_savings")


@dataclass(frozen=True)
class Request:
    key: str
    priority: str   # must / should / nice
    title: str      # What to provide.
    why: str        # Which judgement is empty or rough without it.
    how: str        # Where it goes (JSON field or local config key).


def requests(view: LedgerView, household: dict[str, Any] | None) -> list[Request]:
    """Open data requests, most important first."""
    household = household or {}
    today = view.as_of.date()
    rules = (_cash_flows, _partner, _irregular_flows, _pension_paid, _unclassified, _cost_basis, _toss_cash,
             _surrender_values, _loan_terms, _acquisition, _retirement, _marriage, _transfer_targets, _commitment)
    found = [r for rule in rules if (r := rule(view, household, today)) is not None]
    return sorted(found, key=lambda r: PRIORITY_ORDER[r.priority])


def _assets(view: LedgerView) -> list[Any]:
    return [r for r in view.records if r.kind is Kind.ASSET]


def _flows(view: LedgerView) -> list[Any]:
    return [r for r in view.records if r.kind is Kind.CASHFLOW]


def _cash_flows(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    if _flows(view):
        return None
    return Request("cash-flows", "must", "매월 들어오고 나가는 돈(급여, 생활비, 대출 상환, 보험료 등)",
                   "현금흐름 기록이 없어 월 여유 자금, 비상금 기준, 목표까지의 적립 속도를 계산하지 못합니다.",
                   "JSON 입력의 cashflow 레코드(direction, category, amount, frequency)")


def _partner(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    others = {r.owner_id for r in view.records} - {"self"}
    for owner in sorted(others):
        mine = [r for r in view.records if r.owner_id == owner]
        has_cash = any(r.kind is Kind.ASSET and r.fields["category"] in _CASH for r in mine)
        has_flows = any(r.kind is Kind.CASHFLOW for r in mine)
        if not (has_cash and has_flows):
            return Request(
                "partner-finances", "must", "배우자 명의의 현금·예금, 소득과 지출(대출이 있다면 대출도)",
                "배우자 명의로는 현금이나 소득·지출 기록이 없어, 가계의 여유 자금과 배우자 명의 부채(예: 전세보증금 반환)의 "
                "준비 상황을 고객님 자산만으로 판단하고 있습니다.",
                f'JSON 입력에서 owner_id를 "{owner}"로 한 현금(asset)과 급여·지출(cashflow) 레코드')
    return None


def _irregular_flows(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    flows = _flows(view)
    if not flows or any(r.fields["frequency"] in ("annual", "quarterly", "once") for r in flows):
        return None
    return Request(
        "irregular-flows", "must", "1년에 한두 번 생기는 수입·지출(보너스, 재산세, 자동차보험, 명절·여행 등)",
        "기록된 현금흐름이 모두 매월 반복 항목이라, 월 여유 자금과 비상금 목표가 실제보다 좋게 계산될 수 있습니다.",
        "cashflow 레코드의 frequency를 annual(매년) 또는 once(한 번)로 입력")


def _pension_paid(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    """Recurring pension transfers recorded from mid-year leave earlier contributions unknown, and the
    year-end credit room is computed without them. Asked from September, when the room matters."""
    if today.month < 9:
        return None
    pensions = {r.record_id for r in _assets(view)
                if r.owner_id == "self" and r.fields.get("account_type") in _PENSION_ACCOUNTS}
    transfers = [r for r in _flows(view) if r.owner_id == "self" and r.fields["category"] == "internal_transfer"
                 and r.fields.get("target_record_id") in pensions]
    if not transfers:
        return None
    if any(r.fields["frequency"] == "once" and r.fields["start_date"][:4] == str(today.year) for r in transfers):
        return None
    if all(date.fromisoformat(r.fields["start_date"]) <= date(today.year, 1, 1) for r in transfers):
        return None
    return Request(
        "pension-paid", "should", "올해 1월부터 지금까지 연금저축·IRP에 실제로 넣은 금액",
        "정기 이체 기록이 올해 중간에 시작되어, 그 전에 넣은 금액을 모른 채 연말 세액공제의 남은 한도를 계산하고 있습니다.",
        "기록 시작 전 납입액을 internal_transfer(frequency=once, target_record_id=연금 계좌)로 입력. "
        "그 전에 넣은 돈이 없다면 이 요청은 넘기셔도 됩니다")


def _unclassified(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    count = sum(1 for r in _assets(view) if r.fields["category"] == "unclassified")
    if not count:
        return None
    return Request(
        "unclassified", "should", f"구성을 모르는 증권 계좌 {count}건의 구성(종목, 또는 주식·채권·현금 비율)",
        "종목 쏠림과 평가손익 계산에서 빠지고, 긴급 자금 계산에서는 며칠 안에 팔 수 있는 돈으로 대략 잡힙니다.",
        "총액(unclassified) 레코드를 종목 또는 자산군별 레코드로 나누고, 총액 레코드는 종료(status=closed)")


def _cost_basis(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    count = sum(1 for r in _assets(view) if r.fields.get("symbol") and not r.fields.get("average_cost"))
    if not count:
        return None
    return Request("cost-basis", "should", f"매입 단가가 없는 종목 {count}개의 수량과 매입 단가",
                   "평가손익이 일부 종목만의 합계로 나오고, 해외주식 세금 계산에서 빠집니다.",
                   "asset 레코드의 quantity, unit_price, average_cost")


def _toss_cash(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    synced = any(s["source"] == "toss" for s in view.summary.freshness)
    manual = any(r.fields["category"] == "cash" and r.fields.get("account_type") == "brokerage" for r in _assets(view))
    if not synced or manual:
        return None
    return Request("toss-cash", "should", "토스증권 계좌의 예수금(현금) 잔액",
                   "토스 API는 예수금을 알려 주지 않아(미수집) 즉시 쓸 수 있는 돈에서 증권 계좌 현금이 빠져 있습니다.",
                   "asset 레코드(category=cash, account_type=brokerage, valuation_method=statement)로 직접 입력")


def _surrender_values(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    premiums = [r for r in _flows(view) if r.fields["category"] == "insurance_premium"]
    if not premiums or any(r.fields["category"] == "insurance_surrender_value" for r in _assets(view)):
        return None
    return Request(
        "surrender-values", "should", f"보험 {len(premiums)}건의 해약환급금(지금 해지하면 돌려받는 돈)과 보험 종류(보장성·저축성)",
        "보험에 묶인 돈과 급할 때 쓸 수 있는 돈을 알 수 없고, 저축성 보험 비과세 안내가 보험료 크기만 보고 나옵니다.",
        "asset 레코드(category=insurance_surrender_value, account_type=insurance)")


def _loan_terms(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    loans = [r for r in view.records if r.kind is Kind.LIABILITY and r.fields["category"] != "lease_deposit_obligation"
             and "unknown" in (r.fields["rate_type"], r.fields["repayment_method"])]
    if not loans:
        return None
    return Request("loan-terms", "should", f"대출 {len(loans)}건의 금리 방식(고정·변동)과 상환 방식",
                   "금리가 오를 때의 이자 증가와 추가 상환 효과를 그 대출에 대해 계산하지 못합니다.",
                   "liability 레코드의 rate_type, repayment_method(대출 약정서 확인)")


def _acquisition(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    properties = household.get("properties") or {}
    homes = [r for r in _assets(view) if r.fields["category"] == "real_estate"
             and not all((properties.get(r.record_id) or {}).get(k) for k in ("acquired_year", "acquired_price"))]
    if not homes:
        return None
    return Request("acquisition", "should", f"주택 {len(homes)}채의 취득 연도와 취득가",
                   "팔 때의 양도소득세를 추정하지 못해, 주택 이전 순서와 시기의 세금 비교가 비어 있습니다.",
                   '로컬 설정 [properties."<레코드 ID>"]의 acquired_year, acquired_price')


def _retirement(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    if household.get("birth_year") and household.get("retirement_monthly_spend"):
        return None
    return Request("retirement", "should", "출생 연도와 은퇴 후 월 생활비(지금 물가 기준)",
                   "노후 준비가 궤도에 있는지 판단하지 못하고 있습니다.",
                   "로컬 설정 [household]의 birth_year, retirement_monthly_spend")


def _marriage(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    owners = {r.owner_id for r in _assets(view) if r.fields["category"] == "real_estate"}
    if household.get("marriage_registered") is not None or len(owners) < 2:
        return None
    return Request("marriage", "should", "혼인신고 여부",
                   "두 분이 각각 집을 가지고 있어, 혼인신고 여부에 따라 주택 수와 세금·대출 판단이 달라집니다.",
                   "로컬 설정 [household]의 marriage_registered(true/false)")


def _transfer_targets(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    count = sum(1 for r in _flows(view) if r.fields["category"] == "internal_transfer" and not r.fields.get("target_record_id"))
    if not count:
        return None
    return Request("transfer-targets", "should", f"입금 계좌가 기록되지 않은 이체 {count}건의 대상 계좌",
                   "그 돈이 연금처럼 묶이는지, 언제든 쓸 수 있는 계좌로 가는지 몰라 공제 한도와 모이는 돈 계산에서 빠집니다.",
                   "cashflow 레코드의 target_record_id(입금되는 자산 레코드 ID)")


def _commitment(view: LedgerView, household: dict[str, Any], today: date) -> Request | None:
    count = sum(1 for r in _flows(view) if r.fields["direction"] == "outflow" and r.fields["category"] != "internal_transfer"
                and not r.fields.get("commitment"))
    if not count:
        return None
    return Request("commitment", "nice", f"지출 {count}건이 꼭 내야 하는 돈인지, 줄이거나 멈출 수 있는 돈인지",
                   "급히 돈이 필요할 때 줄일 수 있는 월 지출을 0원으로 보고 계산합니다.",
                   "cashflow 레코드의 commitment(fixed 또는 discretionary)")
