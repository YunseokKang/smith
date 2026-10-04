import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from smith import ledger
from smith.importer import parse_import
from smith.records import ImportBatch, Observation
from smith.relevance import exposures
from smith.summary import build_summary, render, to_dict

SEP1 = "2026-09-01T00:00:00+00:00"
OCT1 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def record(record_id: str, kind: str, **fields: object) -> dict:
    return {"id": record_id, "kind": kind, "owner_id": "self", "effective_at": SEP1, "revision": 1,
            "status": "active", **fields}


def asset(record_id: str, currency: str, value: str, liquidity: str) -> dict:
    return record(record_id, "asset", category="cash" if currency == "KRW" else "stock",
                  account_type="bank" if currency == "KRW" else "brokerage", currency=currency, value=value,
                  valuation_method="manual", liquidity=liquidity)


def flow(record_id: str, direction: str, category: str, amount: str, frequency: str = "monthly",
         **extra: object) -> dict:
    return record(record_id, "cashflow", direction=direction, category=category, currency="KRW", amount=amount,
                  frequency=frequency, start_date="2026-09-01", **extra)


RECORDS = [
    asset("cash", "KRW", "1000000", "immediate"),
    {**asset("us-stock", "USD", "1000", "days"), "owner_id": "partner"},
    record("loan", "liability", category="credit_loan", currency="KRW", outstanding_principal="500000",
           annual_rate="0.06", rate_type="fixed", repayment_method="equal_payment"),
    flow("salary", "inflow", "salary", "3000000"),
    flow("living", "outflow", "living_expense", "1200000"),
    flow("repay", "outflow", "loan_payment", "100000", liability_record_id="loan"),
    # Extra principal repayment on the same loan must not add interest a second time.
    flow("prepay", "outflow", "loan_payment", "50000", liability_record_id="loan"),
    flow("saving", "outflow", "internal_transfer", "500000"),
    flow("premium", "outflow", "insurance_premium", "1200000", frequency="annual"),
    record("home", "goal", category="home", currency="KRW", target_amount="100000000",
           target_date="2029-09-01", priority="high"),
]


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.connect(":memory:")
        self.addCleanup(self.conn.close)
        doc = {"schema_version": 1, "import_id": "manual-1", "source": "manual", "mode": "patch",
               "as_of": SEP1, "owners": [{"id": "self"}, {"id": "partner"}], "records": RECORDS}
        ledger.apply_import(self.conn, parse_import(json.dumps(doc)), recorded_at=OCT1)

    def add_fx(self, rate: str) -> None:
        observed = datetime(2026, 9, 30, tzinfo=timezone.utc)
        batch = ImportBatch("fx-1", "toss", "snapshot", observed, ("self",), (), "x",
                            (Observation("USD", "fx_mid_rate", "KRW", rate, observed),))
        ledger.apply_import(self.conn, batch, recorded_at=OCT1)

    def test_totals_allocation_cash_flow_and_goals(self):
        self.add_fx("1400")
        summary = build_summary(self.conn, as_of=OCT1, known_at=OCT1)
        self.assertTrue(summary.complete)
        self.assertEqual((summary.total_assets, summary.total_liabilities, summary.net_worth),
                         (Decimal(2400000), Decimal(500000), Decimal(1900000)))
        self.assertEqual(summary.by_liquidity, {"immediate": Decimal(1000000), "days": Decimal(1400000)})
        self.assertEqual(summary.by_currency, {"KRW": Decimal(1000000), "USD": Decimal(1400000)})
        self.assertEqual(summary.net_worth_by_owner, {"self": Decimal(500000), "partner": Decimal(1400000)})
        self.assertEqual((summary.monthly_inflow, summary.monthly_outflow, summary.monthly_transfers),
                         (Decimal(3000000), Decimal(1450000), Decimal(500000)))
        self.assertEqual(summary.monthly_loan_interest_estimate, Decimal(2500))
        self.assertEqual(summary.goals[0]["months_left"], 35)
        self.assertEqual(to_dict(summary)["net_worth"], "1900000")
        self.assertIn("Net worth", render(summary)[1])
        links = {e.key: e.amount for e in exposures(self.conn, as_of=OCT1, known_at=OCT1).exposures}
        self.assertEqual((links["usd_assets"], links["krw_cash_and_deposits"]), (Decimal(1400000), Decimal(1000000)))
        self.assertNotIn("variable_rate_debt", links)  # The test loan is fixed-rate.

    def test_missing_fx_and_stale_data_are_reported_not_zeroed(self):
        later = datetime(2027, 1, 15, tzinfo=timezone.utc)
        summary = build_summary(self.conn, as_of=later, known_at=later)
        self.assertFalse(summary.complete)
        self.assertEqual(summary.unconverted, {"USD": Decimal(1000)})
        self.assertEqual(summary.total_assets, Decimal(1000000))
        self.assertTrue(any("older than 90 days" in warning for warning in summary.warnings))
        self.assertTrue(any(line.startswith("INCOMPLETE") for line in render(summary)))

    def test_bad_fx_foreign_cash_flow_and_failed_sync_mark_the_summary_incomplete(self):
        self.add_fx("0")  # A zero rate must not value USD holdings at nothing.
        usd_pay = {**flow("usd-pay", "inflow", "salary", "100"), "currency": "USD"}
        doc = {"schema_version": 1, "import_id": "manual-2", "source": "manual", "mode": "patch",
               "as_of": SEP1, "owners": [{"id": "self"}], "records": [usd_pay]}
        ledger.apply_import(self.conn, parse_import(json.dumps(doc)), recorded_at=OCT1)
        ledger.record_sync_run(self.conn, source="toss", attempted_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
                               outcome="success")
        ledger.record_sync_run(self.conn, source="toss", attempted_at=OCT1, outcome="failure",
                               error_code="network-error")
        summary = build_summary(self.conn, as_of=OCT1, known_at=OCT1)
        self.assertEqual(set(summary.incomplete_areas), {"balance_sheet", "cash_flow"})
        self.assertEqual(summary.unconverted, {"USD": Decimal(1000)})
        self.assertEqual(summary.monthly_inflow, Decimal(3000000))
        self.assertTrue(any("not positive" in w for w in summary.warnings))
        self.assertTrue(any("last sync failed" in w and "network-error" in w for w in summary.warnings))
        # Exposures from an incomplete ledger are partial: shares would use a wrong denominator.
        result = exposures(self.conn, as_of=OCT1, known_at=OCT1)
        self.assertFalse(result.complete)
        self.assertEqual(result.unconverted, {"USD": Decimal(1000)})
        self.assertTrue(all(e.share_of_assets is None for e in result.exposures))
        self.assertTrue(any("partial" in w for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
