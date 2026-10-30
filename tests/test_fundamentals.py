import unittest
from datetime import date
from decimal import Decimal

from smith import fundamentals


def row(name, now, before=None, fs="CFS", sj="IS"):
    return {"account_nm": name, "fs_div": fs, "sj_div": sj, "thstrm_amount": now, "frmtrm_amount": before,
            "rcept_no": "20260814003699"}


def fact(start, end, val, filed="2026-08-26", fp="Q2"):
    return {"start": start, "end": end, "val": val, "filed": filed, "fp": fp, "form": "10-Q"}


class FundamentalsTests(unittest.TestCase):
    def test_a_kr_periodic_report_is_read_consolidated_first(self):
        rows = [row("매출액", "1,000", "800", fs="OFS"), row("매출액", "2,000", "1,000"), row("영업이익", "500", "100"),
                row("당기순이익(손실)", "400", "50"), row("부채총계", "300", sj="BS"), row("자본총계", "1,000", sj="BS")]
        found = fundamentals._kr_read([r for r in rows if r["fs_div"] == "CFS"], 2026, "2Q")
        self.assertEqual((found["revenue"], found["revenue_prior"], found["period"], found["filed"]),
                         ("2000", "1000", "2026년 4~6월", "2026-08-14"))
        shown = fundamentals.summary(found, date(2026, 10, 30))
        self.assertEqual((shown["revenue_yoy"], shown["operating_margin"], shown["debt_ratio"], shown["operating_income_yoy"]),
                         ("1.0000", "0.2500", "0.3000", "4.0000"))

    def test_us_facts_take_the_newest_quarter_of_the_newest_tag(self):
        taxonomy = {
            "Revenues": {"units": {"USD": [fact("2019-01-01", "2019-03-31", 1)]}},          # An old tag, abandoned.
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                fact("2025-04-28", "2025-07-27", 40_000_000_000, filed="2025-08-27"),
                fact("2026-01-26", "2026-07-26", 170_000_000_000),                          # Six months: not a quarter.
                fact("2026-04-27", "2026-07-26", 96_000_000_000)]}},
            "OperatingIncomeLoss": {"units": {"USD": [fact("2026-04-27", "2026-07-26", 64_000_000_000),
                                                      fact("2025-04-28", "2025-07-27", -1_000_000_000)]}},
            "NetIncomeLoss": {"units": {"USD": [fact("2026-04-27", "2026-07-26", 60_000_000_000)]}},
            "Liabilities": {"units": {"USD": [{"end": "2026-07-26", "val": 90_000_000_000, "filed": "2026-08-26"}]}},
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": {"units": {"USD": [
                {"end": "2026-07-26", "val": 225_000_000_000, "filed": "2026-08-26"}]}}}
        found = fundamentals._us_read(taxonomy, fundamentals._US_CONCEPTS["us-gaap"])
        self.assertEqual((found["period"], found["revenue"], found["revenue_prior"], found["equity"]),
                         ("2026년 5~7월", "96000000000", "40000000000", "225000000000"))
        shown = fundamentals.summary({**found, "basis": "SEC"}, date(2026, 10, 30))
        self.assertEqual((shown["revenue"], shown["revenue_yoy"], shown["debt_ratio"]), ("960억 달러", "1.4000", "0.4000"))
        self.assertIsNone(shown["operating_income_yoy"])         # From a loss: no meaningful growth rate.

    def test_lines_in_another_currency_than_revenue_are_unknown(self):
        taxonomy = {"Revenues": {"units": {"USD": [fact("2026-04-01", "2026-06-30", 100_000_000)]}},
                    "OperatingIncomeLoss": {"units": {"EUR": [fact("2026-04-01", "2026-06-30", 50_000_000)]}}}
        found = fundamentals._us_read(taxonomy, fundamentals._US_CONCEPTS["us-gaap"])
        self.assertIsNone(found["operating_income"])                       # Not 50 million dollars.
        self.assertIsNone(fundamentals.summary({**found, "basis": "SEC"}, date(2026, 10, 30))["operating_margin"])

    def test_what_the_report_reads(self):
        old = {"period": "2024년 4~6월", "period_end": "2024-06-30", "currency": "KRW", "basis": "DART", "revenue": "1"}
        self.assertIsNone(fundamentals.summary(old, date(2026, 10, 30)))          # An old period is not current.
        tiny = {"period": "x", "period_end": "2026-06-30", "currency": "USD", "basis": "SEC", "revenue": "80000",
                "operating_income": "-64000000"}
        self.assertIsNone(fundamentals.summary(tiny, date(2026, 10, 30))["operating_margin"])   # -80000%: no meaning.
        cases = [(Decimal("171499470000000"), "KRW", "171.5조 원"), (Decimal("2000000000000"), "KRW", "2조 원"),
                 (Decimal("-800000000000"), "KRW", "-8,000억 원"), (Decimal("91000000"), "USD", "9,100만 달러")]
        for amount, unit, text in cases:
            with self.subTest(text=text):
                self.assertEqual(fundamentals.readable(amount, unit), text)


class FilingSourceTests(unittest.TestCase):
    def test_a_holdings_own_filings_are_a_source_only_when_the_input_has_them(self):
        from smith import narrative
        holding = {"symbol": "123450", "research_topic": "H1",
                   "fundamentals": {"period": "2026년 4~6월", "revenue": "171.5조 원", "revenue_yoy": "1.3000"}}
        payload = {"household": {}, "brief": None, "holdings": [holding]}
        text = ["가상전자는 2026년 4~6월 매출 171.5조 원으로 1년 전보다 130% 늘었습니다."]
        self.assertIsNone(narrative.check_block(text, ["filing"], payload))
        self.assertEqual(narrative.check_block(text, [], payload), "no-source")
        bare = {**payload, "holdings": [{**holding, "fundamentals": None}]}
        self.assertEqual(narrative.check_block(text, ["filing"], bare), "unknown-ref")   # Nothing filed in the input.
        opinion = {"reason": text[0], "alternative": "", "reconsider": "", "refs": ["filing"]}
        self.assertIsNone(narrative._holding_source(opinion, holding, {}))
        self.assertEqual(narrative._holding_source({**opinion, "refs": []}, holding, {}), "no-holding-source")


if __name__ == "__main__":
    unittest.main()
