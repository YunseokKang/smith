import json
import unittest
from datetime import datetime, timezone

from smith import checklist, ledger
from smith.importer import parse_import
from smith.payload import load_view

AT = datetime(2026, 10, 2, tzinfo=timezone.utc)


def rec(record_id, kind, owner="self", **fields):
    return {"id": record_id, "kind": kind, "owner_id": owner, "effective_at": AT.isoformat(), "revision": 1,
            "status": "active", **fields}


def asset(record_id, category, owner="self", **extra):
    return rec(record_id, "asset", owner, category=category, account_type=extra.pop("account_type", "bank"),
               currency="KRW", value="1000000", valuation_method="manual", liquidity=extra.pop("liquidity", "immediate"),
               **extra)


def flow(record_id, category, direction="outflow", frequency="monthly", **extra):
    return rec(record_id, "cashflow", category=category, direction=direction, currency="KRW", amount="100000",
               frequency=frequency, start_date="2026-01-01", **extra)


RECORDS = [
    asset("cash", "cash"),
    asset("fund", "unclassified", account_type="brokerage", liquidity="days"),
    asset("kr", "stock", account_type="brokerage", liquidity="days", symbol="005930", market="KR"),
    asset("home", "real_estate", account_type="none", liquidity="months", occupancy="owner_occupied"),
    asset("partner-home", "real_estate", "partner", account_type="none", liquidity="months", occupancy="leased_out"),
    rec("deposit", "liability", "partner", category="lease_deposit_obligation", currency="KRW",
        outstanding_principal="300000000", annual_rate="0", rate_type="fixed", repayment_method="bullet",
        maturity="2028-08-01"),
    flow("pay", "salary", direction="inflow"),
    flow("premium", "insurance_premium"),
]


class ChecklistTests(unittest.TestCase):
    def requests(self, records, household=None):
        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)
        doc = {"schema_version": 1, "import_id": "c1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}, {"id": "partner"}], "records": records}
        ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
        return checklist.requests(load_view(conn, as_of=AT, known_at=AT), household)

    def test_requests_name_what_is_missing_and_put_essentials_first(self):
        found = self.requests(RECORDS)
        self.assertEqual([r.key for r in found if r.priority == "must"], ["partner-finances", "irregular-flows"])
        self.assertEqual({r.key for r in found if r.priority != "must"},
                         {"unclassified", "cost-basis", "surrender-values", "acquisition", "retirement", "marriage",
                          "commitment"})
        self.assertEqual(found[0].priority, "must")

    def test_provided_information_closes_its_request(self):
        household = {"birth_year": 1990, "retirement_monthly_spend": 4000000, "marriage_registered": False,
                     "properties": {ref: {"acquired_year": 2020, "acquired_price": 800000000}
                                    for ref in ("home", "partner-home")}}
        records = RECORDS + [asset("partner-cash", "cash", "partner"), rec("partner-pay", "cashflow", "partner",
                             category="salary", direction="inflow", currency="KRW", amount="100000",
                             frequency="monthly", start_date="2026-01-01"),
                             flow("tax", "tax", frequency="annual", commitment="fixed")]
        keys = {r.key for r in self.requests(records, household)}
        self.assertTrue(keys.isdisjoint({"partner-finances", "irregular-flows", "acquisition", "retirement",
                                         "marriage"}))


if __name__ == "__main__":
    unittest.main()
