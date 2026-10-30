import json
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from smith import dividends, ledger, tax
from smith.importer import parse_import
from smith.payload import load_view
from smith.records import ImportBatch, Observation

import fast_ledger
from test_dart import FakeDart, form

AT = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 30)


def lot(record_id, symbol, market, quantity, account, currency="KRW", **extra):
    return {"id": record_id, "kind": "asset", "owner_id": extra.pop("owner", "self"), "effective_at": AT.isoformat(),
            "revision": 1, "status": "active", "category": "stock", "account_type": account, "currency": currency,
            "value": str(quantity * 100), "valuation_method": "market", "liquidity": "days", "symbol": symbol,
            "market": market, "quantity": str(quantity), "unit_price": "100", "instrument_name": symbol, **extra}


def kr(record, per_share):
    return ("dividend", "KR:123450", record, json.dumps({"per_share": per_share, "currency": "KRW"}), None)


class DividendTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "smith.db"
        records = [lot("a", "123450", "KR", 100, "brokerage"), lot("b", "123450", "KR", 50, "isa"),
                   lot("c", "FUND", "US", 10, "brokerage", currency="USD"), lot("d", "NOPAY", "US", 5, "brokerage", currency="USD"),
                   lot("h", "123450", "KR", 999, "brokerage", managed_by="hermes")]
        doc = {"schema_version": 1, "import_id": "m1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": records}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
            ledger.apply_import(conn, ImportBatch("fx", "toss", "snapshot", AT, ("self",), (), "x",
                                                  (Observation("USD", "fx_mid_rate", "KRW", "1400", AT),)), recorded_at=AT)
            self.view = load_view(conn, as_of=AT, known_at=AT)

    def store(self, points):
        with closing(ledger.connect(self.db)) as conn:
            ledger.store_evidence(conn, points, retrieved_at=AT)
            return dividends.load(conn, AT)

    def test_the_yearly_run_rate_per_general_account_lot(self):
        found = self.store([kr(date(2025, 9, 30), "300"),        # A year before the latest decided record date: out.
                            kr(date(2025, 12, 31), "500"), kr(date(2026, 3, 31), "300"), kr(date(2026, 6, 30), "300"),
                            kr(date(2026, 12, 15), "400"),        # Decided, record date ahead: in.
                            ("dividend", "US:FUND", date(2026, 10, 25),
                             json.dumps({"ttm_per_share": "2.5", "currency": "USD"}), None)])
        mine = dividends.yearly(self.view, found, TODAY)["self"]
        # KR: (500 + 300 + 300 + 400) x 100 general shares = 150,000; the ISA lot and Hermes lot are out.
        # US: 2.5 dollars x 10 shares x 1,400 won = 35,000. NOPAY has no data: unknown, not zero.
        self.assertEqual(mine["parts"], [("123450", Decimal(150_000), "filing"), ("FUND", Decimal(35_000), "research")])
        self.assertEqual((mine["amount"], mine["unknown"], mine["excluded"]), (Decimal(185_000), ["NOPAY"], ["123450"]))
        stale = dividends.yearly(self.view, {**found, "US:FUND": {date(2026, 8, 1): {"ttm_per_share": "2.5"}}}, TODAY)
        self.assertIn("FUND", stale["self"]["unknown"])                     # A month-old US figure is not used.

    def test_kr_points_take_the_correction_and_us_points_need_their_page(self):
        listing = {"status": "000", "list": [
            {"rcept_no": "20261103800010", "rcept_dt": "20261103", "report_nm": "[기재정정]현금ㆍ현물배당결정", "corp_name": "가"},
            {"rcept_no": "20261102800020", "rcept_dt": "20261102", "report_nm": "현금ㆍ현물배당결정", "corp_name": "가"}]}
        fake = FakeDart(listing=listing, documents={"20261103800010": form(common="361"), "20261102800020": form(common="300")})
        points, failures = dividends.kr_points(["123450", "123455"], key="k" * 40, today=TODAY,
                                               now=AT, cache=Path(self.db.parent, "dart", "c.json"), fetch=fake)
        self.assertEqual(failures, [])
        self.assertEqual({(s, json.loads(v)["per_share"]) for _, s, _, v, _ in points}, {("KR:123450", "361"), ("KR:123455", "361")})
        output = {"tickers": [{"ticker": "FUND", "ttm_per_share": "2.50", "payments": "4", "last_pay_date": "2026-09-30",
                               "claim": "Distributions 0.62 0.63 0.62 0.63", "source_url": "https://fund.example.com/d"},
                              {"ticker": "LIED", "ttm_per_share": "9", "payments": "4", "last_pay_date": "",
                               "claim": "9.00", "source_url": "https://bad.example.com"},
                              {"ticker": "EVIL", "ttm_per_share": "1", "payments": "1", "last_pay_date": "", "claim": "1",
                               "source_url": "https://x.example.com"}]}

        output["tickers"].append({"ticker": "BIG", "ttm_per_share": "999.00", "payments": "4", "last_pay_date": "",
                                  "claim": "Quarterly dividend 0.62", "source_url": "https://big.example.com"})
        pages = {"FUND": ["0.62", "0.63"], "LIED": [], "BIG": ["0.62"]}      # The numbers code found on each page.

        def verifier(items, tier=None):
            return [dict(i, check={"status": "mismatch" if i["topic"] == "LIED" else "matched", "found": pages[i["topic"]]})
                    for i in items], {}
        points = dividends.us_points(["FUND", "LIED", "BIG"], today=TODAY, secrets=[], caller=lambda prompt: (output, None),
                                     verifier=verifier)
        # FUND: its four payments are on the page and add up to 2.50. BIG: the page holds 0.62, never 999.
        self.assertEqual([(s, json.loads(v)["ttm_per_share"]) for _, s, _, v, _ in points], [("US:FUND", "2.50")])

    def test_a_correction_moving_the_record_date_and_a_company_with_no_dividend(self):
        listing = {"status": "000", "total_page": 1, "list": [
            {"rcept_no": "20261003800010", "rcept_dt": "20261003", "report_nm": "[기재정정]현금ㆍ현물배당결정", "corp_name": "가"},
            {"rcept_no": "20261002800020", "rcept_dt": "20261002", "report_nm": "현금ㆍ현물배당결정", "corp_name": "가"}]}
        fake = FakeDart(listing=listing, documents={"20261003800010": form(common="361", record="2026-10-01"),
                                                    "20261002800020": form(common="361", record="2026-09-30")})
        points, _ = dividends.kr_points(["123450"], key="k" * 40, today=TODAY, now=AT,
                                        cache=Path(self.db.parent, "dart", "c.json"), fetch=fake)
        by_day = {day: json.loads(v) for _, s, day, v, _ in points}
        self.assertEqual(by_day[date(2026, 9, 30)], {"withdrawn": True})     # The original's date is withdrawn...
        self.assertEqual(by_day[date(2026, 10, 1)]["per_share"], "361")      # ...and the dividend is counted once.
        self.assertEqual(dividends._per_share({"KR:123450": by_day}, "KR", "123450", TODAY)[0], Decimal(361))
        none, _ = dividends.kr_points(["123450"], key="k" * 40, today=TODAY, now=AT,
                                      cache=Path(self.db.parent, "dart", "c.json"),
                                      fetch=FakeDart(listing={"status": "013"}))
        self.assertEqual(json.loads(none[0][3])["per_share"], "0")           # Read, no dividend: zero, not unknown.
        old = {"KR:123450": {date(2024, 12, 31): {"per_share": "500"}}}
        self.assertIsNone(dividends._per_share(old, "KR", "123450", TODAY)[0])   # A year long gone is not current.

    def test_names_quantities_and_the_sync_cadence(self):
        doc = {"schema_version": 1, "import_id": "m2", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": [
                   {**lot("x", "234560", "KR", 1, "brokerage"), "instrument_name": "김가족 비상금 계좌", "quantity": None}]}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
            view = load_view(conn, as_of=AT, known_at=AT)
        mine = dividends.yearly(view, {}, TODAY)["self"]
        self.assertIn("234560", mine["unknown"])                     # No quantity: unknown, and named by its code...
        self.assertNotIn("김가족 비상금 계좌", json.dumps(mine, ensure_ascii=False, default=str))   # ...never by free text.
        self.assertTrue(dividends.stale(self.db, AT))
        result = dividends.sync(self.db, view, now=AT, today=TODAY, dart_key=None, secrets=[],
                                caller=lambda prompt: (_ for _ in ()).throw(RuntimeError("down")))
        self.assertEqual(len(result["failures"]), 1)
        self.assertFalse(dividends.stale(self.db, AT + timedelta(hours=12)))   # Failed: retried after a day...
        self.assertTrue(dividends.stale(self.db, AT + timedelta(days=1, hours=1)))
        dividends.sync(self.db, view, now=AT + timedelta(days=2), today=TODAY, dart_key=None, secrets=[],
                       caller=lambda prompt: ({"tickers": []}, None))
        self.assertFalse(dividends.stale(self.db, AT + timedelta(days=5)))      # ...clean: a week.

    def test_the_tax_note_counts_dividends_beside_interest(self):
        guard = tax.financial_income_guard(Decimal(100_000_000), Decimal("0.03"), None,
                                           {"amount": Decimal(5_000_000), "parts": [("가상전자", Decimal(5_000_000), "filing")],
                                            "unknown": ["NOPAY"], "excluded": []})
        self.assertEqual((guard["income_now"], guard["share_now"]), (Decimal(8_000_000), Decimal("0.4")))
        text = tax.notes({"homes": [], "home_records": [], "married": None, "year": 2026, "premiums": []},
                         [("self", guard)])[0].body
        for piece in ("이자는 연 약 300만 원", "배당은 연 약 500만 원(가상전자 500만 원)", "연 약 800만 원(기준의 40%)",
                      "배당을 확인하지 못한 1종목(NOPAY)은 빠진 하한", "ISA·연금계좌 배당은 따로 과세"):
            self.assertIn(piece, text)
        self.assertNotIn("배당은 원장에 없어", text)
        # With pay parked in deposits too: the dividends use part of the threshold, so the deposit room shrinks.
        paid = tax.financial_income_guard(Decimal(100_000_000), Decimal("0.03"), Decimal(300_000_000),
                                          {"amount": Decimal(5_000_000), "parts": [], "unknown": [], "excluded": []})
        self.assertEqual(paid["threshold_balance"], Decimal(500_000_000))      # (2,000만 - 500만) / 3%.
        text = tax.notes({"homes": [], "home_records": [], "married": None, "year": 2026, "premiums": []}, [("self", paid)])[0].body
        self.assertIn("이자 연 약 1,200만 원에 배당까지 더해 연 약 1,700만 원(기준의 85%)", text)


def setUpModule():  # noqa: N802 - unittest hook
    fast_ledger.install()


def tearDownModule():  # noqa: N802 - unittest hook
    fast_ledger.uninstall()


if __name__ == "__main__":
    unittest.main()
