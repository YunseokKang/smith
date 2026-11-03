import json
import unittest
from types import SimpleNamespace
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from smith import ledger, narrative, tax
from smith.importer import parse_import
from smith.records import ChangeType, ImportBatch, ImportRejected, Kind, Observation, RecordInput, Status
from smith.report_data import build_report
from smith.report_html import render

AT = datetime(2026, 10, 5, tzinfo=timezone.utc)
HOUSEHOLD = {"birth_year": 1989, "retirement_monthly_spend": 6_000_000, "marriage_registered": False, "cohabiting": True}


def rec(record_id, kind, owner="self", **fields):
    return {"id": record_id, "kind": kind, "owner_id": owner, "effective_at": AT.isoformat(), "revision": 1,
            "status": "active", **fields}


RECORDS = [
    rec("cash", "asset", category="cash", account_type="bank", currency="KRW", value="30000000",
        valuation_method="manual", liquidity="immediate"),
    rec("pension", "asset", category="fund", account_type="pension_savings", currency="KRW", value="60000000",
        valuation_method="statement", liquidity="restricted"),
    rec("irp", "asset", category="fund", account_type="irp", currency="KRW", value="10000000",
        valuation_method="statement", liquidity="restricted"),
    rec("voo", "asset", category="stock", account_type="brokerage", currency="USD", value="20000",
        valuation_method="market", liquidity="days", symbol="VOO", market="US", quantity="40", unit_price="500",
        average_cost="400"),
    rec("home", "asset", category="real_estate", account_type="none", currency="KRW", value="1650000000",
        valuation_method="manual", liquidity="months", occupancy="owner_occupied"),
    rec("home2", "asset", owner="partner", category="real_estate", account_type="none", currency="KRW",
        value="1200000000", valuation_method="manual", liquidity="months", occupancy="leased_out"),
    rec("pay", "cashflow", category="salary", direction="inflow", currency="KRW", amount="9000000",
        frequency="monthly", start_date="2026-01-01"),
    rec("living", "cashflow", category="living_expense", direction="outflow", currency="KRW", amount="3000000",
        frequency="monthly", start_date="2026-01-01"),
    rec("to-pension", "cashflow", category="internal_transfer", direction="outflow", currency="KRW", amount="990000",
        frequency="monthly", start_date="2026-06-01", target_record_id="pension"),
]


def _toss(conn, import_id, at, positions, fx=None):
    """A Toss snapshot of USD positions (symbol, name, quantity, price, average cost[, account])."""
    records = tuple(RecordInput(f"toss-{(rest or ['a'])[0] * 8}-{symbol.lower()}", Kind.ASSET, "self", at, 0, Status.ACTIVE,
                                ChangeType.UPDATE, None, None,
                                {"category": "stock", "account_type": "brokerage", "currency": "USD",
                                 "value": str(Decimal(qty) * Decimal(price)), "valuation_method": "api",
                                 "liquidity": "days", "symbol": symbol, "instrument_name": name, "market": "US",
                                 "quantity": qty, "unit_price": price, "average_cost": cost})
                    for symbol, name, qty, price, cost, *rest in positions)
    observations = () if fx is None else (Observation("USD", "fx_mid_rate", "KRW", fx, at),)
    ledger.apply_import(conn, ImportBatch(import_id, "toss", "snapshot", at, ("self",), records, import_id, observations),
                        recorded_at=at, close_missing=True)


class TaxTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.connect(":memory:")
        self.addCleanup(self.conn.close)
        doc = {"schema_version": 1, "import_id": "t1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}, {"id": "partner"}], "records": RECORDS}
        ledger.apply_import(self.conn, parse_import(json.dumps(doc)), recorded_at=AT)
        ledger.apply_import(self.conn, ImportBatch("fx", "toss", "snapshot", AT, ("self",), (), "x",
                                                   (Observation("USD", "fx_mid_rate", "KRW", "1400", AT),)), recorded_at=AT)

    def data(self):
        return build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="monday", household=HOUSEHOLD)

    def test_target_account_is_only_for_transfers(self):
        bad = dict(RECORDS[6], id="pay2", target_record_id="pension")
        doc = {"schema_version": 1, "import_id": "t2", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": [bad]}
        with self.assertRaises(ImportRejected):
            parse_import(json.dumps(doc))

    def test_pension_contributions_and_credit_room(self):
        t = tax.facts(self.data()["view"], date(2026, 10, 5), HOUSEHOLD)
        plan = tax.pension_plan(t)
        self.assertEqual(plan["savings_paid"], Decimal(6_930_000))     # June to December, 7 x 990,000.
        self.assertEqual(plan["excess"], Decimal(930_000))             # Above the 6M pension savings limit.
        self.assertEqual(plan["irp_room"], Decimal(3_000_000))         # 9M total minus 6M credited, no IRP yet.
        self.assertEqual(plan["refund_gain"], Decimal("396000.000"))   # 3M x 13.2%.
        # 99만 a month into pension savings alone: 50만 stays, 25만 moves to the IRP, 24만 has no credit to earn.
        self.assertEqual((plan["next_savings"], plan["next_irp"], plan["next_rest"]),
                         (Decimal(500_000), Decimal(250_000), Decimal(240_000)))
        self.assertEqual(plan["credit_gain"], Decimal(3_000_000))      # 6M credited now, 9M with the split.
        # The split keeps what is already optimal, adds no money and only moves what earns more credit.
        cases = {"80만 + 20만": ((800_000, 200_000), (500_000, 250_000, 250_000)),
                 "30만 + 0": ((300_000, 0), (300_000, 0, 0)),
                 "60만 + 0": ((600_000, 0), (500_000, 100_000, 0)),
                 "30만 + 50만": ((300_000, 500_000), (300_000, 450_000, 50_000))}
        for name, ((savings, irp), expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(tax.credit_split(Decimal(savings), Decimal(irp)), tuple(Decimal(x) for x in expected))
        self.assertEqual(tax.yearly_credit_base(Decimal(990_000), Decimal(250_000)), Decimal(9_000_000))  # Already full.

    def test_the_split_proposal_says_when_the_credit_is_already_full(self):
        # IRP 30만 a month beside the fixture's pension savings: next year's credit is full either way (review 2026-10-23).
        doc = {"schema_version": 1, "import_id": "t4", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": [
                   rec("irp-account", "asset", category="fund", account_type="irp", currency="KRW", value="1000000",
                       valuation_method="statement", liquidity="restricted"),
                   rec("to-irp", "cashflow", category="internal_transfer", direction="outflow", currency="KRW",
                       amount="300000", frequency="monthly", start_date="2026-01-01", target_record_id="irp-account")]}
        ledger.apply_import(self.conn, parse_import(json.dumps(doc)), recorded_at=AT)
        card = next(p for p in self.data()["advice"]["proposals"] if p.key == "pension-credit")
        self.assertEqual((card.priority, card.headline, card.timing), (3, "공제 밖 연금 납입, ISA 검토", "2027년 1월 납입부터"))
        self.assertIn("나눠도 늘지 않습니다", card.effect)
        self.assertIn("손해가 아니라 선택", card.effect)
        self.assertNotIn("빠짐없이 받습니다", card.effect)

    def test_overseas_gains_and_retirement(self):
        t = tax.facts(self.data()["view"], date(2026, 10, 5), HOUSEHOLD)
        harvest = tax.harvest_plan(t)
        self.assertEqual(harvest["gains"], Decimal(5_600_000))         # (500 - 400) x 40 shares x 1,400.
        self.assertEqual((harvest["realize"], harvest["saving"]), (Decimal(2_500_000), Decimal("550000.00")))
        # Whole shares within the deduction (17 x 140,000 = 2.38M) and the last US trading day that settles in 2026
        # (T+1 on Thursday 31 December), named on the card (user review 2026-11-02).
        self.assertEqual(harvest["sell"], [{"symbol": "VOO", "shares": 17, "gain": Decimal(2_380_000)}])
        self.assertEqual(harvest["trade_by"], date(2026, 12, 30))
        card = next(p for p in self.data()["advice"]["proposals"] if p.key == "overseas-harvest")
        self.assertEqual(card.headline, "연내 VOO 17주 팔고 다시 사기(이익 238만 원)")
        self.assertTrue(card.title.startswith("미국 현지 12월 30일(수) 거래까지 VOO 17주를 팔았다가 다시 사서"), card.title)
        self.assertEqual([tax.us_year_end_trade_day(y) for y in (2022, 2027, 2032)],
                         [date(2022, 12, 29), date(2027, 12, 30), date(2032, 12, 30)])
        self.assertEqual(tax.harvest_lots([{"symbol": "BIG", "quantity": Decimal(2), "gain": Decimal(6_000_000)},
                                           {"symbol": "SMALL", "quantity": Decimal(10), "gain": Decimal(1_000_000)}],
                                          Decimal(2_500_000)),
                         [{"symbol": "SMALL", "shares": 10, "gain": Decimal(1_000_000)}])   # One BIG share alone is over.
        self.assertEqual(tax.harvest_lots([{"symbol": "VOO", "quantity": Decimal(40), "gain": Decimal(5_600_000)},
                                           {"symbol": "SCHD", "quantity": Decimal(3), "gain": Decimal(30_000)}],
                                          Decimal(2_500_000)),
                         [{"symbol": "VOO", "shares": 17, "gain": Decimal(2_380_000)}])   # 30 thousand won: no extra trade.
        # One lot per stock across accounts: never "VOO 4주·VOO 1주" (review 2026-11-02).
        two = [SimpleNamespace(fields={"symbol": "VOO", "quantity": "30"}), SimpleNamespace(fields={"symbol": "VOO", "quantity": "10"})]
        self.assertEqual(tax._lots(two, [Decimal(4_200_000), Decimal(1_400_000)]),
                         [{"symbol": "VOO", "quantity": Decimal(40), "gain": Decimal(5_600_000)}])
        self.assertEqual(tax._lots(two, [Decimal(4_200_000), None]), [])   # Part unknown: the per-share gain is unknown.
        stack = self.data()["pension_stack"]
        self.assertEqual((stack["age"], [t["retire_age"] for t in stack["tiers"]]), (37, [50, 55, 60]))

    def test_this_years_toss_sales_take_the_deduction_and_reach_the_model(self):
        # Client reply 2026-11-03: Smith asked for the cost of a stock it had just recorded as sold. A Toss position
        # closed (all 10 sold) and one cut from 10 to 4 are sales at the last quote; the manual VOO lot is not.
        # Synthetic symbols and amounts.
        t1, t2 = datetime(2026, 10, 20, tzinfo=timezone.utc), datetime(2026, 11, 2, tzinfo=timezone.utc)
        toss = lambda *args: _toss(self.conn, *args)
        toss("s1", t1, [("ABC", "에이비씨", "10", "280", "100"), ("XYZ", "엑스와이지", "10", "400", "300")], "1300")
        toss("s2", t2, [("XYZ", "엑스와이지", "4", "410", "300")], "1350")
        doc = {"schema_version": 1, "import_id": "t5", "source": "manual", "mode": "patch", "as_of": t2.isoformat(),
               "owners": [{"id": "self"}], "records": [dict(RECORDS[3], effective_at=t2.isoformat(), revision=2, quantity="30",
                                                             value="15000")]}
        ledger.apply_import(self.conn, parse_import(json.dumps(doc)), recorded_at=t2)
        data = build_report(self.conn, as_of=t2, known_at=t2, baseline=None, kind="monday", household=HOUSEHOLD)
        view = data["view"]
        self.assertEqual([(s.symbol, s.quantity, s.price, s.gain_won) for s in view.sales],
                         [("ABC", Decimal(10), Decimal(280), Decimal(2_430_000)),    # 180 x 10 x 1,350.
                          ("XYZ", Decimal(6), Decimal(400), Decimal(810_000))])      # 100 x 6 x 1,350.
        self.assertEqual(view.sales_since, t1)
        t = tax.facts(view, t2.date(), HOUSEHOLD)
        plan = tax.harvest_plan(t)
        self.assertEqual((plan["realized"], plan["room"]), (Decimal(3_240_000), Decimal(-740_000)))
        self.assertEqual(plan["realize"], 0)
        card = next(p for p in data["advice"]["proposals"] if p.key == "overseas-harvest")
        self.assertEqual(card.headline, "올해 해외주식 공제 소진, 추가 매도는 내년으로")
        self.assertIn("약 324만 원으로 해외주식 양도세 기본공제 250만 원을 다 썼습니다", card.title)
        self.assertIn("에이비씨 10주·엑스와이지 6주", card.why)
        realized = tax.realized_payload(view)
        self.assertEqual((realized["self_us_realized_gain_won"], realized["self_us_deduction_left_won"]), ("3240000", "0"))
        self.assertEqual(realized["sales"][0]["average_cost"], "100")
        self.assertIn(("해외주식 양도소득", "올해 실현 이익 약 324만 원(토스 매도 2건, 추정) / 남은 공제 0원"),
                      [row[:2] for row in tax.checklist(t)])
        # Every day's model input carries the sales, and Smith's tax figures are then a source to cite.
        rates = build_report(self.conn, as_of=t2, known_at=t2, baseline=None, kind="rates", household=HOUSEHOLD)
        payload = narrative.build_payload(rates["view"], rates, None)
        self.assertEqual(payload["tax"]["realized_this_year"]["self_us_deduction_left_won"], "0")
        self.assertIn("tax", narrative._known_refs(payload))
        # The next year starts with nothing realized.
        later = datetime(2027, 1, 10, tzinfo=timezone.utc)
        self.assertEqual(build_report(self.conn, as_of=later, known_at=t2, baseline=None, kind="monday",
                                      household=HOUSEHOLD)["view"].sales, [])

    def test_sales_review_cases(self):
        # Review 2026-11-03: a move between Toss accounts is no sale; a sale without an FX rate leaves the deduction
        # unknown, not whole; much deduction left beside a small gain is no "used up" card. Synthetic values.
        t1, t2 = datetime(2026, 10, 20, tzinfo=timezone.utc), datetime(2026, 11, 2, tzinfo=timezone.utc)
        cases = {
            # Which account sold at which cost cannot be told when the stock grew elsewhere: unknown, not dropped.
            "moved": ([("ABC", "에이비씨", "10", "280", "100", "a")], [("ABC", "에이비씨", "10", "281", "100", "b")], "1350",
                      [("ABC", None, "moved")]),
            # The shares arrive in the other account a day later (review 2026-11-03), checked at the later snapshot.
            "moved late": ([("ABC", "에이비씨", "10", "280", "100", "a")], [], "1350", [("ABC", None, "moved")]),
            # Another account exists and nothing arrived yet: the fall may still be a move for a week.
            "pending": ([("ABC", "에이비씨", "10", "280", "100", "a"), ("XYZ", "엑스와이지", "1", "110", "100", "b")],
                        [("XYZ", "엑스와이지", "1", "110", "100", "b")], "1350", [("ABC", None, "pending")]),
            # A 1-for-10 reverse split: fewer shares at ten times the cost, no trade.
            "reverse split": ([("ABC", "에이비씨", "1000", "5", "1")], [("ABC", "에이비씨", "100", "50", "10")], "1350",
                              [("ABC", None, "cost-changed")]),
            # Account a's last quote and b's arrival in one snapshot, a closed the next (review 2026-11-03).
            "moved same": ([("ABC", "에이비씨", "10", "281", "100", "a"), ("ABC", "에이비씨", "10", "281", "100", "b")],
                           [("ABC", "에이비씨", "10", "282", "100", "b")], "1350", [("ABC", None, "moved")]),
            "no fx": ([("ABC", "에이비씨", "10", "280", "100"), ("XYZ", "엑스와이지", "1", "110", "100")],
                      [("XYZ", "엑스와이지", "1", "110", "100")], None, [("ABC", None, None)]),
            "room left": ([("ABC", "에이비씨", "10", "105", "100"), ("XYZ", "엑스와이지", "1", "110", "100")],
                          [("XYZ", "엑스와이지", "1", "110", "100")], "1350", [("ABC", Decimal(67_500), None)]),
        }
        for name, (first, second, fx, expected) in cases.items():
            with self.subTest(name):
                conn = ledger.connect(":memory:")
                self.addCleanup(conn.close)
                doc = {"schema_version": 1, "import_id": "t1", "source": "manual", "mode": "patch",
                       "as_of": AT.isoformat(), "owners": [{"id": "self"}], "records": RECORDS[:3]}
                ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
                _toss(conn, "s1", t1, first, fx and "1300")   # "no fx": no rate at all.
                _toss(conn, "s2", t2, second, fx)
                at = t2 + timedelta(days=1)
                _toss(conn, "s3", at, second + [("ABC", "에이비씨", "10", "281", "100", "b")] * (name == "moved late"),
                      "1350")                                   # The rate arrives only after the sale was seen.
                data = build_report(conn, as_of=at, known_at=at, baseline=None, kind="monday", household=HOUSEHOLD)
                self.assertEqual([(s.symbol, s.gain_won, s.unclear) for s in data["view"].sales], expected)
                t = tax.facts(data["view"], at.date(), HOUSEHOLD)
                cards = [p for p in data["advice"]["proposals"] if p.key == "overseas-harvest"]
                pending = [u for u in data["advice"]["undetermined"] if u.key == "overseas-harvest"]
                if name in ("no fx", "moved"):   # Unknown gains: no harvest card, the judgement is pending.
                    self.assertIsNone(tax.harvest_plan(t)["room"])
                    self.assertIsNone(tax.realized_payload(data["view"])["self_us_deduction_left_won"])
                    self.assertEqual((cards, len(pending)), ([], 1))
                    self.assertIsNone(t["us_realized"])                       # Unknown, never shown as 0원.
                    friday = build_report(conn, as_of=at, known_at=at, baseline=None, kind="tax", household=HOUSEHOLD)
                    self.assertIn("올해 실현 미상 · 남은 공제 미상", render(friday)[1])
                    self.assertIn("올해 실현 이익 미상(토스 매도 1건 중 1건 계산 불가) / 남은 공제 미상",
                                  [row[1] for row in tax.checklist(t)])
                elif name == "room left":
                    self.assertEqual(tax.harvest_plan(t)["room"], Decimal(2_432_500))
                    self.assertNotIn("소진", cards[0].headline)

    def test_a_pension_without_an_fx_rate_makes_the_retirement_track_a_lower_bound(self):
        eur = rec("eur-pension", "asset", category="fund", account_type="pension_savings", currency="EUR",
                  value="50000", valuation_method="statement", liquidity="restricted")
        doc = {"schema_version": 1, "import_id": "t3", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": [eur]}
        ledger.apply_import(self.conn, parse_import(json.dumps(doc)), recorded_at=AT)
        data = self.data()
        t = tax.facts(data["view"], date(2026, 10, 5), HOUSEHOLD)
        self.assertEqual((t["pension_assets"], t["pension_unconverted"]), (Decimal(70_000_000), 1))  # Not as zero.
        self.assertIn("pension_savings", data["pension_stack"]["unknown"])
        track = next(tr for tr in data["advice"]["strategy"] if tr.name == "노후 준비")
        self.assertIn("연금저축", track.detail)
        self.assertIn("수령액을 몰라, 준비율은 그 이상이고 연금 밖에서 준비할 돈은 최대치", track.detail)
        # Review 2026-10-23: an unknown pension is not zero, so the need is a ceiling and cannot set the status.
        tier = data["pension_stack"]["tiers"][-1]
        self.assertTrue(tier["outside_need"]["upper_bound"])
        self.assertIn("pension_savings", tier["outside_need"]["unknown"])
        self.assertEqual(track.status, "unknown")
        self.assertIn("최대 ", track.detail)

    def test_capital_gains_estimates(self):
        D = Decimal
        # One home at 1.65bn bought for 1.0bn, held and lived 5 years: taxed share (0.45/1.65), 40% deduction.
        own = tax.capital_gains(D(1_650_000_000), D(1_000_000_000), held=5, lived=5, one_home=True)
        self.assertEqual(own["deduction_rate"], D("0.4"))
        self.assertEqual(own["total"].quantize(D(1)), D(23_003_500))
        # Not exempt: the whole gain, 2% a year for 6 years, top brackets.
        other = tax.capital_gains(D(1_238_000_000), D(500_000_000), held=6, lived=0, one_home=False)
        self.assertEqual(other["deduction_rate"], D("0.12"))
        self.assertEqual(other["total"].quantize(D(1)), D(259_352_280))
        self.assertEqual(tax.capital_gains(D(1_000_000_000), D(500_000_000), held=6, lived=2, one_home=True)["total"], 0)

    def test_report_carries_tax_proposals_notes_and_checklist(self):
        data = self.data()
        keys = [p.key for p in data["advice"]["proposals"]]
        for key in ("pension-credit", "overseas-harvest", "isa-open"):
            self.assertIn(key, keys)
        titles = [n.title for n in data["advice"]["tax_notes"]]
        self.assertTrue(any("혼인신고" in t for t in titles))              # Two owners, marriage not registered.
        self.assertTrue(any("고가주택" in t for t in titles))              # Own home above 1.2 billion won.
        tracks = {t.name: t for t in data["advice"]["strategy"]}
        self.assertIn("60세", tracks["노후 준비"].headline)
        self.assertEqual(data["household_profile"]["age"], 37)
        _, html = render(data)
        for text in ("세금 관점 전략", "올해 연말정산 점검표", "IRP"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
