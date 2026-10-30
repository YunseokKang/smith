import json
import unittest
from datetime import date

from smith import disclosure_insight

FILINGS = [
    {"rcept_no": "20261103800010", "corp_name": "가상전자", "filed_on": "2026-11-03", "title": "현금ㆍ현물배당결정",
     "category": "dividend", "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261103800010",
     "figures": {"per_share": {"common": "361"}, "record_date": "2026-09-30"},
     "household": {"total": {"net": "123975"}}, "held_share": "0.12", "symbols": ["123450"]},
    {"rcept_no": "20261102800020", "corp_name": "가상전자", "filed_on": "2026-11-02", "title": "현금ㆍ현물배당결정",
     "category": "dividend", "url": "https://dart.fss.or.kr/x", "figures": None, "superseded": "2026-11-03"},
    {"rcept_no": "0001111111-26-000071", "corp_name": "GADG", "filed_on": "2026-11-02", "title": "8-K · 미등록 주식 발행",
     "category": "dilution", "url": "https://www.sec.gov/Archives/x.htm", "figures": None, "source": "sec"}]
NEWS = "https://news.example.com/gadg"


def answering(output):
    calls = []

    def call(prompt):
        calls.append(prompt)
        return output, "0.30"
    return call, calls


def checked(status, found=()):
    def verifier(items, tier=None):
        return [dict(i, check={"status": status, "found": list(found)}) for i in items], {}
    return verifier


class InsightTests(unittest.TestCase):
    def output(self, **overrides):
        filings = [{"id": "20261103800010", "signal": "positive",
                    "market_message": "1주당 361원으로 직전 분기와 같아, 회사가 이익 흐름에 자신 있다는 신호로 읽힙니다.",
                    "investor_meaning": "배당은 지급일에 자동으로 들어오니 따로 할 일은 없습니다. 지금 사세요.",
                    "watch": "다음 배당 결정은 내년 1월 이사회.", "source_urls": []},
                   {"id": "0001111111-26-000071", "signal": "caution",
                    "market_message": "새 주식 1,250만 주를 발행하고, 발표 당일 주가는 12달러까지 내렸습니다.",
                    "investor_meaning": "자금 용도가 공장 증설이라 장기 성장에 쓰일 수 있지만 단기 변동은 커질 수 있습니다.",
                    "watch": "다음 분기 실적에서 증설 효과 확인.", "source_urls": [NEWS, "https://unlisted.example.com"]},
                   {"id": "9999", "signal": "positive", "market_message": "입력에 없는 공시", "investor_meaning": "",
                    "watch": "", "source_urls": []}]
        return {"overview": "가상전자 배당은 그대로, GADG는 증자로 지분이 묽어집니다.", "filings": filings,
                "sources": [{"url": NEWS, "title": "GADG 증자", "publisher": "Example News", "published_on": "2026-11-02"}],
                **overrides}

    def test_only_public_facts_go_and_the_reading_is_checked(self):
        call, calls = answering(self.output())
        found = disclosure_insight.interpret(FILINGS, today=date(2026, 11, 3), secrets=[], caller=call,
                                             verifier=checked("reachable"))
        sent = json.loads(calls[0])
        self.assertEqual([f["id"] for f in sent["filings"]], ["20261103800010", "0001111111-26-000071"])  # Superseded out.
        for private in ("123975", "0.12", "held_share", "household", "123450"):
            self.assertNotIn(private, calls[0])                    # Household figures and weights never travel.
        self.assertEqual(set(found["by_id"]), {"20261103800010", "0001111111-26-000071"})   # An unknown id is dropped.
        dividend, issue = found["by_id"]["20261103800010"], found["by_id"]["0001111111-26-000071"]
        self.assertEqual(dividend["investor_meaning"], "배당은 지급일에 자동으로 들어오니 따로 할 일은 없습니다.")  # Order cut.
        self.assertTrue(dividend["orders_removed"])
        self.assertFalse(dividend["unchecked_figures"])             # 361 is the filing's own figure.
        self.assertEqual(issue["sources"], [NEWS])                  # Only cited pages it listed.
        self.assertTrue(issue["unchecked_figures"])                 # 1,250 and 12 are on no page code matched.
        call, _ = answering(self.output())
        matched = disclosure_insight.interpret(FILINGS, today=date(2026, 11, 3), secrets=[], caller=call,
                                               verifier=checked("matched", ["1250", "12"]))
        self.assertFalse(matched["by_id"]["0001111111-26-000071"]["unchecked_figures"])   # The page holds both.
        call, _ = answering(self.output())
        half = disclosure_insight.interpret(FILINGS, today=date(2026, 11, 3), secrets=[], caller=call,
                                            verifier=checked("matched", ["12"]))
        self.assertTrue(half["by_id"]["0001111111-26-000071"]["unchecked_figures"])       # 1,250 is on no page.
        output = self.output()
        output["filings"][1]["market_message"] = "발표 당일 주가가 361% 올랐습니다."
        call, _ = answering(output)
        percent = disclosure_insight.interpret(FILINGS, today=date(2026, 11, 3), secrets=[], caller=call,
                                               verifier=checked("matched", ["361"]))     # The page's 361 is won.
        self.assertTrue(percent["by_id"]["0001111111-26-000071"]["unchecked_figures"])

    def test_another_filings_figure_proves_nothing_and_the_overview_is_checked_too(self):
        output = self.output(overview="지금 매수하세요. GADG 주가가 361% 올랐습니다.")
        output["filings"][1]["market_message"] = "발표 당일 주가가 361% 올랐습니다."      # 361 is 가상전자's dividend.
        call, _ = answering(output)
        found = disclosure_insight.interpret(FILINGS, today=date(2026, 11, 3), secrets=[], caller=call,
                                             verifier=checked("reachable"))
        self.assertTrue(found["by_id"]["0001111111-26-000071"]["unchecked_figures"])
        self.assertEqual(found["overview"], "GADG 주가가 361% 올랐습니다.")                 # The instruction is cut...
        self.assertTrue(found["overview_unchecked"])                                       # ...and its figure flagged.

    def test_trade_instructions_are_cut_and_facts_kept(self):
        cases = [("지금 파세요.", True), ("비중을 줄이세요.", True), ("계속 보유하십시오.", True), ("추가 매수를 권합니다.", True),
                 ("분할 매수하시길 바랍니다.", True), ("목표 주가는 20만 원입니다.", True), ("사두세요.", True),
                 ("지금 매수해야 합니다.", True), ("보유 주식을 모두 매도해야 합니다.", True), ("당장 비중을 줄여야 합니다.", True),
                 ("지금 사는 게 좋습니다.", True),
                 ("회사가 장내 매수합니다.", False), ("자사주를 사들입니다.", False), ("배당은 자동으로 들어옵니다.", False),
                 ("최대주주가 지분을 매도했습니다.", False), ("주식을 보유한 주주에게 지급됩니다.", False)]
        for text, cut in cases:
            with self.subTest(text=text):
                self.assertEqual(bool(disclosure_insight._ORDER.search(text)), cut)


if __name__ == "__main__":
    unittest.main()
