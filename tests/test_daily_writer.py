import json
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from smith import config, daily, daily_html, daily_news, daily_writer, people
from smith.memory import Fact

DAY = date(2026, 11, 20)
NOW = datetime(2026, 11, 19, 21, 0, tzinfo=timezone.utc)
BOOK = people.parse({"people": [{"id": "wife", "label": "와이프", "relation": "spouse",
                                 "birth": {"calendar": "solar", "year": 1989, "month": 11, "day": 27}},
                                {"id": "father", "label": "아버지", "relation": "father",
                                 "birth": {"calendar": "solar", "year": 1960, "month": 11, "day": 20}}]})


def data_for_test():
    inputs = daily.Inputs(None, [], [], BOOK, None, [], {})
    data = daily.assemble(inputs, day=DAY, tz="Asia/Seoul", now=NOW, airways_sensitive=True)
    data["news"] = [{"ref": "R1", "headline": "금통위 앞두고 시장 관망", "detail": "채권 금리가 보합을 보였다.",
                     "publisher": "연합뉴스", "published_on": "2026-11-20", "source_url": "https://www.yna.co.kr/view/x"}]
    return data


class WriterTests(unittest.TestCase):
    def test_verify_keeps_only_what_the_input_supports(self):
        data = data_for_test()
        keys = [r.occurrence.key for r in data["reminders"]]
        self.assertEqual(sorted(keys), ["birthday:father:2026", "birthday:wife:2026"])   # Today and D-7.
        payload_text = str(daily.payload(data))
        output = {"headline": "포근한 금요일, 가볍게", "weather_line": "기온 12~25도라 외투 하나면 됩니다.",
                  "agenda_line": "", "sentence": "오늘도 한 걸음이면 충분합니다. - 괴테",
                  "tasks_line": "밀린 일 23건은 천천히 하나씩 보셔도 됩니다.",
                  "anniversaries": [{"key": "birthday:wife:2026", "idea": "좋아하는 향수를 미리 주문해 두세요 https://shop.example"},
                                    {"key": "birthday:father:2026", "idea": "저녁 식사 자리를 예약해 드릴까요"},
                                    {"key": "birthday:nobody:2026", "idea": "x"}],
                  "news": [{"ref": "R1", "summary": "금통위를 앞두고 시장이 관망세입니다."}, {"ref": "R9", "summary": "없는 기사"}]}
        prose, problems = daily_writer.verify(output, data, payload_text,
                                              tastes_known={"birthday:wife:2026": True, "birthday:father:2026": False})
        self.assertNotIn("headline", prose)                       # Weather is None today: no weather headline...
        self.assertNotIn("weather_line", prose)                   # ...and no weather sentence either.
        self.assertNotIn("sentence", prose)                       # Attributed quotation dropped.
        self.assertNotIn("tasks_line", prose)                     # "23건" is not in the input (two-digit figures are checked).
        self.assertEqual(prose["anniversaries"][0]["idea"], "좋아하는 향수를 미리 주문해 두세요")
        self.assertEqual([i["key"] for i in prose["anniversaries"]], ["birthday:wife:2026", "birthday:father:2026"])
        self.assertNotIn("http", prose["anniversaries"][0]["idea"])
        self.assertTrue(prose["anniversaries"][1]["idea"].endswith(daily_writer.ASK_TASTES))   # No tastes known: it asks.
        self.assertEqual(prose["news"], [{"ref": "R1", "summary": "금통위를 앞두고 시장이 관망세입니다."}])
        self.assertEqual(len(problems), 5)   # weather_line (12·25 not in input), sentence, tasks_line, a key, a ref.

    def test_numbers_are_checked_as_tokens_of_the_input(self):
        data = data_for_test()
        payload = daily.payload(data)
        payload["weather"] = {"today": {"tmin": 12.0, "tmax": 25.0, "pop_max": 30}}
        weather_pool = daily_writer.pool("weather_line", payload)
        self.assertEqual(daily_writer._foreign_numbers("기온 12도에서 25도, 비 30%", weather_pool), [])
        self.assertEqual(daily_writer._foreign_numbers("기온 20도", weather_pool), ["20"])      # The date no longer vouches for 20.
        self.assertEqual(daily_writer._foreign_numbers("강수확률 9%", weather_pool), ["9"])     # Single digits are checked too.
        self.assertEqual(daily_writer._foreign_numbers("일정 0건, 뉴스 1건", daily_writer.pool("agenda_line", payload)), [])   # Counts.
        self.assertEqual(daily_writer._foreign_numbers("1950년 참전", daily_writer.pool("sentence", payload)), ["1950"])     # Not the sentence's.
        self.assertTrue(daily_writer._attributed("처칠에 따르면 성공은 열정을 잃지 않는 것이다"))
        self.assertTrue(daily_writer._attributed("니체의 말처럼 춤추는 별을 낳으려면"))

    def test_tastes_are_known_only_from_memory_about_that_person(self):
        data = data_for_test()
        facts = [Fact("family", "와이프는 향수와 꽃을 좋아한다", DAY, "대화")]
        known = daily_writer._tastes_known(data, facts)
        self.assertEqual(known, {"birthday:wife:2026": True, "birthday:father:2026": False})
        self.assertTrue(daily_writer._attributed("성공은 넘어진 횟수보다 한 번 더 일어나는 것이다 — 처칠"))
        self.assertFalse(daily_writer._attributed("오늘도 한 걸음이면 충분합니다."))

    def test_prose_is_tidied_after_the_checks(self):
        # Natural Korean (report design §34): double passives and dashes are fixed once a text has passed its checks,
        # so an attribution dash is still seen as one.
        data = data_for_test()
        output = {"headline": "", "weather_line": "", "agenda_line": "일정은 없습니다 — 여유로운 하루입니다.",
                  "sentence": "오늘도 한 걸음이면 충분합니다 — 처칠", "tasks_line": "", "anniversaries": [],
                  "news": [{"ref": "R1", "summary": "금리가 보합이라고 보여집니다."}]}
        prose, _ = daily_writer.verify(output, data, str(daily.payload(data)), tastes_known={})
        self.assertEqual(prose["agenda_line"], "일정은 없습니다, 여유로운 하루입니다.")
        self.assertNotIn("sentence", prose)
        self.assertEqual(prose["news"][0]["summary"], "금리가 보합이라고 보입니다.")
        self.assertEqual(prose["korean_fixes"], [{"kind": "dash", "before": "—", "after": ","},   # The record for the footer.
                                                 {"kind": "double-passive", "before": "보여집", "after": "보입"}])


