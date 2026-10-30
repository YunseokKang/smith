import json
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

from smith import insiders, sec

FORM4 = """<ownershipDocument><aff10b5One>{planned}</aff10b5One><nonDerivativeTable>
{rows}</nonDerivativeTable></ownershipDocument>"""
ROW = """<nonDerivativeTransaction><transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>{shares}</value></transactionShares>
<transactionPricePerShare><value>{price}</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>"""


def form4(*rows, planned="0"):
    return FORM4.format(planned=planned, rows="".join(ROW.format(code=c, shares=s, price=p) for c, s, p in rows)).encode()


class InsiderTests(unittest.TestCase):
    def test_us_counts_open_market_trades_only(self):
        recent = {"filings": {"recent": {
            "accessionNumber": ["0001-26-000003", "0001-26-000002", "0001-26-000001"],
            "filingDate": ["2026-10-20", "2026-10-10", "2026-01-01"], "form": ["4", "4", "4"], "items": ["", "", ""],
            "primaryDocument": ["xslF345X06/a.xml", "b.xml", "old.xml"]}}}
        docs = {"a.xml": form4(("S", "1000", "100"), ("F", "300", "100"), planned="1"),       # A planned sale; tax: not trading.
                "b.xml": form4(("P", "200", "50"), ("A", "5000", "0"), ("G", "10", "0"))}      # A buy; an award and a gift.
        asked = []

        def fetch(url, contact):
            asked.append(url)
            return json.dumps(recent).encode() if "submissions" in url else docs[url.rsplit("/", 1)[-1]]
        found = insiders.us_summary(1, contact="me@example.com", since=date(2026, 8, 1), fetch=fetch)
        self.assertEqual({k: found[k] for k in ("forms", "buys", "buy_value", "sells", "sell_shares", "sell_value", "planned_sells")},
                         {"forms": 2, "buys": "1", "buy_value": "10000", "sells": "1", "sell_shares": "1000",
                          "sell_value": "100000", "planned_sells": "1"})
        self.assertTrue(all("xsl" not in url for url in asked if url.endswith(".xml")))   # The raw XML, not the styled view.
        self.assertEqual(insiders.raw_xml("https://x/1/2/b.xml"), "https://x/1/2/b.xml")

    def test_a_trade_without_a_price_is_counted_not_valued_at_zero(self):
        totals = {"buys": 0, "buy_shares": Decimal(0), "buy_value": Decimal(0), "sells": 0, "sell_shares": Decimal(0),
                  "sell_value": Decimal(0), "planned_sells": 0, "unpriced": 0}
        xml = form4(("P", "100", "50")).replace(b"<value>50</value>", b"<footnoteId id='F1'/>")
        insiders._add(totals, ET.fromstring(xml))
        self.assertEqual((totals["buys"], totals["buy_value"], totals["unpriced"]), (1, Decimal(0), 1))

    def test_kr_totals_ownership_changes_without_names(self):
        rows = [{"rcept_dt": "2026-10-26", "repror": "가상 임원", "isu_exctv_ofcps": "부사장", "isu_main_shrholdr": "-",
                 "sp_stock_lmp_irds_cnt": "-160"},
                {"rcept_dt": "2026-10-16", "repror": "가상 회장", "isu_exctv_ofcps": "회장", "isu_main_shrholdr": "최대주주",
                 "sp_stock_lmp_irds_cnt": "7,000,000"},
                {"rcept_dt": "2026-01-02", "repror": "옛 임원", "isu_exctv_ofcps": "상무", "isu_main_shrholdr": "-",
                 "sp_stock_lmp_irds_cnt": "-99,999,999"}]                                  # Outside the window.

        def fetch(url):
            return json.dumps({"status": "000", "list": rows}, ensure_ascii=False).encode()
        found = insiders.kr_summary("00999999", key="k" * 40, since=date(2026, 8, 1), fetch=fetch)
        self.assertEqual((found["reporters_up"], found["reporters_down"], found["net_shares"], found["largest"]),
                         (1, 1, "6999840", {"role": "회장(최대주주)", "change": "7000000"}))
        self.assertNotIn("가상", json.dumps(found, ensure_ascii=False))                    # No person's name.

    def test_an_old_picture_is_not_shown(self):
        found = {"basis": "us-form4", "as_of": "2026-09-01", "buy_value": "1.5", "sell_value": "2.5"}
        self.assertIsNone(insiders.for_report(found, date(2026, 10, 30)))
        self.assertEqual(insiders.for_report({**found, "as_of": "2026-10-28"}, date(2026, 10, 30))["sell_value"], "2")
        with self.assertRaises(sec.Error):
            insiders.us_summary(1, contact="", since=date(2026, 8, 1), fetch=lambda url, contact: b"{}")


if __name__ == "__main__":
    unittest.main()
