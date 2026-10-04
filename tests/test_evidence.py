import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from smith import ledger
from smith.announcements import FEEDS, Announcement, FeedError, fetch_feed
from smith.evidence import SERIES, EvidenceError, describe, fetch_provider

START, END = date(2025, 9, 1), date(2026, 10, 4)


def ecos_rows(*rows: tuple[str, str]) -> bytes:
    return json.dumps({"StatisticSearch": {"list_total_count": len(rows),
                                           "row": [{"TIME": t, "DATA_VALUE": v} for t, v in rows]}}).encode()


def fred_rows(*rows: tuple[str, str]) -> bytes:
    return json.dumps({"observations": [{"date": d, "value": v, "realtime_start": "2026-10-03"}
                                        for d, v in rows]}).encode()  # realtime_start is not a release date.


class EvidenceTests(unittest.TestCase):
    def test_providers_parse_values_and_report_only_codes(self):
        ecos = fetch_provider("ecos", "k", start=START, end=END,
                              get=lambda url: (200, ecos_rows(("20261001", "3"), ("20261002", ""))))
        self.assertEqual(ecos[0][2:4], (date(2026, 10, 1), "3"))
        self.assertEqual(len(ecos), len([s for s in SERIES if s.provider == "ecos"]))  # Blank value skipped.
        fred = fetch_provider("fred", "k", start=START, end=END,
                              get=lambda url: (200, fred_rows(("2026-10-01", "4.78"), ("2026-10-02", "."))))
        self.assertEqual(fred[0][2:], (date(2026, 10, 1), "4.78", None))  # "." is missing, not 0.
        failures = [
            # A series with no observations fails the provider instead of reporting success.
            ("ecos", (200, b'{"RESULT": {"CODE": "INFO-200", "MESSAGE": "none"}}'), "missing-series:722Y001/0101000"),
            ("fred", (200, fred_rows(("2026-10-02", "."))), "missing-series:DFEDTARU"),
            ("ecos", (200, b'{"RESULT": {"CODE": "INFO-100", "MESSAGE": "bad key k"}}'), "INFO-100"),
            ("fred", (400, b'{"error_code": 400, "error_message": "bad key k"}'), "400"),
            ("fred", (200, b'{"unexpected": []}'), "invalid-response"),
        ]
        for provider, response, code in failures:
            with self.subTest(code=code), self.assertRaises(EvidenceError) as caught:
                fetch_provider(provider, "k", start=START, end=END, get=lambda url, r=response: r)
            self.assertEqual((caught.exception.code, str(caught.exception)), (code, f"{provider}: {code}"))

    def test_revised_values_are_added_not_overwritten(self):
        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)
        first, later = datetime(2026, 10, 2, tzinfo=timezone.utc), datetime(2026, 10, 3, tzinfo=timezone.utc)
        point = ("fred", "DGS10", date(2026, 10, 1), "5.24", None)
        self.assertEqual(ledger.store_evidence(conn, [point], retrieved_at=first), 1)
        self.assertEqual(ledger.store_evidence(conn, [point], retrieved_at=later), 0)
        self.assertEqual(ledger.store_evidence(conn, [point[:3] + ("5.25", None)], retrieved_at=later), 1)
        value = lambda known_at: ledger.load_evidence(conn, known_at=known_at)[("fred", "DGS10")][date(2026, 10, 1)][0]
        self.assertEqual((value(first), value(later)), ("5.24", "5.25"))

    def test_describe_reports_latest_changes_and_staleness(self):
        series = {("fred", "DGS10"): {date(2025, 10, 1): ("4.12", None), date(2026, 7, 1): ("4.49", None),
                                      date(2026, 10, 1): ("5.24", None)},
                  ("fred", "DFF"): {date(2026, 8, 1): ("3.88", None)}}
        rows = {row["spec"].series_id: row for row in describe(series, as_of=date(2026, 10, 4))}
        self.assertEqual((rows["DGS10"]["value"], rows["DGS10"]["change_3m"], rows["DGS10"]["change_12m"]),
                         (Decimal("5.24"), Decimal("0.75"), Decimal("1.12")))
        self.assertFalse(rows["DGS10"]["stale"])
        self.assertTrue(rows["DFF"]["stale"])
        self.assertIsNone(rows["DFF"]["change_12m"])  # No point near a year earlier: unknown, not zero.
        self.assertIsNone(rows["DGS2"]["value"])


if __name__ == "__main__":
    unittest.main()


def rss(*items: str) -> bytes:
    return f"<rss><channel><title>t</title>{''.join(items)}</channel></rss>".encode("utf-8")


def item(link: str, title: str = "FOMC statement", date_text: str = "Wed, 16 Sep 2026 18:00:00 GMT") -> str:
    return f"<item><title>{title}</title><link>{link}</link><pubDate>{date_text}</pubDate></item>"


class AnnouncementTests(unittest.TestCase):
    def test_feed_items_are_untrusted_and_filtered(self):
        feed = FEEDS[0]
        body = rss(item("https://www.federalreserve.gov/a.htm", "Line\nbreak " + "x" * 400),
                   item("https://evil.example/federalreserve.gov"),       # Foreign host.
                   item("http://www.federalreserve.gov/b.htm"),           # Not https.
                   item("https://www.federalreserve.gov/c.htm", date_text="soon"),  # No usable date.
                   item("https://[broken"),                                    # Unparsable link.
                   item("https://www.federalreserve.gov/d.htm", "Rate‮ cut  note"))
        items = fetch_feed(feed, fetch=lambda url: (200, body))
        self.assertEqual([i.link for i in items], ["https://www.federalreserve.gov/a.htm",
                                                   "https://www.federalreserve.gov/d.htm"])
        self.assertEqual(items[1].title, "Rate  cut  note")  # Bidi and line separators removed.
        self.assertTrue(items[0].title.startswith("Line break") and len(items[0].title) == 300)
        for label, response, code in [("not xml", (200, b"<html"), "invalid-response"),
                                      ("empty", (200, rss()), "no-items"),
                                      ("too large", (200, b"x" * 2_000_001), "too-large"),
                                      ("http error", (503, b""), "http-503"),
                                      ("redirect", (301, b""), "http-301")]:  # Redirects are never followed.
            with self.subTest(label), self.assertRaises(FeedError) as caught:
                fetch_feed(feed, fetch=lambda url, r=response: r)
            self.assertEqual(caught.exception.code, code)
        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)
        at = datetime(2026, 10, 4, tzinfo=timezone.utc)
        later = datetime(2026, 10, 5, tzinfo=timezone.utc)
        revised = [Announcement(items[0].feed_id, items[0].link, "Revised title", items[0].published_at)]
        self.assertEqual((ledger.store_announcements(conn, items, retrieved_at=at),
                          ledger.store_announcements(conn, items, retrieved_at=at),
                          ledger.store_announcements(conn, revised, retrieved_at=later)), (2, 0, 1))
        titles = lambda known_at: {a.link: a.title for a in ledger.load_announcements(
            conn, since=datetime(2026, 1, 1, tzinfo=timezone.utc), until=later, known_at=known_at)}
        self.assertEqual((titles(at)[items[0].link], titles(later)[items[0].link]),
                         (items[0].title, "Revised title"))