def news_item(headline, url, published_on="2026-11-20", topic="경제", scores=("3", "3", "3")):
    return {"topic": topic, "headline": headline, "detail": "내용", "published_on": published_on, "publisher": "p",
            "source_title": "t", "source_url": url, "kind": "fact", **dict(zip(daily_news.SCORES, scores))}


class NewsTests(unittest.TestCase):
    def test_selection_keeps_listed_overnight_scored_shapes(self):
        raw = [news_item("A 기사", "https://www.yna.co.kr/view/AKR1"),
               news_item("A 기사", "https://www.yna.co.kr/view/AKR2"),                       # Same headline.
               news_item("B 기사", "https://someblog.example.com/p", topic="기술"),
               news_item("C 기사", "https://news.kbs.co.kr/news/1", "2026-11-18"),           # Two days back: not overnight.
               news_item("C2 기사", "https://news.kbs.co.kr/news/2", "2026-11-21"),          # Dated tomorrow.
               news_item("D 기사", "http://www.bbc.com/news/1", "2026-11-19"),
               news_item("E 기사 연락 010-1234-5678", "https://www.reuters.com/world/x", "2026-11-19", "AI"),
               news_item("F 기사", "https://www.chosun.com/x", topic="스포츠"),
               news_item("G 기사", "https://www.chosun.com/g", scores=("3", "high", "3")),
               {k: v for k, v in news_item("H 기사", "https://www.chosun.com/h").items() if k != "urgency"}]
        kept, reasons = daily_news.select(raw, today=DAY, topics=["정치", "경제", "문화", "기술", "AI"])
        self.assertEqual([k["headline"] for k in kept], ["A 기사", "E 기사 연락 [식별정보 제거]", "F 기사"])
        self.assertEqual((kept[2]["topic"], kept[0]["urgency"]), ("기타", 3))
        self.assertEqual(reasons, {"duplicate": 1, "host-not-listed": 1, "stale-or-undated": 2, "bad-url": 1,
                                   "unscored": 2})
        self.assertTrue(daily_news.allowed_host("https://m.yna.co.kr/x") and not daily_news.allowed_host("https://yna.co.kr.evil.com/x"))

    def test_the_model_gets_outlets_window_and_interests_and_code_ranks_the_picks(self):
        # BUG-2026-008: unaware of the list, the model chose unlisted outlets and the 11/18 mail carried no news.
        # User 2026-11-18: overnight events, highest in importance, urgency and relevance to the reader.
        raw = [news_item("n1", "https://n.news.naver.com/article/001/1"), news_item("n2", "https://n.news.naver.com/article/001/2"),
               news_item("ex", "https://www.example.org/a"), news_item("plain", "http://bbc.com/news/1"),
               news_item("low", "https://www.mt.co.kr/x/1", scores=("2", "1", "1")),
               news_item("tie-calm", "https://www.yna.co.kr/a", scores=("4", "2", "3")),
               news_item("tie-urgent", "https://www.yna.co.kr/b", scores=("3", "4", "2")),
               news_item("top", "https://www.reuters.com/c", scores=("5", "4", "5"))]
        sent = []

        def run(args, **kwargs):
            sent.append(json.loads(kwargs["input"]))
            out = {"subtype": "success", "is_error": False, "total_cost_usd": 0.1,
                   "structured_output": {"items": raw, "gaps": []}}
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
        interests = daily_news.clean_interests([" 금리  대출 ", 7, "", "가" * 40])
        result = daily_news.research_news(today=DAY, topics=["경제"], interests=interests, count=3, secrets=[],
                                          executable=Path("claude"), runner=run, verifier=lambda items: (items, {}))
        self.assertEqual(interests, ["금리 대출", "7", "가" * 30])
        self.assertEqual((sent[0]["outlets"], sent[0]["interests"], sent[0]["since"]),
                         (list(daily_news.HOSTS), interests, "2026-11-19 18:00 KST"))
        self.assertEqual([(i["ref"], i["headline"]) for i in result["items"]],
                         [("R1", "top"), ("R2", "tie-urgent"), ("R3", "tie-calm")])   # Sum, then urgency; "low" is cut.
        self.assertEqual(result["unlisted_hosts"], {"n.news.naver.com": 2, "www.example.org": 1})   # Not the http one.

    def test_the_interests_setting_is_validated(self):
        config._check_daily({"news_interests": ["금리·대출", "AI"]})
        for bad in ("금리", ["가" * 31], [" "], ["a"] * 9, [3]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config._check_daily({"news_interests": bad})

    def test_the_card_marks_urgent_items_and_reads_unscored_briefs(self):
        data = data_for_test()   # Its one item has no scores, like a brief stored before daily-news-v3.
        data["news"] = data["news"] + [dict(data["news"][0], ref="R2", headline="수도권 지하철 파업", urgency=5)]
        _, html, _ = daily_html.render(data)
        self.assertEqual(html.count("오늘 확인"), 1)
        self.assertIn("중요도·긴급도·관련도가 높은 순으로 2건", html)

    def test_store_and_reuse_through_the_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "smith.db"
            calls = []

            def researcher(**kwargs):
                calls.append(kwargs["count"])
                return {"items": [{"ref": "R1", "headline": "h", "detail": "d", "source_url": "https://www.yna.co.kr/x",
                                   "published_on": "2026-11-20", "publisher": "연합뉴스", "topic": "경제", "kind": "fact"}],
                        "gaps": [], "dropped": {}, "cost_usd": "0.10"}
            first = daily_news.research_and_store(db, now=NOW, today=DAY, topics=["경제"], interests=["금리"], count=5,
                                                  secrets=[], executable=Path("claude"), researcher=researcher)
            second = daily_news.research_and_store(db, now=NOW, today=DAY, topics=["경제"], interests=["금리"], count=5,
                                                   secrets=[], executable=Path("claude"), researcher=researcher)
            changed = daily_news.research_and_store(db, now=NOW, today=DAY, topics=["경제"], interests=["환율"], count=5,
                                                    secrets=[], executable=Path("claude"), researcher=researcher)
            self.assertEqual((first["outcome"], second["outcome"], changed["outcome"], calls),
                             ("success", "reused", "success", [5, 5]))   # New interests: a new brief.
            self.assertEqual(second["items"][0]["headline"], "h")


if __name__ == "__main__":
    unittest.main()
