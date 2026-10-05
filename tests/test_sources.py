import socket
import unittest
from datetime import datetime, timezone

from smith import research, sources

AT = datetime(2026, 10, 5, tzinfo=timezone.utc)
ITEM = {"ref": "R1", "headline": "기준금리 2.50% 동결", "detail": "한국은행은 기준금리를 연 2.50%로 유지했다. 수도권 0.14% 상승.",
        "source_url": "https://www.bok.or.kr/news/1", "tier": "official"}
PAGE = "<html><head><script>var x = 9.99;</script></head><body><p>기준금리 연 2.50% 유지</p><p>수도권 0.14%</p></body></html>"


class FakeWeb:
    """DNS answers and responses by host; records every (ip, host, path) actually requested."""

    def __init__(self, dns, pages):
        self.dns, self.pages, self.requests = dns, pages, []

    def resolve(self, host):
        if host not in self.dns:
            raise socket.gaierror("no such host")
        return self.dns[host]

    def get(self, ip, host, path):
        self.requests.append((ip, host, path))
        if (host, path) not in self.pages:
            raise TimeoutError("timed out")
        return self.pages[(host, path)]


def html(text, status=200, **headers):
    return status, {"content-type": "text/html; charset=utf-8", **headers}, text.encode("utf-8")


class SourceTests(unittest.TestCase):
    def verify(self, items, web):
        return sources.verify(items, now=AT, resolver=web.resolve, requester=web.get, tier=research.tier)

    def test_a_real_page_with_the_claimed_numbers_is_matched_and_recorded(self):
        web = FakeWeb({"www.bok.or.kr": ["8.8.8.10"]}, {("www.bok.or.kr", "/news/1"): html(PAGE)})
        kept, dropped = self.verify([ITEM], web)
        check = kept[0]["check"]
        self.assertEqual((check["status"], check["matched"], dropped), ("matched", "2/2", {}))
        self.assertEqual(len(check["sha256"]), 64)
        self.assertEqual(kept[0]["tier"], "official")
        self.assertEqual(web.requests, [("8.8.8.10", "www.bok.or.kr", "/news/1")])  # Connected to the checked IP.

    def test_dead_private_and_unresolvable_sources_are_dropped(self):
        items = [dict(ITEM, source_url="https://gone.go.kr/x"), dict(ITEM, source_url="https://rebind.example.com/x"),
                 dict(ITEM, source_url="https://nowhere.example.com/x"),
                 dict(ITEM, source_url="https://mixed.example.com/x")]
        web = FakeWeb({"gone.go.kr": ["8.8.8.1"], "rebind.example.com": ["127.0.0.1"],
                       "mixed.example.com": ["8.8.8.2", "10.0.0.5"]},
                      {("gone.go.kr", "/x"): html("not found", status=404)})
        kept, dropped = self.verify(items, web)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, {"dead-url": 1, "private-address": 2, "no-such-host": 1})
        self.assertEqual([r[1] for r in web.requests], ["gone.go.kr"])  # Nothing sent to a private address.

    def test_redirects_are_checked_hop_by_hop_and_set_the_tier(self):
        web = FakeWeb({"www.molit.go.kr": ["8.8.8.3"], "blog.example.com": ["8.8.8.4"],
                       "internal.example.com": ["192.168.0.1"]},
                      {("www.molit.go.kr", "/a"): html("", status=302, location="https://blog.example.com/b"),
                       ("blog.example.com", "/b"): html(PAGE),
                       ("www.molit.go.kr", "/c"): html("", status=301, location="https://internal.example.com/"),
                       ("www.molit.go.kr", "/d"): html("", status=302, location="http://www.molit.go.kr/d")})
        items = [dict(ITEM, source_url=f"https://www.molit.go.kr/{p}") for p in "acd"]
        kept, dropped = self.verify(items, web)
        self.assertEqual([(k["check"]["final_url"], k["tier"]) for k in kept], [("https://blog.example.com/b", "secondary")])
        self.assertEqual(dropped, {"private-address": 1, "bad-url": 1})   # Redirect to a private host, or to http.

    def test_unmatched_or_unreachable_pages_are_kept_and_marked(self):
        web = FakeWeb({"news.example.com": ["8.8.8.5"], "slow.go.kr": ["8.8.8.6"], "pdf.go.kr": ["8.8.8.7"]},
                      {("news.example.com", "/1"): html("<p>다른 이야기 3.75%</p>"),
                       ("pdf.go.kr", "/f"): (200, {"content-type": "application/pdf"}, b"%PDF")})
        items = [dict(ITEM, source_url="https://news.example.com/1"), dict(ITEM, source_url="https://slow.go.kr/x"),
                 dict(ITEM, source_url="https://pdf.go.kr/f")]
        kept, _ = self.verify(items, web)    # slow.go.kr times out.
        self.assertEqual([(k["check"]["status"], k["tier"]) for k in kept],
                         [("mismatch", "secondary"), ("unchecked", "official"), ("reachable", "official")])
        self.assertEqual([k["ref"] for k in kept], ["R1", "R2", "R3"])

    def test_numbers_ignore_years_and_single_digits(self):
        self.assertEqual(sources.numbers("2026년 9월 4주 0.14% 상승, 1,234억 원, 3건"), {"0.14", "1234"})
        self.assertFalse(sources.public_address("::ffff:127.0.0.1"))
        self.assertFalse(sources.public_address("169.254.169.254"))
        self.assertTrue(sources.public_address("8.8.8.8"))


if __name__ == "__main__":
    unittest.main()
