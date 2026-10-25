import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from smith import agenda, daily, daily_html, holidays, people, weather

TZ = "Asia/Seoul"
DAY = date(2026, 11, 20)   # A Friday six days before the 2026-11-26 rate meeting.
NOW = datetime(2026, 11, 19, 21, 0, tzinfo=timezone.utc)
SEOUL = ZoneInfo(TZ)
FORECAST = weather.Forecast(DAY, 2.0, 11.0, 70, (9, 10), ("비",), "흐림", 5.0, "20261120 0200")
REPORT = weather.Report(FORECAST, None, weather.Observation("2026-11-20 05:00", 3.1, 80.0, None, 1.0),
                        (weather.Warning("강풍", "주의보", "경기도(성남)", True),),
                        weather.AirQuality("정자동", "2026-11-20 05:00", 60.0, 38.0, 2, 3, "나쁨"),
                        (weather.Pollen("잡초류", None, None, False),), {})
EVENTS = [agenda.Event("e1", "연차", date(2026, 11, 20), date(2026, 11, 21), True, None, "", "default", True),
          agenda.Event("e2", "치과", datetime(2026, 11, 20, 14, 0, tzinfo=SEOUL), datetime(2026, 11, 20, 14, 30, tzinfo=SEOUL),
                       False, "분당 서울치과", "", "fromGmail", True)]
TASKS = [agenda.Task("t1", "보고서 제출", date(2026, 11, 10), "", "내 할 일"),
         agenda.Task("t2", "오늘 것", DAY, "", "내 할 일")]
BOOK = people.parse({"people": [{"id": "nephew", "label": "조카", "relation": "nephew",
                                 "birth": {"calendar": "solar", "year": 2025, "month": 11, "day": 27}}],
                     "dates": [{"id": "passport", "label": "여권 만료", "kind": "expiry", "date": "2026-12-20"}]})
AHEAD = [holidays.Holiday(date(2026, 12, 25), "기독탄신일")]


def build(**overrides):
    inputs = daily.Inputs(REPORT, EVENTS, TASKS, BOOK, None, AHEAD, {}, weather_label="성남시 분당구")
    inputs = daily.Inputs(**{**inputs.__dict__, **overrides})
    return daily.assemble(inputs, day=DAY, tz=TZ, now=NOW, airways_sensitive=True)


