import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from smith import cases, checklist, future_income, ledger, tax
from smith.importer import parse_import
from smith.payload import build_context, check_outbound, load_view
from smith.records import ImportRejected
from smith.report_data import build_report
from smith.report_html import render

AT = datetime(2026, 10, 17, tzinfo=timezone.utc)
SALARY = Decimal(100_000_000)


def rec(record_id, kind, at=AT, **fields):
    return {"id": record_id, "kind": kind, "owner_id": "self", "effective_at": at.isoformat(), "revision": 1,
            "status": "active", **fields}


def stock(record_id, quantity, price, at=AT):
    return rec(record_id, "asset", at, category="stock", account_type="brokerage", currency="KRW",
               value=str(quantity * price), valuation_method="market", liquidity="days", symbol="005930", market="KR",
               quantity=str(quantity), unit_price=str(price))


def pay(record_id, category, on, amount, certainty, basis="gross", **extra):
    return rec(record_id, "cashflow", category=category, direction="inflow", currency="KRW", amount=amount,
               frequency="once", start_date=on, certainty=certainty, amount_basis=basis, **extra)


def grant(record_id, on, quantity, certainty, symbol="005930", amount="30000000", **extra):
    return pay(record_id, "stock_compensation", on, amount, certainty, symbol=symbol, market="KR",
               quantity=str(quantity), **extra)


RECORDS = [
    rec("cash", "asset", category="cash", account_type="bank", currency="KRW", value="100000000",
        valuation_method="manual", liquidity="immediate"),
    rec("home", "asset", category="real_estate", account_type="none", currency="KRW", value="1000000000",
        valuation_method="manual", liquidity="months", occupancy="owner_occupied"),
    # Two lots of the same stock: the newer valuation (120,000 won) prices the grants.
    stock("kr-old", 100, 100000, AT - timedelta(days=10)),
    stock("kr-new", 10, 120000),
    rec("fund", "asset", category="fund", account_type="brokerage", currency="KRW", value="20000000",
        valuation_method="statement", liquidity="days"),
    rec("salary", "cashflow", category="salary", direction="inflow", currency="KRW", amount="5000000",
        frequency="monthly", start_date="2026-01-01"),
    rec("living", "cashflow", category="living_expense", direction="outflow", currency="KRW", amount="3000000",
        frequency="monthly", start_date="2026-01-01"),
    # Received: stacks onto the 2026 pay with its actual taxable amount, and is no longer expected.
    pay("bonus-paid", "bonus", "2026-07-01", "20000000", "confirmed", payment_status="received",
        received_on="2026-07-01", taxable_amount="20000000"),
    pay("bonus-dec", "bonus", "2026-12-24", "10000000", "confirmed"),
    pay("bonus-jan", "bonus", "2027-01-25", "50000000", "confirmed"),
    pay("bonus-next", "bonus", "2028-01-25", "50000000", "expected"),
    # After tax, alone in its year (an earlier after-tax entry makes the year's later tax unknown).
    pay("bonus-net", "bonus", "2029-07-01", "8000000", "expected", basis="net"),
    grant("grant-psu", "2028-11-01", 300, "possible", available_from="2029-11-01"),
    grant("grant-other", "2027-03-01", 10, "confirmed", symbol="000660", amount="5000000"),
    # A won amount paid in shares, two thirds locked up: it adds to the 005930 concentration.
    pay("bonus-stock", "bonus", "2027-04-10", "30000000", "expected", symbol="005930", market="KR",
        available_from="2028-04-10"),
]


def load(records):
    doc = {"schema_version": 1, "import_id": "f1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
           "owners": [{"id": "self"}], "records": records}
    batch = parse_import(json.dumps(doc))
    conn = ledger.connect(":memory:")
    ledger.apply_import(conn, batch, recorded_at=AT)
    return conn


