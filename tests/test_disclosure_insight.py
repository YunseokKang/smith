import json
import unittest
from datetime import date

from smith import disclosure_insight

FILINGS = [
    {"rcept_no": "20261103800010", "corp_name": "가상전자", "corp_code": "00999999", "filed_on": "2026-11-03",
     "title": "현금ㆍ현물배당결정", "category": "dividend", "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261103800010",
     "figures": {"per_share": {"common": "361"}, "record_date": "2026-09-30"},
     "household": {"total": {"net": "123975"}}, "held_share": "0.12", "held_value": "4000000", "symbols": ["123450"]},
    {"rcept_no": "20261102800020", "corp_name": "가상전자", "corp_code": "00999999", "filed_on": "2026-11-02",
     "title": "현금ㆍ현물배당결정", "category": "dividend", "url": "https://dart.fss.or.kr/x", "figures": None,
     "superseded": "2026-11-03"},
    {"rcept_no": "20261103800099", "corp_name": "가상전자", "corp_code": "00999999", "filed_on": "2026-11-03",
     "title": "연결재무제표기준영업(잠정)실적(공정공시)", "category": "earnings", "url": "https://dart.fss.or.kr/y", "figures": None},
    {"rcept_no": "0001111111-26-000071", "corp_name": "GADG", "corp_code": "CIK1111111", "filed_on": "2026-11-02",
     "title": "8-K · 미등록 주식 발행", "category": "dilution", "url": "https://www.sec.gov/Archives/x.htm", "figures": None,
     "source": "sec"}]
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


def company(key, **fields):
    base = {"company_id": key, "verdict": "intact", "headline": "", "thesis": "", "reaction": "", "what_happened": "",
            "meaning": "", "watch": "", "source_urls": []}
    return {**base, **fields}