class AssembleTests(unittest.TestCase):
    def test_assembly_is_deterministic_and_dates_only(self):
        data = build()
        self.assertEqual((data["weekday"], data["rest_day"], data["holiday"]), ("금", False, None))
        # D-7 of the 돌 and D-30 of the passport expiry (an expiry's default reminders are 30 and 7 days before).
        self.assertEqual([(r.occurrence.label, r.days_left) for r in data["reminders"]], [("조카 생일", 7), ("여권 만료", 30)])
        self.assertEqual(data["reminders"][0].occurrence.milestone, "돌")
        self.assertEqual([t.title for t in data["tasks"]["overdue"]], ["보고서 제출"])
        self.assertEqual([t.title for t in data["tasks"]["due_today"]], ["오늘 것"])
        self.assertEqual([(f["label"], f["days"]) for f in data["finance_dates"]],
                         [("한국은행 금통위 (기준금리 결정)", 6), ("종합부동산세 납부 시작", 11)])
        self.assertEqual(data["weather"]["advice"].items, ("우산", "외투(아침·저녁 쌀쌀)", "마스크"))
        payload = daily.payload(data)
        self.assertEqual(payload["reminders"][0]["milestone"], "돌")
        self.assertNotIn("37.", str(payload))                                  # No coordinates, no ids.
        self.assertNotIn("e1", str(payload["agenda"]))

    def test_finance_dates_inside_an_open_tax_period(self):
        found = daily.finance_dates(date(2026, 7, 20))
        self.assertEqual([(f["label"], f["days"], f.get("open")) for f in found], [("재산세 1기분 납부 마감", 11, True)])
        self.assertEqual(daily.finance_dates(date(2026, 3, 3)), [])

    def test_render_carries_every_block_and_marks_failures(self):
        data = build()
        subject, html, text = daily_html.render(data)
        self.assertEqual(subject, "[Smith][하루 일과] 11/20 (금) · ⚠ 강풍주의보 · 일정 2건 · 할 일 1건 · 조카 생일 D-7")
        self.assertEqual(daily.payload(data)["weather"]["location"], "성남시 분당구")
        for piece in ("좋은 아침입니다", "성남시 분당구 날씨 기준", "성남시 분당구 정자동 측정소", "⚠ 준비물: 우산", "초미세먼지 38",
                      "오늘 일정 2건 · 할 일 1건", "분당 서울치과", "예약",
                      "오늘 기한", "조카 생일", "★ 돌", "여권 만료", "미완료 할 일", "보고서 제출", "기독탄신일", "금통위", "종합부동산세",
                      "■</span> 강풍주의보", "■</span> 나쁨", "○</span> 계절 밖"):
            self.assertIn(piece, html, piece)
        lights = weather.signals(REPORT, data["weather"]["advice"], airways_sensitive=True)
        self.assertEqual([(s.name, s.level, s.label) for s in lights],
                         [("날씨", "critical", "강풍주의보"), ("미세먼지", "serious", "나쁨"), ("꽃가루", "none", "계절 밖"),
                          ("특보", "critical", "강풍주의보")])
        mild = weather.Forecast(DAY, 2.0, 11.0, 55, (9,), ("비",), "흐림", 3.0, "20261120 0200")   # Under the 70% "큰 비" line.
        moderate = weather.Report(mild, None, None, (), weather.AirQuality("정자동", "", 40.0, 30.0, 2, 2, None), (), {})
        calm = weather.advise(moderate, airways_sensitive=True)
        self.assertEqual([(s.level, s.label) for s in weather.signals(moderate, calm, airways_sensitive=True)][:2],
                         [("warning", "우산·외투"), ("warning", "보통 · 기관지 주의")])   # Upper half of 보통 for a sensitive airway.
        self.assertEqual(weather.signals(moderate, weather.advise(moderate, airways_sensitive=False), airways_sensitive=False)[1].label, "보통")
        self.assertNotIn("오늘의 뉴스", html)                                        # No news yet: no empty card.
        self.assertLess(html.index("조카 생일"), html.index("여권 만료"))             # Soonest first in the dates table.
        self.assertIn("남은 날", html)
        self.assertIn("- 종일 연차", text)
        broken = build(weather_report=None, tasks=None, failures={"weather": "not-configured", "tasks": "http-500"})
        subject, html, _ = daily_html.render(broken)
        self.assertNotIn("강풍", subject)
        self.assertIn("날씨 자료를 오늘 받지 못했습니다", html)
        self.assertIn("할 일 목록을 오늘 읽지 못했습니다", html)
        self.assertIn("오늘 받지 못한 자료: tasks(http-500), weather(not-configured)", html)

    def test_prose_replaces_only_its_own_lines(self):
        data = build()
        data["prose"] = {"weather_line": "비와 바람이 있는 날이라 우산과 외투를 챙기세요.", "sentence": "오늘도 한 걸음.",
                         "anniversaries": [{"key": "birthday:nephew:2026", "idea": "돌잔치 선물로 은수저 세트"}]}
        _, html, _ = daily_html.render(data)
        self.assertIn('src="cid:fairy"', html)                                        # The mail references attachments...
        self.assertIn('src="cid:weather"', html)
        self.assertIn('src="cid:mood"', html)
        _, inline_html, _ = daily_html.render(data, inline=True)
        self.assertIn("data:image/png;base64,", inline_html)                           # ...and the preview embeds them.
        from smith import daily_art
        self.assertEqual(daily_art.weather_key(REPORT, data["weather"]["advice"]), "storm")   # A relevant 강풍주의보 wins.
        self.assertEqual(sorted(daily_art.images_for(data)), ["fairy", "mood", "weather"])
        self.assertEqual(daily_art.mood_key("nope", date(2026, 11, 20)) in daily_art.MOODS, True)
        self.assertIn("비와 바람이 있는 날이라", html)
        self.assertIn("오늘의 문장", html)
        self.assertIn("돌잔치 선물로 은수저 세트", html)
        self.assertIn("초미세먼지 38", html)                                          # The code's air line stays.


class HolidayTests(unittest.TestCase):
    def test_rest_days_and_long_weekends(self):
        ahead = [holidays.Holiday(date(2026, 10, 3), "개천절"), holidays.Holiday(date(2026, 10, 5), "대체공휴일"),
                 holidays.Holiday(date(2026, 10, 9), "한글날")]
        today = date(2026, 9, 30)                                                   # Wednesday.
        self.assertEqual(holidays.long_weekends(ahead, today), [(date(2026, 10, 3), date(2026, 10, 5)),
                                                                (date(2026, 10, 9), date(2026, 10, 11))])
        self.assertTrue(holidays.is_rest_day(date(2026, 10, 4), None))
        self.assertFalse(holidays.is_rest_day(date(2026, 10, 6), None))


if __name__ == "__main__":
    unittest.main()
