import json
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal

from smith import ledger, tax
from smith.importer import parse_import
from smith.records import ImportBatch, ImportRejected, Observation
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
        stack = self.data()["pension_stack"]
        self.assertEqual((stack["age"], [t["retire_age"] for t in stack["tiers"]]), (37, [50, 55, 60]))

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
