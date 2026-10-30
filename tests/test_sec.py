import gzip
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from smith import sec

NOW = datetime(2026, 11, 3, 7, 20, tzinfo=timezone.utc)
CONTACT = "me@example.com"
TICKERS = {"0": {"cik_str": 1111111, "ticker": "GADG", "title": "GADGET CORP"},
           "1": {"cik_str": 2222222, "ticker": "FORN", "title": "FOREIGN PLC"}}


def submissions(*rows):
    """The submissions JSON's parallel arrays for (accession, date, form, items, document) rows."""
    keys = ("accessionNumber", "filingDate", "form", "items", "primaryDocument")
    return {"cik": "1111111", "filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(keys)}}}


class FakeSec:
    def __init__(self, recent=None):
        self.calls, self.recent = [], recent or submissions(
            ("0001111111-26-000070", "2026-11-02", "8-K", "2.02,9.01", "gadg-20261102.htm"),
            ("0001111111-26-000071", "2026-11-02", "8-K", "3.02,5.02", "gadg-20261102b.htm"),
            ("0001111111-26-000060", "2026-11-01", "4", "", "xslF345X06/form4.xml"),       # Insider form: routine here.
            ("0001111111-26-000050", "2026-10-01", "8-K", "1.01", "old.htm"))              # Older than the window.

    def __call__(self, url, contact):
        self.calls.append((url, contact))
        return json.dumps(TICKERS if url == sec.TICKERS else self.recent).encode("utf-8")


class SecTests(unittest.TestCase):
    def test_a_filing_means_what_its_form_and_items_say(self):
        cases = [("8-K", ("2.02", "9.01"), ("earnings", ["실적 발표"])),
                 ("8-K", ("5.02", "3.02"), ("dilution", ["경영진·이사 변경", "미등록 주식 발행"])),   # The graver first.
                 ("8-K", ("4.02",), ("risk", ["과거 재무제표 신뢰 불가"])),
                 ("8-K/A", ("2.02",), ("earnings", ["실적 발표", "정정 보고"])),
                 ("8-K", ("7.01", "9.01"), None),                                     # Regulation FD, exhibits: routine.
                 ("6-K", (), ("corporate", ["해외 발행사 수시 보고"])),
                 ("SCHEDULE 13D/A", (), ("corporate", ["경영 참여 목적 지분 보고의 변경"])),
                 ("10-Q", (), None), ("4", (), None)]
        for form, items, expected in cases:
            with self.subTest(form=form, items=items):
                self.assertEqual(sec.meaning(sec.Filing("x", date(2026, 11, 2), form, items, "u")), expected)

    def test_recent_filings_with_links_and_a_cached_ticker_map(self):
        with tempfile.TemporaryDirectory() as directory:
            cache, fake = Path(directory) / "sec" / "company_tickers.json", FakeSec()
            self.assertEqual(sec.cik("gadg", CONTACT, cache, now=NOW, fetch=fake), 1111111)
            self.assertIsNone(sec.cik("VOO", CONTACT, cache, now=NOW, fetch=fake))       # A fund: not a company filer.
            TICKERS["2"] = {"cik_str": 3333333, "ticker": "BRK-B", "title": "CLASS B"}
            cache.unlink()
            self.assertEqual(sec.cik("BRK.B", CONTACT, cache, now=NOW, fetch=fake), 3333333)   # A broker's spelling.
            del TICKERS["2"]
            self.assertEqual(sum(url == sec.TICKERS for url, _ in fake.calls), 2)          # Cached; re-read once deleted.
            found = sec.filings(1111111, CONTACT, since=date(2026, 10, 31), fetch=fake)
        self.assertEqual([(f.accession, f.items) for f in found],
                         [("0001111111-26-000071", ("3.02", "5.02")), ("0001111111-26-000070", ("2.02", "9.01")),
                          ("0001111111-26-000060", ())])
        self.assertEqual(found[1].url, "https://www.sec.gov/Archives/edgar/data/1111111/000111111126000070/gadg-20261102.htm")
        self.assertTrue(all(contact == CONTACT for _, contact in fake.calls))
        with self.assertRaises(sec.Error) as caught:                                      # The SEC requires a contact.
            sec.filings(1111111, "", since=date(2026, 10, 31), fetch=fake)
        self.assertEqual(caught.exception.code, "no-contact")
        with self.assertRaises(sec.Error):
            sec.filings(1111111, CONTACT, since=date(2026, 10, 31), fetch=FakeSec(recent={"filings": {"recent": []}}))


class FakeResponse:
    def __init__(self, body, encoding="gzip"):
        self.body, self.headers = body, {"Content-Encoding": encoding}

    def read(self, limit=-1):
        return self.body[:limit] if limit >= 0 else self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetcherTests(unittest.TestCase):
    def test_a_truncated_or_expanding_body_is_a_bad_response_not_a_crash(self):
        whole = gzip.compress(b"x" * 5000)
        cases = [(whole[:len(whole) // 2], "bad-response"),       # Cut off mid-stream (EOFError inside gzip).
                 (whole, "bad-response")]                          # Small on the wire, too big once expanded.
        for body, code in cases:
            response = FakeResponse(body)
            with self.subTest(size=len(body)), mock.patch.object(sec, "open_url", return_value=response):
                with mock.patch.object(sec, "MAX_BYTES", 1000), mock.patch.object(sec.time, "sleep"):
                    with self.assertRaises(sec.Error) as caught:
                        sec.default_fetcher("https://data.sec.gov/x.json", CONTACT)
                self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
