import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from smith import ledger, report_html
from smith.announcements import Announcement
from smith.importer import parse_import
from smith.records import ImportBatch, Observation
from smith.report_data import build_report
from smith.report_html import render

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def rec(record_id, kind, revision=1, effective=T0, **fields):
    return {"id": record_id, "kind": kind, "owner_id": "self", "effective_at": effective.isoformat(),
            "revision": revision, "status": "active", **fields}


def stock(revision, effective, quantity, price):
    value = str(Decimal(quantity) * Decimal(price))
    return rec("us-1", "asset", revision, effective, category="stock", account_type="brokerage", currency="USD",
               value=value, valuation_method="market", liquidity="days", symbol="XYZ", market="US",
               quantity=quantity, unit_price=price, average_cost="90")


def cash(revision, effective, value, **extra):
    return rec("cash", "asset", revision, effective, category="cash", account_type="bank", currency="KRW",
               value=value, valuation_method="manual", liquidity="immediate", **extra)


def loan(revision, effective, principal):
    return rec("loan", "liability", revision, effective, category="credit_loan", currency="KRW",
               outstanding_principal=principal, annual_rate="0.05", rate_type="variable", repayment_method="bullet")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.apply("i0", T0, [stock(1, T0, "10", "100"), cash(1, T0, "1000000"), loan(1, T0, "500000")], fx="1300")
        later = datetime(2026, 9, 20, tzinfo=timezone.utc)
        self.apply("i1", later, [stock(2, later, "12", "110"), cash(2, later, "1500000"), loan(2, later, "400000"),
                                 rec("house", "asset", 1, later, category="real_estate", account_type="none",
                                     currency="KRW", value="1000000", valuation_method="appraisal", liquidity="months")],
                   fx="1400")
        # A correction recorded after T0 restates the starting cash balance; it is not a real change.
        self.apply("i2", datetime(2026, 9, 25, tzinfo=timezone.utc),
                   [cash(3, T0, "1100000", change_type="correction", corrects_revision=1, reason="typo")])

    def apply(self, import_id, at, records, fx=None):
        doc = {"schema_version": 1, "import_id": import_id, "source": "manual", "mode": "patch",
               "as_of": at.isoformat(), "owners": [{"id": "self"}], "records": records}
        ledger.apply_import(self.conn, parse_import(json.dumps(doc)), recorded_at=at)
        if fx:
            batch = ImportBatch(f"fx-{import_id}", "toss", "snapshot", at, ("self",), (), "x",
                                (Observation("USD", "fx_mid_rate", "KRW", fx, at),))
            ledger.apply_import(self.conn, batch, recorded_at=at)

    def test_change_decomposition_adds_up_and_separates_causes(self):
        change = build_report(self.conn, as_of=T1, known_at=T1, baseline=T0, kind="monday")["change"]
        parts = change["parts"]
        # Start: 10*100*1300 + 1,000,000 - 500,000 = 1,800,000. End: 1320*1400 + 1,500,000 - 400,000 + 1,000,000.
        self.assertEqual((change["start"], change["end"]), (Decimal(1_800_000), Decimal(3_948_000)))
        self.assertEqual(parts["correction"], Decimal(100_000))
        self.assertEqual(parts["market"], Decimal(130_000))      # 10 shares x 10 USD x 1300
        self.assertEqual(parts["trades"], Decimal(286_000 + 1_000_000))  # 2 shares x 110 x 1300, new house
        self.assertEqual(parts["fx"], Decimal(132_000))          # 1320 USD x 100
        self.assertEqual(parts["cash_savings"], Decimal(400_000))  # 1,100,000 restated -> 1,500,000
        self.assertEqual(parts["debt"], Decimal(100_000))
        self.assertEqual(sum(parts.values()), change["total"])

    def test_first_report_is_a_baseline_and_thursday_is_short(self):
        data = build_report(self.conn, as_of=T1, known_at=T1, baseline=None, kind="thursday")
        self.assertIsNone(data["change"])
        subject, html = render(data)
        self.assertIn("기준선", subject)
        self.assertIn("기준선", html)
        self.assertNotIn("자산 구성", html)  # Sector sections are Monday-only.
        self.assertIn("데이터 상태", html)

    def test_monday_html_escapes_external_titles_and_hides_ledger_ids(self):
        data = build_report(self.conn, as_of=T1, known_at=T1, baseline=T0, kind="monday")
        data["sectors"]["macro"]["announcements"] = [
            Announcement("fed-monetary", "https://www.federalreserve.gov/x", "<script>alert(1)</script>", T1)]
        _, html = render(data)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)
        for section in ("순자산", "자산 구성", "현금·유동성", "부채·금리", "주식·증권", "거시 환경", "용어 풀이"):
            self.assertIn(section, html)
        for internal in ("us-1", "loan", "variable_rate_debt", "manual"):
            self.assertNotIn(f">{internal}<", html)
        self.assertIn('href="https://www.federalreserve.gov/x"', html)  # Sources stay linked (AC-04).

    def test_proposals_are_computed_from_the_household_numbers(self):
        later = datetime(2026, 10, 2, tzinfo=timezone.utc)
        self.apply("p1", later, [
            rec("pay", "cashflow", 1, later, category="salary", direction="inflow", currency="KRW", amount="5000000",
                frequency="monthly", start_date="2026-01-01"),
            rec("living", "cashflow", 1, later, category="living_expense", direction="outflow", currency="KRW",
                amount="2000000", frequency="monthly", start_date="2026-01-01"),
            rec("lease", "liability", 1, later, category="lease_deposit_obligation", currency="KRW",
                outstanding_principal="300000000", annual_rate="0", rate_type="fixed", repayment_method="bullet",
                maturity="2028-06-30"),
            rec("pension", "asset", 1, later, category="fund", account_type="pension_savings", currency="KRW",
                value="10000000", valuation_method="market", liquidity="restricted")])
        ledger.store_evidence(self.conn, [("ecos", "817Y002/010502000", date(2025, 10, 1), "3.00", None),
                                          ("ecos", "817Y002/010502000", date(2026, 10, 1), "2.50", None)],
                              retrieved_at=later)
        data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="monday")
        proposals = {p.key: p for p in data["advice"]["proposals"]}
        self.assertEqual(set(proposals), {"lease-return", "emergency-reserve", "surplus-plan", "pension-credit",
                                          "prepayment", "variable-rate"})
        # Act-now items: a deposit savings cannot cover within two years, cash under one month of spending,
        # and the year-end tax deadline in Q4.
        self.assertEqual([p.key for p in data["advice"]["proposals"][:3]],
                         ["lease-return", "emergency-reserve", "pension-credit"])
        self.assertLess(proposals["lease-return"].figures["accumulated"], Decimal(300_000_000))
        self.assertEqual(proposals["surplus-plan"].figures["surplus"], Decimal(3_000_000))
        self.assertEqual(proposals["lease-return"].figures["months"], Decimal(20))
        # 0.5%p lower market rate on the 400,000 won variable loan: about 167 won less interest a month.
        self.assertEqual(proposals["variable-rate"].figures["monthly_change"].quantize(Decimal(1)), Decimal(-167))
        self.assertIn("이어가시는 것이 합리적", proposals["prepayment"].title)  # 5% loan > 2.5% x (1 - 15.4%).
        subject, html = render(data)
        self.assertIn("제안 3건", subject)
        self.assertIn("이번 주 가장 중요한 한 가지", html)
        self.assertIn("목표까지 지금 궤도에 있습니까", html)

    def test_proposals_never_treat_unconverted_amounts_as_known(self):
        later = datetime(2026, 10, 2, tzinfo=timezone.utc)
        self.apply("u1", later, [
            rec("eur-cash", "asset", 1, later, category="cash", account_type="bank", currency="EUR", value="5000",
                valuation_method="manual", liquidity="immediate"),
            rec("pay", "cashflow", 1, later, category="salary", direction="inflow", currency="KRW", amount="5000000",
                frequency="monthly", start_date="2026-01-01"),
            rec("living", "cashflow", 1, later, category="living_expense", direction="outflow", currency="KRW",
                amount="2000000", frequency="monthly", start_date="2026-01-01"),
            rec("lease", "liability", 1, later, category="lease_deposit_obligation", currency="KRW",
                outstanding_principal="300000000", annual_rate="0", rate_type="fixed", repayment_method="bullet",
                maturity="2028-06-30")])
        data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="monday")
        self.assertIsNone(data["kpis"]["immediate"])
        proposals = {p.key: p for p in data["advice"]["proposals"]}
        self.assertNotIn("emergency-reserve", proposals)              # Unknown, not a confident shortfall.
        self.assertNotIn("accumulated", proposals["lease-return"].figures)
        tracks = {t.name: t for t in data["advice"]["strategy"]}
        self.assertEqual(tracks["전세보증금 반환"].status, "unknown")
        self.assertEqual(tracks["노후 준비"].headline, "연금 계좌 정보 없음")  # No accounts is not 0 won.
        self.assertIn("환산하지 못해", render(data)[1])

    def test_prepayment_compares_the_highest_rate_loan(self):
        later = datetime(2026, 10, 2, tzinfo=timezone.utc)
        self.apply("r1", later, [
            rec("big", "liability", 1, later, category="mortgage", currency="KRW", outstanding_principal="1000000000",
                annual_rate="0.02", rate_type="variable", repayment_method="equal_principal"),
            rec("dear", "liability", 1, later, category="credit_loan", currency="KRW", outstanding_principal="100000000",
                annual_rate="0.06", rate_type="fixed", repayment_method="bullet")])
        ledger.store_evidence(self.conn, [("ecos", "817Y002/010502000", date(2026, 10, 1), "3.00", None)],
                              retrieved_at=later)
        data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="monday")
        prepayment = next(p for p in data["advice"]["proposals"] if p.key == "prepayment")
        self.assertEqual(prepayment.figures["loan_rate"], Decimal("0.06"))   # Not the largest (2%) loan.
        self.assertIn("이어가시는 것이 합리적", prepayment.title)

    def test_summary_amounts_are_exact_for_the_largest_importable_values(self):
        at = datetime(2026, 9, 3, tzinfo=timezone.utc)
        value, rate = "888888888888888.12345678", "777777777777777.87654321"
        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)
        self.conn = conn
        self.apply("x0", at, [rec("usd", "asset", 1, at, category="cash", account_type="bank", currency="USD",
                                  value=value, valuation_method="manual", liquidity="immediate")], fx=rate)
        summary = build_report(conn, as_of=T1, known_at=T1, baseline=None, kind="monday")["view"].summary
        with localcontext() as context:
            context.prec = 100
            self.assertEqual(summary.total_assets, Decimal(value) * Decimal(rate))

    def test_links_must_be_whole_plain_urls(self):
        self.assertEqual(report_html._cell(("출처", "https://host/path suffix")), "출처")
        self.assertIn('href="https://host/path"', report_html._cell(("출처", "https://host/path")))

    def test_calendar_dates_use_the_household_timezone(self):
        # Monday 06:00 KST is Sunday 21:00 UTC; a salary starting on Monday counts in that report.
        monday = datetime(2026, 10, 5, 6, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        self.apply("i3", T1, [rec("pay", "cashflow", 1, T1, category="salary", direction="inflow", currency="KRW",
                                  amount="1000000", frequency="monthly", start_date="2026-10-05")])
        data = build_report(self.conn, as_of=monday.astimezone(timezone.utc), known_at=T1, baseline=None, kind="monday")
        self.assertEqual(data["view"].summary.monthly_inflow, Decimal(1_000_000))
        self.assertIn("10/05", render(data)[0])

    def test_unconverted_and_ended_amounts_are_unknown_not_zero(self):
        later = datetime(2026, 10, 2, tzinfo=timezone.utc)
        self.apply("i3", later, [
            rec("eur-loan", "liability", 1, later, category="credit_loan", currency="EUR", outstanding_principal="1000",
                annual_rate="0.04", rate_type="variable", repayment_method="bullet"),
            rec("old-premium", "cashflow", 1, later, category="insurance_premium", direction="outflow",
                currency="KRW", amount="120000", frequency="monthly", start_date="2026-01-01", end_date="2026-09-30")])
        sectors = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="monday")["sectors"]
        self.assertIsNone(sectors["debt"]["variable_total"])
        self.assertIsNone(sectors["debt"]["shocks"][0]["monthly_increase"])
        self.assertEqual(sectors["pension_insurance"]["monthly_premiums"], Decimal(0))

    def test_new_foreign_records_need_only_the_current_rate(self):
        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)
        self.conn, start = conn, datetime(2026, 9, 2, tzinfo=timezone.utc)
        self.apply("n0", T0, [cash(1, T0, "1000")])
        self.apply("n1", start, [rec("usd", "asset", 1, start, category="cash", account_type="bank", currency="USD",
                                     value="100", valuation_method="manual", liquidity="immediate")], fx="1300")
        change = build_report(conn, as_of=T1, known_at=T1, baseline=T0, kind="monday")["change"]
        self.assertTrue(change["complete"])
        self.assertEqual((change["total"], change["parts"]["trades"]), (Decimal(130_000), Decimal(130_000)))

    def test_decomposition_is_exact_for_the_largest_importable_values(self):
        big = datetime(2026, 9, 3, tzinfo=timezone.utc)
        q, p0, p1 = "999999999999999.12345678", "999999999999999.87654321", "999999999999998.11111111"
        self.apply("b0", big, [rec("big", "asset", 1, big, category="stock", account_type="brokerage", currency="USD",
                                   value=p0, valuation_method="market", liquidity="days", quantity=q, unit_price=p0)])
        self.apply("b1", T1, [rec("big", "asset", 2, T1, category="stock", account_type="brokerage", currency="USD",
                                  value=p1, valuation_method="market", liquidity="days", quantity=q, unit_price=p1)],
                   fx="1399.12345678")
        change = build_report(self.conn, as_of=T1, known_at=T1, baseline=big, kind="monday")["change"]
        self.assertLessEqual(abs(sum(change["parts"].values()) - change["total"]), Decimal("0.5"))


if __name__ == "__main__":
    unittest.main()