class ImportRuleTests(unittest.TestCase):
    def test_compensation_records_must_state_what_plans_need(self):
        cases_ = {
            "grant without shares": (pay("g", "stock_compensation", "2027-01-01", "1", "confirmed"), "symbol"),
            "recurring bonus": ({**pay("b", "bonus", "2027-01-01", "1", "confirmed"), "frequency": "annual"}, "frequency"),
            "bonus without certainty": ({k: v for k, v in pay("b", "bonus", "2027-01-01", "1", "x").items()
                                         if k != "certainty"}, "certainty"),
            "basis on a salary": (rec("s", "cashflow", category="salary", direction="inflow", currency="KRW",
                                      amount="1", frequency="monthly", start_date="2026-01-01", amount_basis="gross"),
                                  "amount_basis"),
            "certainty on spending": (rec("o", "cashflow", category="living_expense", direction="outflow",
                                          currency="KRW", amount="1", frequency="monthly", start_date="2026-01-01",
                                          certainty="confirmed"), "certainty"),
            "usable before paid": (grant("g", "2027-01-01", 1, "confirmed", available_from="2026-12-01"),
                                   "available_from"),
            "bonus with a share count": (pay("b", "bonus", "2027-01-01", "1", "confirmed", symbol="005930",
                                             market="KR", quantity="1"), "quantity"),
            "received without its facts": (pay("b", "bonus", "2026-07-01", "1", "confirmed", payment_status="received",
                                               received_on="2026-07-02"), "taxable_amount"),
            "scheduled with a receipt day": (pay("b", "bonus", "2027-01-01", "1", "confirmed",
                                                 received_on="2027-01-02"), "received_on"),
            "status on a salary": (rec("s", "cashflow", category="salary", direction="inflow", currency="KRW",
                                       amount="1", frequency="monthly", start_date="2026-01-01",
                                       payment_status="scheduled"), "payment_status"),
        }
        for name, (record, field) in cases_.items():
            with self.subTest(name), self.assertRaises(ImportRejected) as raised:
                load([record])
            self.assertTrue(any(f".{field}:" in p for p in raised.exception.problems), raised.exception.problems)
        # Closing a grant once it is paid needs none of its fields.
        closing = {**rec("g", "cashflow", category="stock_compensation", direction="inflow"), "status": "closed"}
        load([closing]).close()