class InsightTests(unittest.TestCase):
    def output(self, **overrides):
        companies = [company("00999999", headline="배당 1주당 361원, 직전과 같음", thesis="보유 이유는 그대로입니다.",
                             what_happened="1주당 361원으로 직전 분기와 같습니다.",
                             meaning="배당은 지급일에 자동으로 들어옵니다. 지금 사세요.", watch="내년 1월 이사회."),
                     company("CIK1111111", verdict="watch", headline="새 주식 발행으로 지분 희석", reaction="발표일 -8%",
                             what_happened="새 주식 1,250만 주를 발행합니다. 발표 당일 주가는 12달러까지 내렸습니다.",
                             meaning="자금 용도가 공장 증설이라 장기 성장에 쓰일 수 있습니다.", watch="다음 분기 실적에서 증설 효과 확인.",
                             source_urls=[NEWS, "https://unlisted.example.com"]),
                     company("unknown", what_happened="입력에 없는 회사")]
        return {"companies": companies,
                "sources": [{"url": NEWS, "title": "GADG 증자", "publisher": "Example News", "published_on": "2026-11-02"}],
                **overrides}

    def interpret(self, output, verifier):
        call, calls = answering(output)
        return disclosure_insight.interpret(FILINGS, today=date(2026, 11, 3), secrets=[], caller=call, verifier=verifier), calls

    def test_only_public_facts_go_and_one_reading_covers_a_company(self):
        found, calls = self.interpret(self.output(), checked("reachable"))
        sent = json.loads(calls[0])
        self.assertEqual([f["id"] for f in sent["filings"]], ["20261103800010", "20261103800099", "0001111111-26-000071"])
        self.assertEqual({f["company_id"] for f in sent["filings"]}, {"00999999", "CIK1111111"})
        for private in ("123975", "0.12", "4000000", "held_share", "held_value", "household", "123450"):
            self.assertNotIn(private, calls[0])                    # Household figures and weights never travel.
        self.assertEqual(set(found["companies"]), {"00999999", "CIK1111111"})          # An unknown company is dropped.
        # A company's filings share its one reading (a results release and a dividend are one story, told once).
        self.assertIs(found["by_id"]["20261103800010"], found["by_id"]["20261103800099"])
        self.assertNotIn("20261102800020", found["by_id"])                             # Superseded: not read.
        dividend, issue = found["companies"]["00999999"], found["companies"]["CIK1111111"]
        self.assertEqual(dividend["meaning"], "배당은 지급일에 자동으로 들어옵니다.")       # The order is cut.
        self.assertTrue(dividend["orders_removed"])
        self.assertFalse(dividend["unchecked_figures"])            # 361 is the filing's own figure.
        self.assertEqual((issue["verdict"], issue["reaction"]), ("watch", "발표일 -8%"))
        self.assertEqual(issue["sources"], [NEWS])                 # Only cited pages it listed.
        self.assertTrue(issue["unchecked_figures"])
        # The doubt sits on the sentence that holds the figure no page confirmed, not on the whole reading.
        self.assertEqual(issue["what_happened"], "새 주식 1,250만 주를 발행합니다.* 발표 당일 주가는 12달러까지 내렸습니다.*")
        self.assertEqual(issue["meaning"], "자금 용도가 공장 증설이라 장기 성장에 쓰일 수 있습니다.")

    def test_a_page_that_holds_the_figures_clears_them_and_a_percentage_needs_the_input(self):
        matched, _ = self.interpret(self.output(), checked("matched", ["1250", "12"]))
        self.assertFalse(matched["companies"]["CIK1111111"]["unchecked_figures"])     # The page holds both.
        half, _ = self.interpret(self.output(), checked("matched", ["12"]))
        self.assertEqual(half["companies"]["CIK1111111"]["what_happened"],
                         "새 주식 1,250만 주를 발행합니다.* 발표 당일 주가는 12달러까지 내렸습니다.")
        output = self.output()
        output["companies"][1]["what_happened"] = "발표 당일 주가가 361% 올랐습니다."      # 361 is 가상전자's dividend.
        percent, _ = self.interpret(output, checked("matched", ["361"]))                 # ...and the page's 361 is won.
        self.assertTrue(percent["companies"]["CIK1111111"]["what_happened"].endswith("*"))

    def test_texts_are_cut_at_a_sentence_end_and_stripped_of_markup(self):
        long = "첫 문장입니다. " + "로열티는 예상에 못 미쳤습니다. " * 30
        output = self.output()
        output["companies"][0].update(what_happened=long, thesis="<b>보유 이유는</b> 그대로* 입니다.")
        found, _ = self.interpret(output, checked("reachable"))
        reading = found["companies"]["00999999"]
        self.assertLessEqual(len(reading["what_happened"]), disclosure_insight.LIMITS["what_happened"])
        self.assertTrue(reading["what_happened"].endswith("습니다."))                  # Whole sentences only.
        self.assertEqual(reading["thesis"], "보유 이유는 그대로 입니다.")
        self.assertEqual(disclosure_insight.clip("가나다라 마바사아 자차카타", 10), "가나다라…")   # One long sentence: at a space.

    def test_trade_instructions_are_cut_and_facts_kept(self):
        cases = [("지금 파세요.", True), ("비중을 줄이세요.", True), ("계속 보유하십시오.", True), ("추가 매수를 권합니다.", True),
                 ("분할 매수하시길 바랍니다.", True), ("목표 주가는 20만 원입니다.", True), ("사두세요.", True),
                 ("지금 매수해야 합니다.", True), ("보유 주식을 모두 매도해야 합니다.", True), ("당장 비중을 줄여야 합니다.", True),
                 ("지금 사는 게 좋습니다.", True),
                 ("회사가 장내 매수합니다.", False), ("자사주를 사들입니다.", False), ("배당은 자동으로 들어옵니다.", False),
                 ("최대주주가 지분을 매도했습니다.", False), ("주식을 보유한 주주에게 지급됩니다.", False),
                 ("보유 이유는 그대로입니다.", False)]
        for text, cut in cases:
            with self.subTest(text=text):
                self.assertEqual(bool(disclosure_insight._ORDER.search(text)), cut)


if __name__ == "__main__":
    unittest.main()
