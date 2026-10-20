"""The investment principles library and the policy checks derived from it (report design §13)."""
import unittest
from decimal import Decimal

from smith import principles


def position(symbol, share, name=None, kind_hint="stock"):
    return {"symbol": symbol, "name": name or symbol, "share": None if share is None else Decimal(share), "hermes": False,
            "market": "US", "value": Decimal(1)}


class LibraryTests(unittest.TestCase):
    def test_every_principle_is_sourced_from_a_free_primary_page(self):
        ids = [p.id for p in principles.LIBRARY]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 33)                                   # 21 + the reading list's 12.
        for p in principles.LIBRARY:
            with self.subTest(p.id):
                self.assertTrue(p.sources and all(url.startswith("https://") for _, url in p.sources))
                self.assertTrue(p.principle and p.apply and p.title and p.investor)
        # The reading list's entries cite the free primary sources, never a book scan.
        self.assertIn("gutenberg.org/ebooks/60979", principles.BY_ID["LIV-SIT"].url)
        self.assertIn("5장", principles.BY_ID["LIV-SIT"].sources[0][0])
        self.assertIn("franklintempleton.com", principles.BY_ID["TEMP-VALUE"].url)
        self.assertIn("1992", principles.BY_ID["GRA-MOS"].sources[0][0])
        self.assertIn("pbs.org", principles.BY_ID["LYNCH-FLOWERS"].url)
        self.assertEqual([p.id for p in principles.cited(["LIV-TIPS", "nope", "LIV-TIPS", "GRA-MOS"])], ["LIV-TIPS", "GRA-MOS"])

    def test_the_templeton_entries_follow_the_published_rules(self):
        """Review 2026-10-20: rule 13 says outperforming is difficult, not that trying mostly fails; rule 10 says
        hold unless switching to more attractive stocks, never "buy more"."""
        self.assertIn("어려운 일", principles.BY_ID["TEMP-INVEST"].principle)
        self.assertNotIn("대개 실패", principles.BY_ID["TEMP-INVEST"].principle)
        self.assertIn("더 매력적인 종목으로 바꿀 이유가 없는 한", principles.BY_ID["TEMP-NO-PANIC"].principle)
        self.assertNotIn("더 살 때", principles.BY_ID["TEMP-NO-PANIC"].principle)
        for pid in ("LIV-SIT", "LIV-NO-AVERAGE-DOWN", "LIV-TIPS", "LIV-EMOTIONS"):
            self.assertIn("Smith의 해석", principles.BY_ID[pid].apply)                  # A speculator's rule, translated.

    def test_the_stock_count_is_shown_not_judged(self):
        few = [position("AAA", "0.5"), position("VOO", "0.4"), position("BBB", "0.005")]
        self.assertEqual(principles.stock_count(few), (2, 1))
        self.assertEqual(principles.stock_count([position("AAA", None)]), (1, 0))
        self.assertFalse(any(c.key == "follow" for c in principles.policy({"holdings": {"positions": few, "unlisted": Decimal(0),
                                                                                         "total": Decimal(1)},
                                                                            "sectors": {"cash": {"reserve_target": None, "reserve_gap": None,
                                                                                                 "emergency_months_assumption": 6},
                                                                                        "debt": {"loans": []}},
                                                                            "advice": {"glide": None}})))

    def test_the_narrative_gets_the_relevant_part_of_the_library_with_its_opposing_views(self):
        class Check:
            def __init__(self, status, ids):
                self.status, self.principles = status, ids
        data = {"policy": [Check("ok", ("MUN-INVERT", "MARKS-DEFENSIVE", "BUF-NO-LEVERAGE")),
                           Check("breach", ("BUF-CIRCLE", "BUF-KNOW-SOMETHING", "MARKS-DEFENSIVE", "VAN-BALANCE"))],
                "holdings": {"positions": []}}
        chosen = [p.id for p in principles.relevant(data)]
        self.assertEqual(chosen[:4], ["BUF-CIRCLE", "BUF-KNOW-SOMETHING", "MARKS-DEFENSIVE", "VAN-BALANCE"])  # The review's first.
        self.assertLessEqual(len(chosen), principles.RELEVANT_MAX)
        self.assertGreaterEqual(len(chosen), principles.RELEVANT_DEFAULT)
        self.assertEqual(len(chosen), len(set(chosen)))
        self.assertIn("BUF-INDEX", chosen)                                           # The opposing view travels along ...
        self.assertIn("LIV-NO-AVERAGE-DOWN", chosen)                                 # ... (BUF-DCA is in the base set).
        small = [p.id for p in principles.relevant(data, limit=4)]
        self.assertEqual(small[:4], chosen[:4])
        self.assertTrue(set(small[4:]) <= {principles.OPPOSING[i] for i in small[:4] if i in principles.OPPOSING})


if __name__ == "__main__":
    unittest.main()
