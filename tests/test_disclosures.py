import json
import tempfile
import unittest
import urllib.parse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from smith import dart, disclosures, events, ledger, sec
from smith.importer import parse_import

import fast_ledger
from test_dart import form, zipped

RECORDED = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 3, 7, 20, tzinfo=timezone.utc)      # 16:20 KST, no rate meeting near.
CONFIG = {"app": {"timezone": "Asia/Seoul"}, "events": {"enabled": True, "check_time": "16:10", "portfolio_move_pct": 50}}
KEY = "k" * 40


def lot(record_id, symbol, quantity, account, **extra):
    return {"id": record_id, "kind": "asset", "owner_id": "self", "effective_at": RECORDED.isoformat(), "revision": 1,
            "status": "active", "category": "stock", "account_type": account, "currency": "KRW",
            "value": str(quantity * 10_000), "valuation_method": "market", "liquidity": "days", "symbol": symbol,
            "market": "KR", "quantity": str(quantity), "unit_price": "10000", **extra}


def filing(number, day, title, corp="가상전자"):
    return {"rcept_no": number, "rcept_dt": day, "report_nm": title, "corp_name": corp}


class FakeDart:
    """Two synthetic issuers: 가상전자 (common 123450, preferred 123455) and 가상바이오 (234560)."""

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []
        self.lists = {"00999999": [filing("20261103800010", "20261103", "[기재정정]현금ㆍ현물배당결정"),
                                   filing("20261102800020", "20261102", "현금ㆍ현물배당결정"),
                                   filing("20261102800030", "20261102", "분기보고서 (2026.09)"),        # Routine: no event.
                                   filing("20261102000040", "20261102", "전환사채권발행결정")],
                      "00777777": [filing("20261103000050", "20261103", "주요사항보고서(유상증자결정)", "가상바이오")]}

    def __call__(self, url):
        if self.fail:
            raise dart.Error("network-error")
        parts = urllib.parse.urlsplit(url)
        path, query = parts.path.rsplit("/", 1)[-1], dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append(path)
        if path == "corpCode.xml":
            return zipped("CORPCODE.xml", "<result><list><corp_code>00999999</corp_code><stock_code>123450</stock_code>"
                                          "</list><list><corp_code>00777777</corp_code><stock_code>234560</stock_code>"
                                          "</list></result>")
        if path == "list.json":
            return json.dumps({"status": "000", "list": self.lists[query["corp_code"]]}, ensure_ascii=False).encode()
        if path == "document.xml":   # The correction states the right amount; the original a wrong one.
            return zipped("d.xml", form(common="361" if query["rcept_no"] == "20261103800010" else "300"))
        if path == "piicDecsn.json":
            row = {"rcept_no": "20261103000050", "nstk_ostk_cnt": "100,000", "bfic_tisstk_ostk": "900,000",
                   "ic_mthn": "제3자배정증자"}
        else:
            row = {"rcept_no": "20261102000040", "bd_fta": "12,200,000,000", "cv_prc": "15,410", "cvisstk_cnt": "791,693",
                   "cvisstk_tisstk_vs": "8.12", "cvrqpd_bgd": "2027년 11월 06일", "bdis_mthn": "사모"}
        return json.dumps({"status": "000", "list": [row]}, ensure_ascii=False).encode()


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "smith.db"
        doc = {"schema_version": 1, "import_id": "m1", "source": "manual", "mode": "patch", "as_of": RECORDED.isoformat(),
               "owners": [{"id": "self"}],
               "records": [lot("a", "123450", 400, "brokerage"), lot("b", "123455", 5, "isa"), lot("c", "234560", 30, "brokerage"),
                           lot("h", "345670", 10, "brokerage", managed_by="hermes")]}     # Hermes: never looked up.
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=RECORDED)
        self.sent, self.said = [], []

    def sender(self, credentials, *, recipient, subject, html, text):
        self.sent.append((subject, html))
        return f"msg-{len(self.sent)}"

    def check(self, fake, now=NOW):
        return events.check(self.db, CONFIG, now=now, reason="manual", sync=False, researcher=None, sender=self.sender,
                            recipient="me@example.com", credentials=("a", "b", "c"), say=self.said.append,
                            dart_key=KEY, dart_fetch=fake)

    def test_material_filings_of_held_companies_go_out_once_with_codes_figures(self):
        outcomes = self.check(FakeDart())
        self.assertEqual([o["status"] for o in outcomes], ["sent"])
        subject, html = self.sent[0]
        # Correction first, routine filing left out; 가상바이오's new shares and the bond follow by receipt number.
        self.assertIn("보유 종목 공시 4건", subject)
        self.assertNotIn("분기보고서", html)
        # The correction's 361 won: 400 shares in a general account (144,400 - 22,230) + 5 in an ISA (1,805) = 123,975.
        self.assertIn("약 12만 원 입금 예정", subject)
        self.assertIn("세전 146,205원", html)
        self.assertIn("11/03 정정 공시로 바뀌었습니다", html)          # The original is shown, not counted.
        self.assertIn("기존 주주 지분이 약 10.0% 줄어듭니다", html)    # 100,000 / (900,000 + 100,000).
        self.assertIn("791,693주(발행 주식의 8.12%)", html)
        self.assertIn("사채 122억 원", html)
        with closing(ledger.connect_read_only(self.db)) as conn:
            note, = [n for n in ledger.event_notes(conn) if n["kind"] == "disclosure"]
        self.assertEqual(len(note["details"]["filings"]), 4)
        self.assertNotIn("146205", json.dumps(note["details"]))         # The ledger keeps what was carried, not amounts.
        self.assertEqual(self.check(FakeDart(), NOW.replace(minute=NOW.minute + 5)), [])   # Carried: not sent again.
        self.assertEqual(len(self.sent), 1)

    def test_the_stored_note_keeps_the_pay_date_and_what_a_retry_needs(self):
        self.check(FakeDart())
        with closing(ledger.connect_read_only(self.db)) as conn:
            note, = [n for n in ledger.event_notes(conn) if n["kind"] == "disclosure"]
        stored = {f["rcept_no"]: f for f in note["details"]["filings"]}
        self.assertEqual(stored["20261103800010"]["pay_date"], "2026-11-27")      # For the daily mail's date line.
        self.assertIsNone(stored["20261102800020"]["pay_date"])                   # Superseded: its correction carries it.
        self.assertEqual((stored["20261103000050"]["corp_code"], stored["20261103000050"]["symbols"]), ("00777777", ["234560"]))

    def test_a_correction_of_another_quarter_replaces_nothing(self):
        fake = FakeDart()
        fake.document_for = {"20261103800010": form(common="300", record="2026-06-30", pay="2026-08-20")}
        original = fake.__call__

        def call(url):
            number = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("rcept_no")
            if number in fake.document_for and "document.xml" in url:
                return zipped("d.xml", fake.document_for[number])
            return original(url)
        self.check(call)
        subject, html = self.sent[0]
        self.assertNotIn("정정 공시로 바뀌었습니다", html)          # June's correction does not touch September's decision.
        self.assertIn("약 10만 원 입금 예정", subject)              # The 300-won September decision; June's was paid in August.

    def test_overlapping_checks_carry_each_filing_once(self):
        first, second = [{"rcept_no": "1"}], [{"rcept_no": "1"}, {"rcept_no": "2"}]
        with closing(ledger.connect(self.db)) as conn:
            for key, filings, at in (("disclosure:2026-11-03:1", first, NOW), ("disclosure:2026-11-03:2", second, NOW.replace(minute=21))):
                ledger.claim_event_note(conn, key=key, kind="disclosure", event_date=NOW.date(), now=at,
                                        lease=events.BUILDING, max_attempts=3, details={"filings": filings})
            # Whichever reads first, the earlier claim keeps filing 1 and the later one carries only filing 2.
            self.assertEqual(disclosures.owned(conn, "disclosure:2026-11-03:1", first), first)
            self.assertEqual(disclosures.owned(conn, "disclosure:2026-11-03:2", second), [{"rcept_no": "2"}])

    def test_us_holdings_are_looked_up_in_sec_edgar_by_item_codes(self):
        from test_sec import FakeSec
        us = {**lot("u", "GADG", 10, "brokerage"), "market": "US", "currency": "USD", "unit_price": "10", "value": "100"}
        doc = {"schema_version": 1, "import_id": "m2", "source": "manual", "mode": "patch", "as_of": RECORDED.isoformat(),
               "owners": [{"id": "self"}], "records": [us]}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=RECORDED)
        fake = FakeSec()
        outcomes = events.check(self.db, CONFIG, now=NOW, reason="manual", sync=False, researcher=None, sender=self.sender,
                                recipient="me@example.com", credentials=("a", "b", "c"), say=self.said.append,
                                sec_contact="me@example.com", sec_fetch=fake)          # No DART key: the US alone.
        self.assertEqual([o["status"] for o in outcomes], ["sent"])
        subject, html = self.sent[0]
        self.assertIn("보유 종목 공시 2건 · GADG 희석 주의", subject)   # The dilutive issue leads; the insider form is left out.
        self.assertIn("8-K · 미등록 주식 발행 · 경영진·이사 변경", html)
        self.assertIn("8-K · 실적 발표", html)
        self.assertIn("SEC EDGAR", html)
        # Only the public ticker map and the company's filing list are asked for: nothing of the household travels.
        self.assertEqual({url for url, _ in fake.calls}, {sec.TICKERS, "https://data.sec.gov/submissions/CIK0001111111.json"})
        with closing(ledger.connect_read_only(self.db)) as conn:
            note, = [n for n in ledger.event_notes(conn) if n["kind"] == "disclosure"]
        self.assertEqual({f["source"] for f in note["details"]["filings"]}, {"sec"})

    def test_one_company_with_two_tickers_and_one_failing_source(self):
        from test_sec import TICKERS, FakeSec
        tickers = {**TICKERS, "2": {"cik_str": 1111111, "ticker": "GADGB", "title": "GADGET CORP"}}   # Two classes.
        records = [{**lot(f"u{n}", t, 10, "brokerage"), "market": "US", "currency": "USD", "unit_price": "10", "value": "100"}
                   for n, t in enumerate(("GADG", "GADGB"))]
        doc = {"schema_version": 1, "import_id": "m2", "source": "manual", "mode": "patch", "as_of": RECORDED.isoformat(),
               "owners": [{"id": "self"}], "records": records}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=RECORDED)
        fake = FakeSec()

        def sec_fetch(url, contact):
            return json.dumps(tickers).encode() if url == sec.TICKERS else fake(url, contact)
        with closing(ledger.connect_read_only(self.db)) as conn:
            from smith.payload import load_view
            view = load_view(conn, as_of=NOW, known_at=NOW)
        found, _ = disclosures.scan_due(self.db, view, key=KEY, today=NOW.date(), now=NOW, fetch=FakeDart(),
                                        contact="me@example.com", sec_fetch=sec_fetch)
        us = [f for f in found if f.get("source") == "sec"]
        self.assertEqual([f["symbols"] for f in us], [["GADG", "GADGB"]] * 2)          # Each filing once, both tickers.
        self.assertEqual(sum("submissions" in url for url, _ in fake.calls), 1)        # One lookup for the company.

        def broken(url, contact):
            raise EOFError("cut")                                                       # Not an sec.Error at all.
        found, failures = disclosures.scan_due(self.db, view, key=KEY, today=NOW.date(), now=NOW, fetch=FakeDart(),
                                               contact="me@example.com", sec_fetch=broken)
        self.assertEqual(len(found), 4)                                                 # The DART filings still go out.
        self.assertTrue(any("SEC" in f for f in failures))

    def test_the_note_reads_what_each_filing_means_and_survives_a_failed_reading(self):
        def reading(filings, *, today, secrets):
            return {"overview": "가상전자 배당은 유지, 가상바이오는 증자로 지분이 묽어집니다.", "sources": [],
                    "cost_usd": "0.3", "prompt_version": "test",
                    "by_id": {"20261103000050": {"signal": "caution", "market_message": "제3자배정 증자로 지분이 묽어집니다.",
                                                 "investor_meaning": "단기 변동이 커질 수 있습니다.", "watch": "자금 용도 확인.",
                                                 "sources": [], "unchecked_figures": True}}}
        events.check(self.db, CONFIG, now=NOW, reason="manual", sync=False, researcher=None, sender=self.sender,
                     recipient="me@example.com", credentials=("a", "b", "c"), say=self.said.append, dart_key=KEY,
                     dart_fetch=FakeDart(), interpreter=reading)
        _, html = self.sent[0]
        for piece in ("가상전자 배당은 유지, 가상바이오는 증자로", "시장의 메시지", "나 같은 개인 투자자에게", "지켜볼 점",
                      "주의", "기존 주주 지분이 약 10.0% 줄어듭니다",   # The code's figure stays beside the reading.
                      "이 회사 비중은 약", "자동 확인되지 않았습니다"):
            self.assertIn(piece, html)

        def broken(filings, *, today, secrets):
            raise RuntimeError("model down")
        later = NOW.replace(minute=NOW.minute + 5)
        self.sent.clear()
        with closing(ledger.connect(self.db)) as conn:       # Forget the first note so the same filings go again.
            conn.execute("DELETE FROM event_notes")
            conn.commit()
        events.check(self.db, CONFIG, now=later, reason="manual", sync=False, researcher=None, sender=self.sender,
                     recipient="me@example.com", credentials=("a", "b", "c"), say=self.said.append, dart_key=KEY,
                     dart_fetch=FakeDart(), interpreter=broken)
        self.assertIn("공시 해석을 만들지 못해 사실만 전합니다", self.sent[0][1])

    def test_a_failed_lookup_is_said_and_never_breaks_the_check(self):
        self.assertEqual(self.check(FakeDart(fail=True)), [])
        self.assertTrue(any("전자공시 조회 실패(network-error)" in line for line in self.said))
        later = NOW.replace(minute=NOW.minute + 5)
        self.assertEqual([o["status"] for o in self.check(FakeDart(), later)], ["sent"])   # Not marked seen: found again.


def setUpModule():  # noqa: N802 - unittest hook
    fast_ledger.install()


def tearDownModule():  # noqa: N802 - unittest hook
    fast_ledger.uninstall()


if __name__ == "__main__":
    unittest.main()