class FutureIncomeTests(unittest.TestCase):
    def setUp(self):
        conn = load(RECORDS)
        self.addCleanup(conn.close)
        self.conn = conn
        self.view = load_view(conn, as_of=AT, known_at=AT)
        self.items = {i.record_id: i for i in future_income.inflows(self.view, salary=SALARY)}

    def test_earned_income_tax_follows_the_deduction_and_brackets(self):
        # 100M gross: 14.75M deduction, 85.25M taxed in the 24% band (-5.76M), plus 10% local tax.
        self.assertEqual(tax.earned_income_tax(Decimal(100_000_000)), Decimal("16170000.00"))

    def test_only_unpaid_pay_is_listed_and_grants_use_the_newest_held_price(self):
        self.assertNotIn("bonus-paid", self.items)
        psu, other = self.items["grant-psu"], self.items["grant-other"]
        self.assertEqual((psu.gross, psu.price, psu.priced_on), (Decimal(36_000_000), Decimal(120000), AT.date()))
        # No holding of that stock: the entered estimate stands.
        self.assertEqual((other.gross, other.price), (Decimal(5_000_000), None))
        self.assertEqual(psu.usable_from, date(2029, 11, 1))

    def test_bonuses_stack_on_the_years_pay_and_net_amounts_are_kept(self):
        dec = self.items["bonus-dec"]
        # The July bonus already paid sits under December's: 100M salary + 20M.
        expected = (tax.earned_income_tax(Decimal(130_000_000)) - tax.earned_income_tax(Decimal(120_000_000))
                    + Decimal(10_000_000) * tax.PAYROLL_INSURANCE_RATE)
        self.assertEqual((dec.withholding, dec.withholding_basis), (expected, "estimated"))
        # An after-tax entry has no known gross: it is never shown as a gross amount.
        net_entry = self.items["bonus-net"]
        self.assertEqual((net_entry.gross, net_entry.net, net_entry.withholding_basis), (None, Decimal(8_000_000), "net_input"))
        assumed = future_income.inflows(self.view)[0]
        self.assertEqual((assumed.withholding, assumed.withholding_basis),
                         (Decimal(10_000_000) * (Decimal("0.418") + Decimal("0.05")), "assumed"))

    def test_home_plan_counts_pay_by_certainty_and_keeps_the_plan_without_it(self):
        items = list(self.items.values())
        facts = cases.home_facts(self.view, target_price=Decimal(2_000_000_000), target_date=date(2029, 10, 1),
                                 pay=items)
        without = cases.home_facts(self.view, target_price=Decimal(2_000_000_000), target_date=date(2029, 10, 1),
                                   pay=[])
        refs = {v: k for k, v in self.view.aliases.items()}
        counted = {s["name"]: {refs[r] for r in s["expected_pay_refs"]} for s in facts["scenarios"]}
        sure = {"bonus-dec", "bonus-jan", "grant-other"}
        # The PSU is usable only after the target date: listed apart in the favourable scenario.
        expected = {"bonus-next", "bonus-net", "bonus-stock"}
        self.assertEqual(counted, {"adverse": sure, "base": sure | expected, "favorable": sure | expected})
        favorable = next(s for s in facts["scenarios"] if s["name"] == "favorable")
        self.assertAlmostEqual(Decimal(favorable["expected_pay_after_target"]), self.items["grant-psu"].net, delta=1)
        for s, plain in zip(facts["scenarios"], without["scenarios"]):
            with self.subTest(s["name"]):
                self.assertEqual(s["available_self"], plain["available_self"])
                gap = Decimal(s["required_with_costs"]) - Decimal(s["available_self_with_expected_pay"])
                self.assertAlmostEqual(Decimal(s["gap_self_with_expected_pay"]), gap, delta=1)
                self.assertLess(Decimal(s["gap_self_with_expected_pay"]), Decimal(s["gap_self"]))

    def test_payment_is_stated_never_inferred_from_dates_or_closing(self):
        """The reviewer's case: salary 100M, a July bonus of 20M, a December one of 10M (to estimate)."""
        july = pay("july", "bonus", "2026-07-01", "20000000", "confirmed")
        received = {**july, "payment_status": "received", "received_on": "2026-07-03", "taxable_amount": "20000000"}
        closed = {**rec("july", "cashflow"), "status": "closed"}
        december = pay("december", "bonus", "2026-12-24", "10000000", "confirmed")
        salary = rec("salary", "cashflow", category="salary", direction="inflow", currency="KRW", amount="5000000",
                     frequency="monthly", start_date="2026-01-01")
        later = AT + timedelta(minutes=1)
        for name, update, tax_due, overdue in (("received", received, "4273000", False),
                                                ("cancelled", closed, "3940250", False),
                                                ("not yet confirmed", None, "4273000", True)):
            with self.subTest(name):
                conn = load([salary, july, december])
                if update is not None:
                    doc = {"schema_version": 1, "import_id": "f2", "source": "manual", "mode": "patch",
                           "as_of": later.isoformat(), "owners": [{"id": "self"}],
                           "records": [{**update, "effective_at": later.isoformat(), "revision": 2}]}
                    ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=later)
                view = load_view(conn, as_of=later, known_at=later)
                found = {i.record_id: i for i in future_income.inflows(view, salary=SALARY)}
                self.assertEqual(found["december"].withholding, Decimal(tax_due))
                self.assertEqual("july" in found and found["july"].overdue, overdue)
                # Unconfirmed past pay stays listed for the client but out of every plan, and asks to be confirmed.
                counted = future_income.at_goal(list(found.values()), owner_id="self", scenario="favorable",
                                                horizon=date(2027, 1, 1), today=AT.date(), rate=Decimal(0))
                self.assertEqual(counted["before_refs"], ["december"])
                requests = {r.key: r.priority for r in checklist.requests(view, {"annual_gross_salary": int(SALARY)})}
                self.assertEqual(requests.get("pay-overdue"), "must" if overdue else None)
                self.assertEqual("expected-pay" in requests, False)
                conn.close()

    def test_unknown_earlier_pay_and_known_zero_are_not_guessed(self):
        records = [pay("usd", "bonus", "2027-01-10", "1000", "confirmed"),  # No USD rate in this ledger.
                   pay("krw", "bonus", "2027-02-10", "1000000", "confirmed")]
        records[0]["currency"] = "USD"
        conn = load(records + [grant("first-grant", "2028-03-01", 10, "confirmed", symbol="000660")])
        self.addCleanup(conn.close)
        view = load_view(conn, as_of=AT, known_at=AT)
        found = {i.record_id: i for i in future_income.inflows(view, salary=SALARY)}
        # The salary is known; the bracket is not, because the earlier payment has no value in won.
        self.assertEqual((found["krw"].withholding_basis, found["krw"].net), ("prior_unknown", None))
        review = future_income.review(view, list(found.values()))
        self.assertFalse(review["assumed_tax"])
        # No securities yet: 0% now is known, so the first grant still warns (100% after it).
        stock_row = review["same_stock"][0]
        self.assertEqual((stock_row["share_now"], stock_row["share_with_grants"]), (Decimal(0), Decimal(1)))

    def test_retirement_gets_only_pay_the_home_goal_does_not_need(self):
        items = [i for i in self.items.values() if i.certainty in ("confirmed", "expected")]
        before, after = items[0], replace(items[0], record_id="late", usable_from=date(2030, 1, 1))
        home = {"target_date": date(2029, 10, 1), "required": Decimal(100), "pay_at_target": Decimal(40)}
        cases_ = {"no home goal": (None, False, [before.net, after.net]),
                  "home needs all of it": ({**home, "available_with_pay": Decimal(90)}, True, [Decimal(0), after.net]),
                  "home needs half": ({**home, "available_with_pay": Decimal(120)}, True, [before.net / 2, after.net]),
                  "home unknown": (None, True, None)}
        for name, (goal, has_goal, expected) in cases_.items():
            with self.subTest(name):
                share = future_income.retirement_share([before, after], home=goal, home_goal=has_goal)
                self.assertEqual(None if share is None else [amount for _, amount in share], expected)
        extra = [(date(2027, 1, 1), Decimal(1_000_000)), (date(2060, 1, 1), Decimal(9_000_000))]
        # Usable in 2027, retired in 2050: 23 years at the real return; pay after retirement is not counted.
        self.assertEqual(tax.set_aside(extra, 2050), Decimal(1_000_000) * (1 + tax.REAL_RETURN) ** 23)
        self.assertEqual(tax.set_aside(extra, None), Decimal(10_000_000))
        self.assertIsNone(tax.set_aside(None, 2050))                         # Unknown, not zero.

    def test_unknown_amount_makes_the_goal_figure_unknown_not_smaller(self):
        unknown = future_income.Inflow("x", "self", "stock_compensation", "confirmed", True, date(2027, 1, 1),
                                       date(2027, 1, 1), "VOO", "US", Decimal(1), None, None, None, None, "unknown")
        found = future_income.at_goal([unknown], owner_id="self", scenario="adverse", horizon=date(2029, 1, 1),
                                      today=AT.date(), rate=Decimal(0))
        self.assertIsNone(found["value"])

    def test_model_context_carries_pay_without_identifiers_and_the_report_shows_it(self):
        found = future_income.review(self.view, list(self.items.values()))
        context = build_context(self.view, {}, include_positions=True,
                                expected_pay=future_income.context(self.view, found))
        check_outbound(json.dumps(context, ensure_ascii=False), [])
        psu = next(i for i in context["expected_pay"]["items"] if i["category"] == "stock_compensation"
                   and i["certainty"] == "possible")
        self.assertEqual((psu["symbol"], psu["quantity"], psu["usable_from"]), ("005930", "300", "2029-11-01"))
        self.assertTrue(psu["ref"].startswith("C"))
        # Concentration counts what the base scenario adds after tax: the bonus paid in shares, not the merely
        # possible PSU.
        same = next(s for s in found["same_stock"] if s["symbol"] == "005930")
        self.assertEqual(same["granted"], self.items["bonus-stock"].net)
        self.assertGreater(same["share_with_grants"], same["share_now"])
        # The full report carries the section whatever the daily editions hold (their make-up is tested apart).
        data = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="monday",
                            household={"annual_gross_salary": int(SALARY)})
        html = render(data)[1]
        self.assertIn("앞으로 들어올 돈", html)
        self.assertIn("최종 세후", html)
        self.assertTrue(any(i["kind"] == "inflow" for i in data["timeline"]))


if __name__ == "__main__":
    unittest.main()
