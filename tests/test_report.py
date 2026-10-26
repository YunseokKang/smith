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
        data = build_report(self.conn, as_of=T1, known_at=T1, baseline=T0, kind="monday")
        change = data["change"]
        parts = change["parts"]
        # Start: 10*100*1300 + 1,000,000 - 500,000 = 1,800,000. End: 1320*1400 + 1,500,000 - 400,000 + 1,000,000.
        self.assertEqual((change["start"], change["end"]), (Decimal(1_800_000), Decimal(3_948_000)))
        # The typo fix restates the start to 1,900,000; it is reported apart and is not part of the change.
        self.assertEqual((change["correction"], change["restated_start"]), (Decimal(100_000), Decimal(1_900_000)))
        self.assertNotIn("correction", parts)
        self.assertEqual(parts["market"], Decimal(130_000))      # 10 shares x 10 USD x 1300
        self.assertEqual(parts["trades"], Decimal(286_000 + 1_000_000))  # 2 shares x 110 x 1300, new house
        self.assertEqual(parts["fx"], Decimal(132_000))          # 1320 USD x 100
        self.assertEqual(parts["cash_savings"], Decimal(400_000))  # 1,100,000 restated -> 1,500,000
        self.assertEqual(parts["debt"], Decimal(100_000))
        self.assertEqual(sum(parts.values()), change["total"])
        self.assertEqual((change["total"], data["kpis"]["net_worth_change"]), (Decimal(2_048_000), Decimal(2_048_000)))
        self.assertIn("실제 변동이 아니어서", render(data)[1])

    def test_a_breakdown_that_does_not_add_up_is_withheld_not_fatal(self):
        from unittest import mock

        from smith import report_data
        with mock.patch.object(report_data, "_record_change", return_value={"market": Decimal(1)}), \
                self.assertLogs("smith.report_data", level="WARNING") as logs:
            data = build_report(self.conn, as_of=T1, known_at=T1, baseline=T0, kind="monday")
        self.assertNotRegex(" ".join(logs.output), r"\d{4,}")    # No amounts in the log.
        self.assertFalse(data["change"]["balanced"])
        _, html = render(data)
        self.assertIn("분해를 싣지 않았습니다", html)
        self.assertNotIn("시장 손익(시세 변동)", html)

    def test_first_report_is_a_baseline_and_thursday_is_short(self):
        data = build_report(self.conn, as_of=T1, known_at=T1, baseline=None, kind="thursday")
        self.assertIsNone(data["change"])
        subject, html = render(data)
        self.assertIn("목요 변화 점검", subject)  # With proposals the subject leads with them.
        self.assertIn("기준선", html)
        self.assertNotIn("자산 구성", html)  # Sector sections are Monday-only.
        self.assertNotIn("데이터 상태", html)  # v3.0: the data status is a system note, not part of the mail.
        self.assertIn("Smith는 조회 전용 자문입니다", html)

    def test_monday_html_escapes_external_titles_and_hides_ledger_ids(self):
        data = build_report(self.conn, as_of=T1, known_at=T1, baseline=T0, kind="monday")
        data["sectors"]["macro"]["announcements"] = [
            Announcement("fed-monetary", "https://www.federalreserve.gov/x", "<script>alert(1)</script>", T1)]
        _, html = render(data)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)
        for section in ("순자산", "자산 구성", "현금·유동성", "부채·금리", "보유 종목 점검", "거시 환경"):
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
        # The one stock is all of the stocks: above the single-company limit, so a trim schedule is proposed too.
        self.assertEqual(set(proposals), {"lease-return", "emergency-reserve", "surplus-plan", "pension-credit",
                                          "prepayment", "variable-rate", "overseas-harvest", "isa-open",
                                          "goal-stocks-glide", "spending-room", "concentration-trim"})
        # Cash 1.5M plus 3M a month for 20 months cannot return the 3억 deposit, so all the shares
        # (12 x 110 USD x 1,400) rest on that date: move them in 5 quarterly steps until 2027-12.
        glide = proposals["goal-stocks-glide"]
        self.assertEqual((glide.priority, glide.figures["from_stocks"], glide.figures["per_quarter"]),
                         (2, Decimal(1_848_000), Decimal(369_600)))
        self.assertEqual(glide.figures["stress"], Decimal(554_400))    # A 30% fall when they must be sold.
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
        self.assertIn("대응 필요", subject)          # Act-now proposals: the status says so.
        self.assertIn("⚡ 금주 핵심 액션: ", html)
        self.assertIn("목표까지 지금 궤도에 있습니까", html)

    def test_glide_plan_moves_only_goal_money_that_cash_cannot_cover(self):
        from smith.holdings import Need, glide_plan
        today = date(2026, 10, 11)
        a, b = Need("A", date(2027, 10, 1), Decimal(30)), Need("B", date(2029, 4, 1), Decimal(20))
        far = Need("far", date(2030, 1, 1), Decimal(500))
        soon = Need("soon", date(2026, 10, 30), Decimal(30))                     # Later this month.

        def res(stocks, cash, monthly, **others):
            return {"self": {"stocks": stocks, "cash": cash, "monthly": monthly}, **others}
        partner = {"partner": {"stocks": Decimal(0), "cash": Decimal(100), "monthly": Decimal(0)}}
        cases = [  # resources, needs -> [(label, from stocks, quarters, started)], "unknown" or None
            (res(None, Decimal(10), Decimal(1)), [a], "unknown"),                 # Unconverted input.
            (res(Decimal(100), Decimal(10), Decimal(1)), [far], None),            # Beyond three years.
            (res(Decimal(100), Decimal(40), Decimal(1)), [a], None),              # Cash and savings cover it.
            # One pool per owner: by A, 30 - (10 + 12) = 8; by B, 50 - (10 + 30) = 10 in all, so 2 more.
            (res(Decimal(100), Decimal(10), Decimal(1)), [b, a], [("A", 8, 2, True), ("B", 2, 6, False)]),
            (res(Decimal(5), Decimal(10), Decimal(1)), [a, b], [("A", 5, 2, True)]),  # Capped by the shares.
            # A deficit shrinks the pool: 30 - (40 - 12) = 2 (review 2026-10-11: it was clipped to zero).
            (res(Decimal(100), Decimal(40), Decimal(-1)), [a], [("A", 2, 2, True)]),
            # Due later this month: no months of saving, moved now in one step (it was skipped as month 0).
            (res(Decimal(100), Decimal(10), Decimal(1)), [soon], [("soon", 20, 1, True)]),
            # Another member's cash never pays the client's goal (it used to make the plan vanish).
            (res(Decimal(100), Decimal(0), Decimal(0), **partner), [Need("A", date(2027, 10, 1), Decimal(100))],
             [("A", 100, 2, True)]),
        ]
        for resources, needs, expected in cases:
            with self.subTest(expected=expected):
                plan = glide_plan(today=today, needs=needs, resources=resources)
                steps = None if plan is None else "unknown" if plan["unknown"] and not plan["steps"] else [
                    (s["label"], s["from_stocks"], s["quarters"], s["started"]) for s in plan["steps"]]
                self.assertEqual(steps, expected)
        plan = glide_plan(today=today, needs=[a, b], resources=res(Decimal(100), Decimal(10), Decimal(1)))
        self.assertEqual((plan["earmarked"], plan["stress"], plan["free"]), (Decimal(10), Decimal(3), Decimal(90)))
        self.assertEqual((plan["steps"][1]["start"], plan["steps"][1]["end"]), (date(2027, 4, 1), date(2028, 10, 1)))

    def test_lots_of_one_symbol_are_one_position(self):
        from smith.holdings import review
        later = datetime(2026, 10, 2, tzinfo=timezone.utc)
        with_cost = dict(stock(1, later, "8", "110"), id="us-2", average_cost="120")
        without_cost = {k: v for k, v in dict(with_cost, id="us-3").items() if k != "average_cost"}
        # (12 x 90 + 8 x 120) / 20 = 102; a lot without a cost makes the whole position's cost unknown.
        for import_id, record, expected in (("lot-a", with_cost, (Decimal(20), Decimal(102), 2)),
                                            ("lot-b", without_cost, (Decimal(28), None, 3))):
            with self.subTest(import_id):
                self.apply(import_id, later, [record])
                data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="monday")
                (position,) = review(data["view"])["positions"]
                self.assertEqual((position["quantity"], position["average_cost"], position["lots"]), expected)
                self.assertEqual(position["value"], expected[0] * 110 * 1400)

    def test_principles_library_and_investment_policy(self):
        from smith import principles
        ids = [p.id for p in principles.LIBRARY]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(p.sources and p.principle and all(label and url.startswith("https://")
                                                              for label, url in p.sources) for p in principles.LIBRARY))
        self.assertEqual(len(principles.BY_ID["BUF-MR-MARKET"].sources), 2)     # 1987 and 2013, each with its URL.
        self.assertEqual(principles.kind({"symbol": "SPYM", "name": "SPYM"}), "broad_index")  # Formerly SPLG.
        for symbol, name, kind in (("VOO", "VOO", "broad_index"), ("069500", "KODEX 200", "broad_index"),
                                   ("QQQ", "QQQ", "narrow_index"), ("SCHD", "SCHD", "fund"),
                                   ("999999", "TIGER 2차전지테마", "fund"), ("123456", "예시전자", "stock")):
            with self.subTest(symbol):
                self.assertEqual(principles.kind({"symbol": symbol, "name": name}), kind)
        data = build_report(self.conn, as_of=T1, known_at=T1, baseline=None, kind="monday")
        checks = {c.key: c for c in data["policy"]}
        self.assertTrue(all(i in principles.BY_ID for c in checks.values() for i in c.principles))
        # One individual stock is all of the stocks: over the single-company limit, no index core, and a
        # credit loan is outstanding while stocks are held.
        self.assertEqual({k: c.status for k, c in checks.items()},
                         {"reserve": "unknown", "leverage": "watch", "dated": "ok", "single": "breach", "core": "breach"})
        self.assertEqual(checks["core"].current, "0.0%")
        _, html = render(data)
        self.assertIn("투자 원칙과 우리 집 투자 기준", html)
        self.assertIn("조정 권장", html)

    def test_holdings_review_edges_from_the_review(self):
        """Securities without a symbol are part of the stocks; one instrument per (market, symbol, currency,
        manager); an unknown market is not overseas; a manual name stays local."""
        from smith import principles, research
        from smith.holdings import review
        later = datetime(2026, 10, 2, tzinfo=timezone.utc)

        def holding(rid, value, **extra):
            return rec(rid, "asset", 1, later, category="stock", account_type="brokerage", currency="KRW", value=value,
                       valuation_method="market", liquidity="days", **extra)
        # Large enough that the fixture's own shares (about 1.8M won) do not matter.
        self.apply("edges", later, [
            holding("voo", "250000000", symbol="VOO", market="US", instrument_name="VOO"),
            holding("plain", "750000000"),                                        # No symbol: make-up unknown.
            holding("aaa", "10000000", symbol="AAA", market="US", instrument_name="김철수만 아는 비밀 종목"),
            holding("aaa-h", "10000000", symbol="AAA", market="US", managed_by="hermes"),
            holding("bbb", "5000000", symbol="BBB")])                             # No market recorded.
        data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="monday")
        r = review(data["view"])
        by = {(p["symbol"], p["hermes"]): p for p in r["positions"]}
        self.assertEqual(r["unlisted"], Decimal(750_000_000))
        self.assertEqual(by[("AAA", False)]["lots"], 1)                         # Hermes lot kept apart.
        self.assertIsNone(by[("BBB", False)]["overseas"])
        self.assertIsNone(r["overseas_share"])                                  # Unknown market: unknown share.
        checks = {c.key: c for c in principles.policy(data)}
        # VOO is far from half once the unlisted 75 counts, but those could be index funds: unknown, not met.
        self.assertEqual((checks["core"].status, checks["single"].status), ("unknown", "unknown"))
        topics = " ".join(t.title + t.question for t in research.holding_topics(data["view"]))
        self.assertNotIn("김철수", topics)                                      # Manual names stay local.
        self.assertIn("시장 미상", topics)

    def test_status_names_missing_inputs_and_judgements_it_could_not_make(self):
        data = build_report(self.conn, as_of=T1, known_at=T1, baseline=None, kind="thursday")
        self.assertIn("cash-flows", [r.key for r in data["requests"] if r.priority == "must"])
        # A variable loan but no CD rate evidence: undetermined, never "all clear".
        self.assertEqual({u.topic for u in data["advice"]["undetermined"]},
                         {"대출 추가 상환과 예금 비교", "변동금리 대출 이자 변화"})
        subject, html = render(data)
        self.assertIn("주의", subject)
        for text in ("다음에 확인할 것", "자료가 부족해 이번에 판단하지 못한 것",
                     "근거의 성격"):                   # Thursday cards keep the certainty label.
            self.assertIn(text, html)
        # The badge's reason is a system note (v3.0), kept for the e-mail answers, not printed in the mail.
        self.assertTrue(any("판단에 꼭 필요한 자료 1건" in note for note in report_html.system_notes(data)))
        self.assertNotIn("데이터가 모두 최신이며 누락이 없습니다", html)

    def test_hermes_assets_are_never_spendable_or_proposed_for_sale(self):
        later = datetime(2026, 10, 2, tzinfo=timezone.utc)
        self.apply("h1", later, [rec("kis", "asset", 1, later, category="stock", account_type="brokerage",
                                     currency="USD", value="2000", valuation_method="market", liquidity="days",
                                     symbol="HRM", market="US", quantity="10", unit_price="200", average_cost="100",
                                     managed_by="hermes")])
        data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="monday")
        cash = data["sectors"]["cash"]
        self.assertEqual(cash["liquidity"]["days"], Decimal(1320 * 1400))    # The client's own US shares only.
        self.assertEqual(cash["hermes"], Decimal(2000 * 1400))
        harvest = next(p for p in data["advice"]["proposals"] if p.key == "overseas-harvest")
        self.assertEqual(harvest.figures["gains"], Decimal(12 * 20 * 1400))  # The Hermes gain is left out.
        self.assertIn("Hermes와 상의", harvest.risks)
        self.assertIn("Hermes 운용", render(data)[1])

    def test_toss_cash_is_shown_as_not_collected(self):
        data = build_report(self.conn, as_of=T1, known_at=T1, baseline=None, kind="monday")
        data["sectors"]["cash"]["toss_cash"] = {"synced": True, "manual_records": 0, "manual_as_of": None,
                                                "buying_power": [{"currency": "KRW", "value": Decimal(500_000),
                                                                  "observed_at": T1}]}
        # A standing data caveat: a system note for the e-mail answers, not mail prose (user review 2026-10-18).
        notes = " ".join(report_html.system_notes(data))
        self.assertIn("미수집", notes)
        self.assertIn("매수 가능 금액은 500,000원", notes)
        self.assertTrue(any(n.startswith("토스증권 예수금: API가 제공하지 않아 미수집") for n in report_html.system_notes(data)))

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
        self.assertIn("환산하지 못해", proposals["lease-return"].why)      # Kept for the e-mail answers ...
        self.assertIn("일부 데이터가 불완전", render(data)[1])                # ... and the mail flags it.

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
        self.assertIsNone(sectors["debt"]["shocks"][0]["monthly_change"])
        self.assertEqual(sectors["pension_insurance"]["monthly_premiums"], Decimal(0))

    def test_rate_scenarios_run_both_ways_and_dollar_assets_get_a_per_100_won_sensitivity(self):
        from smith import narrative
        # Variable loan 400,000 at T1; 12 shares x 110 USD at 1,400 = 1,848,000 won of dollar assets.
        ledger.store_evidence(self.conn, [("ecos", "731Y001/0000001", date(2025, 10, 1), "1300", None),
                                          ("ecos", "731Y001/0000001", date(2026, 10, 1), "1400", None),
                                          # The base rate stepped twice, on the 2026 meeting days of 07-16 and 08-27.
                                          ("ecos", "722Y001/0101000", date(2025, 10, 1), "2.5", None),
                                          ("ecos", "722Y001/0101000", date(2026, 7, 16), "2.75", None),
                                          ("ecos", "722Y001/0101000", date(2026, 8, 27), "3", None),
                                          ("ecos", "722Y001/0101000", date(2026, 10, 1), "3", None)], retrieved_at=T1)
        data = build_report(self.conn, as_of=T1, known_at=T1, baseline=None, kind="rates")
        shocks = {s["shock"]: s["monthly_change"] for s in data["sectors"]["debt"]["shocks"]}
        self.assertEqual(sorted(shocks), [Decimal("-0.01"), Decimal("-0.005"), Decimal("0.005"), Decimal("0.01"), Decimal("0.02")])
        self.assertEqual((shocks[Decimal("0.01")].quantize(Decimal("0.01")), shocks[Decimal("-0.01")]),
                         (Decimal("333.33"), -shocks[Decimal("0.01")]))
        fx = data["sectors"]["macro"]["fx"]
        self.assertEqual((fx["usd_amount"], fx["per_100_won"], fx["change_12m"], fx["year_effect"]),
                         (Decimal(1320), Decimal(132_000), Decimal(100), Decimal(132_000)))
        html = render(data)[1]
        for text in ("1%p 내리면 그만큼 줄어듭니다", "-1.0%p", "▼ -333원", "100원 움직일 때", "1,300원→1,400원", "늘었습니다",
                     # The policy rate's path and the next meeting (report design §28): 400,000 x 0.25% / 12 = 83 won.
                     "한국은행 기준금리 3%는 07.16 +0.25%p, 08.27 +0.25%p 뒤 동결(10.01 기준)입니다.", "다음 금통위 10.22",
                     "다음 금통위는 10월 22일(목)입니다. 0.25%p 움직이면 월 이자가 약 83원 달라집니다."):
            self.assertIn(text, html)
        metrics = narrative.build_payload(data["view"], data, None)["sector_metrics"]
        self.assertEqual(metrics["debt"]["rate_shocks"][0], {"rate_change": "-0.01", "monthly_interest_change": "-333"})
        self.assertEqual((metrics["fx"]["won_change_per_100_won_move"], metrics["fx"]["rate_change_12m_won"]), ("132000", "100"))
        self.assertEqual(metrics["policy"], {"bok_steps_12m": [{"date": "2026-07-16", "rate_change": "0.0025"},
                                                               {"date": "2026-08-27", "rate_change": "0.0025"}],
                                             "fed_upper_steps_12m": [], "next_bok_rate_meeting": "2026-10-22",
                                             "bok_rate_meeting_today": False})
        meeting_day = datetime(2026, 10, 22, tzinfo=timezone.utc)      # 09:00 KST on the October meeting day.
        on_the_day = render(build_report(self.conn, as_of=meeting_day, known_at=meeting_day, baseline=None, kind="rates"))[1]
        self.assertIn("오늘 금통위가 기준금리를 정합니다", on_the_day)
        self.assertIn("오늘 금통위", on_the_day)
        # Without the series the year's effect is unknown, not zero; without dollars there is no sensitivity.
        bare = build_report(self.conn, as_of=T0, known_at=T0, baseline=None, kind="rates")["sectors"]["macro"]["fx"]
        self.assertEqual((bare["year_effect"], bare["per_100_won"]), (None, Decimal(100_000)))
        self.assertNotIn("환율 민감도", render(build_report(ledger.connect(":memory:"), as_of=T1, known_at=T1,
                                                           baseline=None, kind="rates"))[1])

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

    def test_new_and_paid_off_loans_are_debt_changes_not_trades(self):
        start = datetime(2026, 9, 2, tzinfo=timezone.utc)
        new_loan = rec("loan2", "liability", 1, start, category="credit_loan", currency="KRW",
                       outstanding_principal="1000000", annual_rate="0.05", rate_type="fixed", repayment_method="bullet")
        self.apply("d0", start, [new_loan])
        parts = build_report(self.conn, as_of=T1, known_at=T1, baseline=T0, kind="monday")["change"]["parts"]
        self.assertEqual(parts["debt"], Decimal(100_000 - 1_000_000))    # Old loan repaid 100k, new loan 1M.
        self.assertEqual(parts["trades"], Decimal(286_000 + 1_000_000))   # Unchanged: shares and the house only.
        closed = dict(new_loan, revision=2, effective_at=datetime(2026, 9, 21, tzinfo=timezone.utc).isoformat(),
                      status="closed")
        self.apply("d1", datetime(2026, 9, 21, tzinfo=timezone.utc), [closed])
        parts = build_report(self.conn, as_of=T1, known_at=T1, baseline=datetime(2026, 9, 3, tzinfo=timezone.utc),
                             kind="monday")["change"]["parts"]
        self.assertEqual(parts["debt"], Decimal(100_000 + 1_000_000))     # Paid off: debt falls by 1M.

    def test_markets_report_lists_the_holdings_whose_quantity_changed(self):
        from smith import narrative
        data = build_report(self.conn, as_of=T1, known_at=T1, baseline=T0, kind="markets")
        # XYZ went from 10 to 12 shares: 2 shares x 110 USD x 1400, the change at today's price and rate.
        self.assertEqual([(c["symbol"], c["quantity_start"], c["quantity_end"], c["change"], c["amount"])
                          for c in data["holding_quantity_changes"]["changes"]],
                         [("XYZ", Decimal(10), Decimal(12), "more", Decimal(308_000))])
        html = render(data)[1]
        self.assertIn("9월 1일 이후 수량이 바뀐 종목", html)
        self.assertIn("10 → 12", html)
        moved = narrative.holdings_payload(data["view"], data)["holding_quantity_changes"]
        self.assertEqual(moved, {"from": "2026-09-01", "changes": [
            {"symbol": "XYZ", "market": "US", "quantity_before": "10", "quantity_after": "12", "change": "more",
             "value_change": "308000"}], "holdings_not_compared": 0})
        # No comparison report, or a day that does not show the holdings: nothing computed or shown.
        for kind, baseline in (("markets", None), ("property", T0)):
            with self.subTest(kind=kind, baseline=baseline):
                self.assertIsNone(build_report(self.conn, as_of=T1, known_at=T1, baseline=baseline,
                                               kind=kind)["holding_quantity_changes"])
        # Selling the last holding still shows, although no position is left to list.
        sold = datetime(2026, 9, 28, tzinfo=timezone.utc)
        self.apply("sold", sold, [dict(stock(3, sold, "12", "110"), status="closed")])
        html = render(build_report(self.conn, as_of=T1, known_at=T1, baseline=T0, kind="markets"))[1]
        self.assertIn("전량 정리", html)

    def test_quantity_changes_net_lots_and_keep_unknowns_apart(self):
        from smith.report_data import quantity_changes

        def row(symbol, q0, q1, p0="100", p1="130", hermes=False, rate="1300", priced_at=T1):
            return {"name": symbol, "symbol": symbol, "market": "US", "currency": "USD", "hermes": hermes,
                    "priced_at_start": T0, "priced_at_end": priced_at,
                    "quantity_start": None if q0 is None else Decimal(q0),
                    "quantity_end": None if q1 is None else Decimal(q1),
                    "price_start": None if q0 in (None, "0") or p0 is None else Decimal(p0),
                    "price_end": None if q1 in (None, "0") or p1 is None else Decimal(p1),
                    "rate_start": Decimal("1200"), "rate_end": None if rate is None else Decimal(rate)}
        changes = quantity_changes([
            row("MOV", "5", "0"), row("MOV", "0", "5"),                     # Moved between two own accounts.
            row("MOV", "3", "4", hermes=True),                              # Hermes lots stay a position of their own.
            row("NET", "10", "14"), row("NET", "5", "0"),                   # One account grew, one closed: 15 -> 14.
            row("OLD", "2", "0"), row("NEW", "0", None),                    # Sold out; opened without a quantity.
            row("FOG", None, None), row("ODD", "1", None),                  # Unknown whether anything changed.
            row("NOP", "10", "13", p1=None), row("JPY", "1", "2", rate=None),  # No price, no rate: unknown.
            row("STL", "10", "11", p1="50", priced_at=T0), row("STL", "10", "11", p1="110")])  # Newest price wins.
        self.assertEqual(changes["uncompared"], ["FOG", "ODD"])
        self.assertEqual([(c["symbol"], c["hermes"], c["change"], c["amount"]) for c in changes["changes"]],
                         [("STL", False, "more", Decimal(286_000)), ("OLD", False, "closed", Decimal(-240_000)), ("MOV", True, "more", Decimal(169_000)),
                          ("NET", False, "less", Decimal(-169_000)), ("JPY", False, "more", None),
                          ("NEW", False, "new", None), ("NOP", False, "more", None)])
        # Unknown is not "no change": the empty list is scoped and the uncompared holdings are named.
        html = report_html._quantity_changes({"holding_quantity_changes": {"changes": [], "uncompared": ["FOG"]},
                                              "baseline": T0})
        self.assertIn("수량을 비교할 수 있는 종목 가운데", html)
        self.assertIn("비교하지 못한 종목 1개: FOG", html)
        for quantity, shown in (("28.582791", "28.5828"), ("1200.00", "1,200"), ("0.5", "0.5"), (None, "미상")):
            with self.subTest(quantity=quantity):
                self.assertEqual(report_html._qty(None if quantity is None else Decimal(quantity)), shown)

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

    def test_one_report_reads_one_ledger_state_even_if_an_import_lands_mid_build(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from smith import report_data
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "smith.db"
        self.conn = ledger.connect(path)
        self.addCleanup(self.conn.close)
        self.apply("s0", T0, [cash(1, T0, "100")])
        writer = ledger.connect(path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA busy_timeout = 50")
        blocked, original = [], report_data.decompose

        def import_then_decompose(*args, **kwargs):
            mid = datetime(2026, 9, 10, tzinfo=timezone.utc)
            doc = {"schema_version": 1, "import_id": "mid", "source": "manual", "mode": "patch",
                   "as_of": mid.isoformat(), "owners": [{"id": "self"}], "records": [cash(2, mid, "200")]}
            try:
                ledger.apply_import(writer, parse_import(json.dumps(doc)), recorded_at=mid)
            except Exception as error:  # noqa: BLE001 - the report holds the read snapshot.
                blocked.append(type(error).__name__)
            return original(*args, **kwargs)
        with mock.patch.object(report_data, "decompose", import_then_decompose):
            data = build_report(self.conn, as_of=T1, known_at=T1, baseline=T0, kind="monday")
        self.assertEqual(blocked, ["OperationalError"])          # The writer waits for the report.
        self.assertEqual((data["kpis"]["net_worth"], data["change"]["end"]), (Decimal(100), Decimal(100)))

    def test_largest_amount_times_largest_rate_passes_every_path(self):
        # 15-digit amount x 15-digit rate: about 30 integer digits, past the default 28-digit context.
        from smith import narrative, summary
        from smith.payload import load_view
        top = "999999999999999.99999999"
        self.apply("m0", T1, [rec("max", "asset", 1, T1, category="cash", account_type="bank", currency="USD",
                                  value=top, valuation_method="manual", liquidity="immediate")], fx=top)
        data = build_report(self.conn, as_of=T1, known_at=T1, baseline=T0, kind="monday")
        render(data)
        totals = summary.build_summary(self.conn, as_of=T1, known_at=T1)
        summary.to_dict(totals), summary.render(totals)
        json.dumps(narrative.build_payload(load_view(self.conn, as_of=T1, known_at=T1), data, None))


if __name__ == "__main__":
    unittest.main()
