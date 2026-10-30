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
                               "claim": "Distributions 0.61 0.62 0.63 0.64", "source_url": "https://fund.example.com/d"},
                              {"ticker": "LIED", "ttm_per_share": "9", "payments": "4", "last_pay_date": "",
                               "claim": "9.00", "source_url": "https://bad.example.com"},
                              {"ticker": "EVIL", "ttm_per_share": "1", "payments": "1", "last_pay_date": "", "claim": "1",
                               "source_url": "https://x.example.com"}]}

        output["tickers"].append({"ticker": "BIG", "ttm_per_share": "999.00", "payments": "4", "last_pay_date": "",
                                  "claim": "Quarterly dividend 0.62", "source_url": "https://big.example.com"})
        output["tickers"] += [{"ticker": "SAME", "ttm_per_share": "1.00", "payments": "4", "last_pay_date": "",
                               "claim": "0.25 0.25 0.25 0.25", "source_url": "https://same.example.com"},
                              {"ticker": "NONE", "ttm_per_share": "0", "payments": "0", "last_pay_date": "",
                               "claim": "No distributions", "source_url": "https://none.example.com/a.pdf"}]
        pages = {"FUND": ["0.61", "0.62", "0.63", "0.64"], "LIED": [], "BIG": ["0.62"],   # Numbers code found on each page.
                 "SAME": ["0.25"], "NONE": []}

        def verifier(items, tier=None):
            return [dict(i, check={"status": "mismatch" if i["topic"] == "LIED" else "matched", "found": pages[i["topic"]]})
                    for i in items], {}
        points = dividends.us_points(["FUND", "LIED", "BIG", "SAME", "NONE"], today=TODAY, secrets=[],
                                     caller=lambda prompt: (output, None),
                                     verifier=verifier)
        # FUND: its four payments are on the page and add up to 2.50. BIG: the page holds 0.62, never 999. SAME: one
        # 0.25 on the page says nothing of four payments. NONE: a zero on a page code could not read is no proof.
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

    def test_sec_companies_sum_their_declared_quarters_and_funds_go_to_research(self):
        def q(start, end, val, filed="2026-08-26"):
            return {"start": start, "end": end, "val": val, "filed": filed}
        rows = [q("2025-04-01", "2025-06-30", 0.15),                       # Fifth newest: out.
                q("2025-07-01", "2025-09-30", 0.20),
                q("2025-10-01", "2025-12-31", 0.25), q("2026-01-01", "2026-03-31", 0.25),
                q("2026-04-01", "2026-06-30", 0.24, filed="2026-07-30"), q("2026-04-01", "2026-06-30", 0.25),   # Amended.
                q("2026-01-01", "2026-06-30", 0.50)]                        # Six months: not a quarter.
        # The latest four back-to-back quarters (a quarter is filed weeks after it ends): 0.20 + 0.25 + 0.25 + 0.25.
        self.assertEqual(dividends._declared_total(rows, TODAY), Decimal("0.95"))
        three = [r for r in rows if r["start"] >= "2025-10-01"]
        self.assertIsNone(dividends._declared_total(three, TODAY))             # Three quarters are not a year...
        self.assertEqual(dividends._declared_total(three + [q("2025-01-01", "2025-12-31", 1.0)], TODAY),
                         Decimal("1.0"))                                        # ...the latest full year is.
        tickers = {"0": {"cik_str": 1, "ticker": "CO", "title": "CO"}}
        facts = {"facts": {"us-gaap": {"CommonStockDividendsPerShareDeclared": {"units": {"USD/shares": rows}}}}}

        def fetch(url, contact):
            return json.dumps(tickers if url.endswith("company_tickers.json") else facts).encode()
        points, funds, failures = dividends.sec_points(["CO", "FUND"], contact="me@example.com", today=TODAY, now=AT,
                                                       cache=Path(self.db.parent, "sec", "t.json"), fetch=fetch)
        self.assertEqual(([(s, json.loads(v)["ttm_per_share"]) for _, s, _, v, _ in points], funds, failures),
                         ([("US:CO", "0.95")], ["FUND"], []))

    def test_a_late_correction_replaces_its_own_quarter_and_an_unreadable_class_is_unknown(self):
        listing = {"status": "000", "total_page": 1, "list": [
            {"rcept_no": "20260801800030", "rcept_dt": "20260801", "report_nm": "[기재정정]현금ㆍ현물배당결정", "corp_name": "가"},
            {"rcept_no": "20260730800020", "rcept_dt": "20260730", "report_nm": "현금ㆍ현물배당결정", "corp_name": "가"},
            {"rcept_no": "20260430800010", "rcept_dt": "20260430", "report_nm": "현금ㆍ현물배당결정", "corp_name": "가"}]}
        docs = {"20260430800010": form(common="100", record="2026-03-31"), "20260730800020": form(common="200", record="2026-06-30"),
                "20260801800030": form(common="110", record="2026-03-31")}            # Q1 corrected after Q2.
        points, failures = dividends.kr_points(["123450"], key="k" * 40, today=TODAY, now=AT,
                                               cache=Path(self.db.parent, "dart", "c.json"),
                                               fetch=FakeDart(listing=listing, documents=docs))
        by_day = {day: json.loads(v).get("per_share") for _, _, day, v, _ in points}
        self.assertEqual(by_day, {date(2026, 3, 31): "110", date(2026, 6, 30): "200"})   # Q2 stands.
        two_classes = form(classes=(("가상전자우", "우선주", "361"), ("가상전자2우B", "우선주", "411")))
        points, failures = dividends.kr_points(["123455"], key="k" * 40, today=TODAY, now=AT,
                                               cache=Path(self.db.parent, "dart", "c.json"),
                                               fetch=FakeDart(listing={"status": "000", "total_page": 1, "list": listing["list"][2:]},
                                                              documents={"20260430800010": two_classes}))
        # No zero for a class the form leaves open: its record date is unknown, said as a failure.
        self.assertEqual([(day, json.loads(v)) for _, _, day, v, _ in points], [(date(2026, 9, 30), {"unknown": True})])
        self.assertEqual(failures, ["전자공시 배당 금액 해석 불가(종류주식)"])
        known = {"KR:123455": {date(2025, 12, 31): {"per_share": "500"}, date(2026, 9, 30): {"unknown": True}}}
        self.assertIsNone(dividends._per_share(known, "KR", "123455", TODAY)[0])   # The earlier value does not stand.

    def test_holdings_whose_filings_report_no_dividend_are_said_apart(self):
        found = {"US:NOPAY": {TODAY: {"ttm_per_share": "0", "currency": "USD", "basis": "sec", "none_reported": True}}}
        mine = dividends.yearly(self.view, found, TODAY)["self"]
        self.assertEqual((mine["none_reported"], "NOPAY" in mine["unknown"]), (["NOPAY"], False))
        guard = tax.financial_income_guard(Decimal(100_000_000), Decimal("0.03"), None, mine)
        text = tax.notes({"homes": [], "home_records": [], "married": None, "year": 2026, "premiums": []}, [("self", guard)])[0].body
        self.assertIn("공시에 배당 기록이 없는 1종목(NOPAY)은 0으로 보았습니다", text)

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
