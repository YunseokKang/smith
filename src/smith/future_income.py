"""Expected pay beyond the salary (bonuses, share grants) and how plans count it (requirement FR-20).

Money not yet paid is not wealth: it never enters net worth or the monthly cash flow. Plans count it by how
sure it is (records.CERTAINTIES): the adverse scenario only what is confirmed, the base scenario also what
is expected, the favourable one everything. A one-time inflow that states no certainty counts as possible.

Lifecycle: payment is what the client states, never inferred from a date or a closed record. A `received`
revision (actual day and taxable amount) leaves the plans and only stacks onto the year's later pay. A
scheduled payment whose day has passed is `overdue`: listed for the client to confirm, left out of plans.

Valuation, in the base currency:
- A share grant is worth its quantity at the latest price Smith holds for the same stock (an asset record
  with the same market, symbol and currency); without one, the entered estimate (`amount`).
- A gross amount loses the estimated final tax and insurance of the year (tax.extra_pay_withholding), not
  what is withheld on payday. The client's payments stack on the base salary in date order within a
  calendar year, so a large bonus climbs the brackets. An amount entered after tax has no known gross:
  later pay of that year then has an unknown tax ("prior_unknown"), never an assumed one.
- At a goal date, cash has earned the scenario's return since it became usable; shares carry the market
  from today, because their value moves with the price until they are sold.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from smith import tax
from smith.fmt import percent
from smith.payload import LedgerView, base_amount, owner_alias, won
from smith.records import COMPENSATION_CATEGORIES, INFLOW_CATEGORIES, Kind, RecordInput

SCENARIO_CERTAINTIES = {"adverse": ("confirmed",), "base": ("confirmed", "expected"),
                        "favorable": ("confirmed", "expected", "possible")}
_SECURITIES = ("stock", "fund", "bond", "unclassified")


@dataclass(frozen=True)
class Inflow:
    """One future one-time inflow, valued now. Amounts are None when an FX rate is missing."""

    record_id: str
    owner_id: str
    category: str
    certainty: str            # "possible" when the record does not say (see `certainty_stated`).
    certainty_stated: bool
    paid_on: date
    usable_from: date
    symbol: str | None
    market: str | None
    quantity: Decimal | None
    price: Decimal | None     # Per share in the grant's currency, when valued at the market price.
    priced_on: date | None
    gross: Decimal | None     # Before tax; None when entered after tax or without an FX rate.
    withholding: Decimal | None
    # "estimated" (on the stated salary), "assumed" (assumed marginal rate: no salary, or another owner),
    # "net_input" (entered after tax), "as_entered" (income other than pay, taken as stated), "unknown" (no FX
    # rate) or "prior_unknown" (an earlier payment of the year has no known gross, so the bracket is unknown).
    withholding_basis: str
    net_entered: Decimal | None = None  # The amount as entered, for "net_input" and "as_entered".
    overdue: bool = False               # Its day has passed and the client has not said it was received.

    @property
    def net(self) -> Decimal | None:
        if self.withholding_basis in ("net_input", "as_entered"):
            return self.net_entered
        return None if self.gross is None or self.withholding is None else self.gross - self.withholding

    @property
    def shares(self) -> bool:
        """A grant of a fixed number of shares: its value moves with the price from today."""
        return self.category == "stock_compensation"

    @property
    def in_stock(self) -> bool:
        """Paid in shares, either a share grant or a won-denominated bonus converted to shares on payday."""
        return self.symbol is not None


def inflows(view: LedgerView, *, salary: Decimal | None = None) -> list[Inflow]:
    """Unpaid one-time inflows of every owner (scheduled and overdue), oldest first. `salary` is the client's
    yearly gross base pay without the bonuses and grants recorded here; without it extra pay is taxed at the
    assumed rate."""
    today = view.as_of.date()
    once = sorted((r for r in view.records if r.kind is Kind.CASHFLOW and r.fields["frequency"] == "once"
                   and r.fields["category"] in INFLOW_CATEGORIES),
                  key=lambda r: (_pay_day(r), r.record_id))
    stacked: dict[int, Decimal | None] = {}  # The client's gross extra pay so far in each calendar year.
    found = []
    for record in once:
        f, day = record.fields, _pay_day(record)
        pay, received = f["category"] in COMPENSATION_CATEGORIES, f.get("payment_status") == "received"
        if not pay and day <= today:
            continue  # Other one-time income has no payment status: its date is all there is.
        value, price, priced_on = _value(view, record)
        gross = _taxable(view, record) if received else value if pay and f["amount_basis"] == "gross" else None
        prior = stacked.get(day.year, Decimal(0))
        if record.owner_id == "self" and pay:
            stacked[day.year] = None if prior is None or gross is None else prior + gross
        if received:
            continue  # In the balances now: it only stacks onto the year's later pay.
        withholding, basis = _withholding(record, gross, salary, prior)
        usable = f.get("available_from")
        found.append(Inflow(
            record.record_id, record.owner_id, f["category"], f.get("certainty") or "possible",
            f.get("certainty") is not None, day, date.fromisoformat(usable) if usable else day,
            f.get("symbol"), f.get("market"), Decimal(f["quantity"]) if f.get("quantity") else None,
            price, priced_on, gross, withholding, basis,
            net_entered=value if basis in ("net_input", "as_entered") else None, overdue=day <= today))
    return found


def _pay_day(record: RecordInput) -> date:
    """The actual payment day once received, else the scheduled one."""
    return date.fromisoformat(record.fields.get("received_on") or record.fields["start_date"])


def _taxable(view: LedgerView, record: RecordInput) -> Decimal | None:
    rate = view.rates.get(record.fields["currency"])
    return None if rate is None else Decimal(record.fields["taxable_amount"]) * rate


def _withholding(record: RecordInput, gross: Decimal | None, salary: Decimal | None,
                 prior: Decimal | None) -> tuple[Decimal | None, str]:
    """(final yearly tax and insurance, basis) on one unpaid payment; `prior` is the client's gross extra pay
    earlier in the same year, None when one of those amounts is unknown."""
    f = record.fields
    if f["category"] not in COMPENSATION_CATEGORIES:
        return None, "as_entered"
    if f["amount_basis"] == "net":
        return None, "net_input"
    if gross is None:
        return None, "unknown"
    if record.owner_id != "self" or salary is None:
        return tax.extra_pay_withholding(gross, pay_before=None)
    if prior is None:
        return None, "prior_unknown"
    return tax.extra_pay_withholding(gross, pay_before=salary + prior)


def _value(view: LedgerView, record: RecordInput) -> tuple[Decimal | None, Decimal | None, date | None]:
    """(value, share price, price date). A grant without a held price falls back to its estimate. For an
    after-tax grant the quantity is the shares actually kept, so the value is after tax too."""
    f = record.fields
    if f["category"] == "stock_compensation":
        quote = market_price(view, f["market"], f["symbol"], f["currency"])
        if quote is not None:
            price, priced_on = quote
            rate = view.rates.get(f["currency"])
            return (None if rate is None else Decimal(f["quantity"]) * price * rate), price, priced_on
    return base_amount(view, record), None, None


def market_price(view: LedgerView, market: str | None, symbol: str, currency: str) -> tuple[Decimal, date] | None:
    """The newest per-share price among the held positions in the same stock, with its valuation date."""
    quotes = [r for r in view.records if r.kind is Kind.ASSET and r.fields.get("symbol") == symbol
              and r.fields.get("market") == market and r.fields["currency"] == currency and r.fields.get("unit_price")]
    if not quotes:
        return None
    latest = max(quotes, key=lambda r: (r.effective_at, r.record_id))
    return Decimal(latest.fields["unit_price"]), latest.effective_at.date()


def at_goal(items: list[Inflow], *, owner_id: str, scenario: str, horizon: date, today: date,
            rate: Decimal) -> dict[str, Any]:
    """What one owner's inflows counted in `scenario` are worth at `horizon`, grown at `rate` a year.

    `value` covers the inflows usable by then; `after` is the nominal net of those usable only later (money
    for later goals, not this one). Either is None when an amount is unknown. Overdue pay is left out until
    the client confirms it."""
    counted = [i for i in items if i.owner_id == owner_id and not i.overdue
               and i.certainty in SCENARIO_CERTAINTIES[scenario]]
    value: Decimal | None = Decimal(0)
    after: Decimal | None = Decimal(0)
    refs_before, refs_after = [], []
    for item in counted:
        net = item.net
        if item.usable_from > horizon:
            refs_after.append(item.record_id)
            after = None if after is None or net is None else after + net
            continue
        refs_before.append(item.record_id)
        # A bonus paid in shares carries the market from payday, even while a lock-up keeps it unusable.
        start = today if item.shares else item.paid_on if item.in_stock else item.usable_from
        months = max(0, (horizon.year - start.year) * 12 + horizon.month - start.month)
        value = None if value is None or net is None else value + net * (1 + rate) ** (Decimal(months) / 12)
    return {"value": value, "after": after, "before_refs": refs_before, "after_refs": refs_after}


def retirement_share(items: list[Inflow], *, home: dict[str, Any] | None,
                     home_goal: bool) -> list[tuple[date, Decimal | None]] | None:
    """The client's base-scenario pay left for retirement, as (usable from, net) pairs, so that one won is
    never planned for both goals. The nearest home goal (report_data._home_progress) first takes what its
    gap needs from the pay usable by its date; pay usable later and the part it does not need go to
    retirement. None when a home goal exists but its figures are unknown."""
    counted = [i for i in items if i.owner_id == "self" and not i.overdue
               and i.certainty in SCENARIO_CERTAINTIES["base"]]
    if not counted or not home_goal:
        return [(i.usable_from, i.net) for i in counted]
    if home is None or home.get("pay_at_target") is None or home.get("available_with_pay") is None:
        return None
    spare = max(Decimal(0), home["available_with_pay"] - home["required"])
    share = min(Decimal(1), spare / home["pay_at_target"]) if home["pay_at_target"] else Decimal(1)
    return [(i.usable_from, i.net if i.usable_from > home["target_date"] or i.net is None else i.net * share)
            for i in counted]


def review(view: LedgerView, items: list[Inflow]) -> dict[str, Any]:
    """The report's view of expected pay: the inflows, net totals of the scheduled ones by certainty and by
    year, the overdue ones to confirm, and the concentration pay in shares adds to a stock already held."""
    scheduled = [i for i in items if not i.overdue]
    by_certainty: dict[str, Decimal | None] = {key: Decimal(0) for key in SCENARIO_CERTAINTIES["favorable"]}
    by_year: dict[int, Decimal | None] = {}
    for item in scheduled:
        for groups, key in ((by_certainty, item.certainty), (by_year, item.paid_on.year)):
            current = groups.get(key, Decimal(0))
            groups[key] = None if current is None or item.net is None else current + item.net
    return {"items": items, "by_certainty": by_certainty, "by_year": dict(sorted(by_year.items())),
            "overdue": [i for i in items if i.overdue], "same_stock": _same_stock(view, scheduled),
            "assumed_tax": any(i.withholding_basis == "assumed" for i in items),
            "prior_unknown": any(i.withholding_basis == "prior_unknown" for i in items),
            "unstated_certainty": sum(not i.certainty_stated for i in items)}


def _same_stock(view: LedgerView, items: list[Inflow]) -> list[dict[str, Any]]:
    """For each stock pay comes in: the client's holding now, what the base scenario adds after tax (what
    is actually held once paid), and the stock's share of the client's securities before and after (the
    employer's own stock adds to the risk already carried by the salary)."""
    mine = [r for r in view.records if r.kind is Kind.ASSET and r.owner_id == "self"
            and r.fields["category"] in _SECURITIES]
    securities = [base_amount(view, r) for r in mine]
    total = None if any(v is None for v in securities) else sum(securities, Decimal(0))
    found = []
    for market, symbol in sorted({(i.market, i.symbol) for i in items if i.in_stock and i.owner_id == "self"}):
        held_values = [base_amount(view, r) for r in mine if r.fields.get("symbol") == symbol
                       and r.fields.get("market") == market]
        grants = [i.net for i in items if i.in_stock and i.owner_id == "self" and i.symbol == symbol
                  and i.market == market and i.certainty in SCENARIO_CERTAINTIES["base"]]
        held = None if any(v is None for v in held_values) else sum(held_values, Decimal(0))
        granted = None if any(v is None for v in grants) else sum(grants, Decimal(0))
        known = held is not None and granted is not None and total is not None
        # No securities yet is a known 0%, not an unknown share: a first grant still makes a 100% position.
        found.append({"symbol": symbol, "market": market, "held": held, "granted": granted,
                      "share_now": None if not known else held / total if total else Decimal(0),
                      "share_with_grants": (held + granted) / (total + granted) if known and total + granted else None})
    return found


def context(view: LedgerView, found: dict[str, Any]) -> dict[str, Any]:
    """The model's allowlisted view of `review`: refs, categories, dates, certainty and whole-won amounts.
    A grant carries its ticker, market and share count (public facts, user decision 2026-10-11)."""
    def ratio(value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.4f}"
    items = []
    for i in found["items"]:
        item = {"ref": view.aliases[i.record_id], "owner": owner_alias(view, i.owner_id), "category": i.category,
                "certainty": i.certainty, "certainty_stated": i.certainty_stated, "paid_on": i.paid_on.isoformat(),
                "usable_from": i.usable_from.isoformat(), "gross": won(i.gross), "tax_and_insurance": won(i.withholding),
                "tax_basis": i.withholding_basis, "net_after_final_tax": won(i.net),
                "status": "overdue_unconfirmed" if i.overdue else "scheduled"}
        if i.shares:
            item.update(symbol=i.symbol, market=i.market, quantity=str(i.quantity),
                        valued_at=None if i.price is None else str(i.price),
                        priced_on=None if i.priced_on is None else i.priced_on.isoformat())
        elif i.in_stock:
            item.update(paid_in_shares_of=i.symbol, market=i.market)
        items.append(item)
    return {"note": "Not yet paid: never part of net worth or the monthly cash flow. Plans count scheduled pay by "
                    "certainty (adverse: confirmed; base: confirmed and expected; favorable: all). net_after_final_tax "
                    "is after the year's estimated final tax, not the cash received on payday (withholding differs "
                    "and is settled later). Overdue pay is unconfirmed and left out of plans.",
            "items": items, "net_by_certainty": {k: won(v) for k, v in found["by_certainty"].items()},
            "net_by_year": {str(k): won(v) for k, v in found["by_year"].items()},
            "same_stock": [{"symbol": s["symbol"], "market": s["market"], "held": won(s["held"]),
                            "granted_base_scenario": won(s["granted"]), "share_of_securities": ratio(s["share_now"]),
                            "share_of_securities_with_grants": ratio(s["share_with_grants"])}
                           for s in found["same_stock"]]}


def assumptions(items: list[Inflow]) -> list[str]:
    """The assumptions a report states when it shows expected pay (none when there is no such pay)."""
    if not items:
        return []
    return ["예정 소득(성과급·주식 보상): 받기 전까지 순자산·월 현금흐름에 넣지 않음. 불리 시나리오는 확정분만, 기준은 확정+예상, "
            "유리는 가능성까지 포함",
            "주식 보상은 같은 종목의 가장 최근 보유 평가 단가로 평가(없으면 입력한 추정액), 받을 때까지 주가가 시나리오 수익률로 움직인다고 가정",
            "성과급·주식 보상의 세금: 그해 근로소득에 더해지는 소득세·지방소득세 증가분(근로소득공제만 반영)과 건강·장기요양·"
            f"고용보험 약 {percent(tax.PAYROLL_INSURANCE_RATE, 0)}. 세전 기본 연봉을 모르면 한계세율 "
            f"{percent(tax.ASSUMED_MARGINAL_RATE, 0)}(지방소득세 별도) 가정. 세후는 그해 최종 세부담 기준이며, 지급일에 "
            "실제로 받는 돈은 원천징수 방식에 따라 다르고 연말정산·급여 공제로 나중에 맞춰짐. 현행 법령 확인 필요"]
