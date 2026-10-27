import json
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from smith import agenda, daily, evening, evening_html, evening_send, evening_writer, gmail, ledger, netflix_picks, people, talk, weather
from smith.config import _check_evening
from tests.test_weather import LOC, POLLEN_OAK, POLLEN_WEEDS, WARNINGS, air, kma, rows

SEOUL = ZoneInfo("Asia/Seoul")
TONIGHT, DAY = date(2026, 11, 19), date(2026, 11, 20)   # The mail is sent on the 19th about the 20th.
CONFIG = {"app": {"timezone": "Asia/Seoul"}, "daily": {"enabled": True, "airways_sensitive": True},
          "evening": {"enabled": True, "time": "21:00"}, "mail": {"recipient": "client@example.com"}}
CREDS = ("cid", "secret", "refresh")
BOOK = people.parse({"people": [{"id": "nephew", "label": "조카", "relation": "nephew",
                                 "birth": {"calendar": "solar", "year": 2025, "month": 11, "day": 27}},
                                {"id": "mother", "label": "어머니", "relation": "mother",
                                 "birth": {"calendar": "solar", "year": 1960, "month": 11, "day": 20}}]})
EVENTS = [agenda.Event("e1", "치과", datetime(2026, 11, 20, 9, 0, tzinfo=SEOUL), datetime(2026, 11, 20, 9, 30, tzinfo=SEOUL),
                       False, "강남", "", "default", True),
          agenda.Event("e2", "KTX 부산", datetime(2026, 11, 20, 9, 15, tzinfo=SEOUL), datetime(2026, 11, 20, 12, 0, tzinfo=SEOUL),
                       False, None, "", "fromGmail", True)]
TASKS = [agenda.Task("t1", "서류 제출", DAY, "", "내 할 일")]


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=SEOUL).astimezone(timezone.utc)


def collector(db, config, *, now, day):
    return daily.Inputs(None, EVENTS, TASKS, BOOK, None, [], {})


def candidate(ref="T1", title="환승연애4", detail="요즘 많이 보는 연애 예능입니다. 20대 시청자가 많습니다.", url="https://www.osen.co.kr/article/1"):
    return {"ref": ref, "topic": "drama", "headline": title, "detail": detail, "published_on": "2026-11-18",
            "publisher": "OSEN", "source_title": title, "source_url": url, "kind": "fact"}


class TomorrowWeatherTests(unittest.TestCase):
    def test_latest_published_run(self):
        for local, expected in (((21, 0), (20, 0)), ((20, 10), (17, 0)), ((2, 20), (2, 0)), ((1, 0), (23, 0))):
            with self.subTest(local=local):
                now = datetime(2026, 11, 19, *local, tzinfo=SEOUL)
                run = weather.latest_run(now)
                self.assertEqual((run.hour, run.minute), expected)
                self.assertEqual(run.date(), now.date() if expected != (23, 0) else now.date() - timedelta(days=1))

    def test_tomorrow_report_is_the_next_day_with_the_regional_outlook(self):
        dust = {"PM10": [{"informData": "2026-11-20", "informCode": "PM10", "dataTime": "2026-11-19 17시 발표",
                          "informGrade": "서울 : 보통,경기남부 : 보통"}],
                "PM25": [{"informData": "2026-11-19", "informCode": "PM25", "dataTime": "2026-11-19 17시 발표",
                          "informGrade": "경기남부 : 좋음"},
                         {"informData": "2026-11-20", "informCode": "PM25", "dataTime": "2026-11-19 17시 발표",
                          "informGrade": "서울 : 나쁨,경기남부 : 나쁨"}]}
        forecast = rows("20261119", POP={21: "10"}) + rows("20261120", POP={8: "70"}, PTY={8: "1"}, SKY={12: "4"})
        seen = []

        def get(url):
            seen.append(url)
            base = url.split("?")[0]
            if base == weather.DUST_FORECAST_URL:
                return air(dust["PM10" if "InformCode=PM10" in url else "PM25"])
            return {weather.FORECAST_URL: kma(forecast), weather.WARNING_URL: kma(WARNINGS),
                    weather.POLLEN_URLS["잡초류"]: POLLEN_WEEDS}.get(base, (200, POLLEN_OAK))
        report = weather.fetch("KEY", LOC, now=datetime(2026, 11, 19, 21, 0, tzinfo=SEOUL), getter=get, horizon="tomorrow")
        self.assertEqual((report.horizon, report.today.day, report.today.pop_max, report.now), ("tomorrow", DAY, 70, None))
        self.assertIn("base_date=20261119&base_time=2000", next(u for u in seen if u.startswith(weather.FORECAST_URL)))
        self.assertNotIn(weather.STATION_URL, " ".join(seen))           # No measurement: tonight's value is not tomorrow's.
        self.assertEqual((report.air.outlook, report.air.pm25_grade, report.air.pm10_grade, report.air.pm25), (True, 3, 2, None))
        self.assertEqual([(p.kind, p.today) for p in report.pollen if p.in_season], [("잡초류", 1)])   # Tomorrow's index.
        advice = weather.advise(report, airways_sensitive=True)
        self.assertEqual([i for i in advice.items if i in ("우산", "마스크")], ["우산", "마스크"])
        self.assertIn("내일 경기남부 예보: 초미세먼지 나쁨, 미세먼지 보통", advice.air_line)
        light = next(s for s in weather.signals(report, advice, airways_sensitive=True) if s.name == "미세먼지")
        self.assertEqual((light.label, light.detail), ("나쁨", "권역 예보"))
        commute = [weather.window(report.today, label, a, b) for label, a, b in evening.COMMUTE_HOURS]
        self.assertEqual(weather.window_line(commute[0]), "출근길 07~09시 · 기온 자료 없음 · 비 70% (08시부터)")   # Only POP/PTY at 08:00.
        self.assertIsNone(commute[1])                                              # No evening hours in the fixture.
        dry_unknown = weather.Forecast(DAY, 2.0, 9.0, None, (), (), None, None, "", (weather.Hour(7, 4.0, None, None, None),))
        self.assertEqual(weather.window_line(weather.window(dry_unknown, "출근길", 7, 9)), "출근길 07~09시 · 4℃ · 강수 자료 없음")
        shown = weather.for_model(report, advice)
        self.assertEqual((shown["tomorrow"]["date"], shown["horizon"], "today" in shown), ("2026-11-20", "tomorrow", False))


class PrepAndTalkTests(unittest.TestCase):
    def test_prep_lists_tonight_actions_in_order(self):
        inputs = daily.Inputs(None, EVENTS, TASKS, BOOK, None, [], {})
        data = evening.assemble(inputs, day=DAY, tz="Asia/Seoul", now=at(TONIGHT, 21), airways_sensitive=True)
        texts = [p["text"] for p in data["prep"]]
        self.assertEqual(texts[0], "첫 일정 09:00 치과 · 강남: 알람과 나설 시간을 맞춰 두세요")
        self.assertIn("예약 확인: KTX 부산", texts)
        self.assertIn("겹치는 일정: 치과 / KTX 부산. 오늘 밤 하나를 정리해 두세요", texts)
        self.assertIn("내일 기한 할 일: 서류 제출", texts)
        self.assertIn("내일 어머니 생신입니다. 축하 메시지와 선물을 오늘 밤 준비해 두세요", texts)
        self.assertIn("조카 생일까지 8일 남았습니다. 선물이나 예약을 미리 알아보세요", texts)   # D-7 from tomorrow is 8 nights.
        self.assertEqual((data["evening_of"], data["product"], data["talk_kind"]), (TONIGHT, "evening", talk.kind_for(DAY)))

    def test_commute_windows_and_rest_day_hours(self):
        hourly = tuple(weather.Hour(h, t, p, "비" if h >= 19 else None, w)
                       for h, t, p, w in ((7, 2.0, 10, 3.0), (8, 3.0, 10, 9.5), (9, 5.0, 20, 4.0),
                                          (18, 9.0, 30, 2.0), (19, 8.0, 60, 2.0), (20, 7.0, 70, 2.0)))
        forecast = weather.Forecast(DAY, 2.0, 13.0, 70, (19, 20), ("비",), "흐림", 9.5, "20261119 2000", hourly)
        report = weather.Report(forecast, None, None, (), None, (), {}, "tomorrow")
        advice = weather.advise(report, airways_sensitive=False)
        inputs = daily.Inputs(report, [], [], BOOK, None, [], {})
        data = evening.assemble(inputs, day=DAY, tz="Asia/Seoul", now=at(TONIGHT, 21), airways_sensitive=False)
        self.assertEqual([weather.window_line(w) for w in data["commute"]],
                         ["출근길 07~09시 · 2~5℃ · 비 없음 · 바람 강함", "퇴근길 18~20시 · 7~9℃ · 비 70% (19시부터)"])
        self.assertIn("우산을 현관에 꺼내 두세요 (퇴근길 19시부터 비)", [p["text"] for p in data["prep"]])
        self.assertIn("우산", advice.items)
        holiday = daily.Inputs(report, [], [], BOOK, None, [], {})
        rest = evening.assemble(holiday, day=date(2026, 11, 21), tz="Asia/Seoul", now=at(DAY, 21), airways_sensitive=False)
        self.assertEqual([w.label for w in rest["commute"]], ["오후 외출"])   # Saturday: outing hours (no 10~12시 rows here).

    def test_rotation_alternates_warm_and_current(self):
        kinds = [talk.kind_for(DAY + timedelta(days=n)) for n in range(6)]
        self.assertEqual(sorted(kinds), sorted(talk.ROTATION))
        self.assertEqual(talk.kind_for(DAY, ("cheer",)), "cheer")

    def test_only_confirmed_pages_count(self):
        for check, expected in (({"status": "matched", "final_url": "https://www.osen.co.kr/a"}, True),
                                ({"status": "reachable", "final_url": "https://www.osen.co.kr/a"}, True),
                                ({"status": "unchecked", "reason": "timeout"}, False),
                                ({"status": "mismatch", "final_url": "https://www.osen.co.kr/a"}, False),
                                ({"status": "reachable", "final_url": "https://spam.example.com/a"}, False)):
            with self.subTest(check=check):
                self.assertEqual(talk.confirmed_page(candidate() | {"check": check}), expected)

    def test_fallback_line_skips_the_last_month(self):
        data = {"day": DAY, "talk_recent": [{"line": line} for line in evening_html.FALLBACK_LINES[:-1]]}
        self.assertEqual(evening_html.fallback_line(data), evening_html.FALLBACK_LINES[-1])
        data["talk_recent"] = [{"line": line} for line in evening_html.FALLBACK_LINES]   # Newest first: all used.
        self.assertEqual(evening_html.fallback_line(data), evening_html.FALLBACK_LINES[-1])

    def test_select_keeps_only_listed_fresh_pleasant_new_items(self):
        today = date(2026, 11, 19)
        raw = [candidate(),
               candidate(title="환승연애 4"),                                            # Same title, spaced differently.
               candidate(title="블로그 글", url="https://blog.example.com/a"),
               candidate(title="옛 드라마", url="https://www.osen.co.kr/article/2") | {"published_on": "2026-09-01"},
               candidate(title="배우 열애설", detail="열애 논란이 이어집니다.", url="https://www.osen.co.kr/article/3"),
               candidate(title="지난달 예능", url="https://www.osen.co.kr/article/4"),
               candidate(title="흑백요리사2", url="https://n.news.naver.com/article/5")]
        kept, reasons = talk.select(raw, today=today, kind="drama", avoid=["지난달 예능"])
        self.assertEqual([k["headline"] for k in kept], ["환승연애4", "흑백요리사2"])
        self.assertEqual(reasons, {"repeat": 2, "bad-or-unlisted-url": 1, "stale-or-undated": 1, "sour-topic": 1})

    def test_talk_item_checks(self):
        data = {"talk_kind": "drama", "talk_candidates": [candidate()]}
        sent = {"partner_memory": [{"text": "아내는 요즘 이직 준비로 바쁘다"}], "partner_notes": [],
                "recent_lines": ["오늘도 고생 많았어."]}
        cases = [({"ref": "T1", "title": "아무 제목", "line": "요즘 환승연애4 다들 본다던데 같이 볼래?", "note": "지어낸 설명"},
                  ("환승연애4", None)),                                       # The candidate's title, not the model's.
                 ({"ref": "T9", "title": "", "line": "같이 볼래?", "note": ""}, (None, "unknown candidate (T9)")),
                 ({"ref": "T1", "title": "", "line": "환승연애4 시청률 15%래", "note": ""}, (None, "number not in input")),
                 ({"ref": "T1", "title": "", "line": "환승연애4, 20대가 많이 본대", "note": ""}, ("환승연애4", None)),   # 20: detail.
                 ({"ref": "T1", "title": "", "line": "요즘 그 예능 재밌대", "note": ""}, (None, "line does not name the topic")),
                 ({"ref": "T1", "title": "", "line": "환승연애4 출연자가 마약으로 구속됐대", "note": ""}, (None, "sour topic"))]
        for raw, (title, problem) in cases:
            with self.subTest(raw=raw["line"]):
                item, why = evening_writer.verify_talk(raw, data, sent, [])
                self.assertEqual((item["title"] if item else None, why), (title, problem))
        item, _ = evening_writer.verify_talk(cases[0][0], data, sent, [])
        self.assertEqual(item["note"], candidate()["detail"])   # Facts about a topic are the article's, written by code.
        warm = {"talk_kind": "drama", "talk_candidates": []}   # Research found nothing: read as a cheer item.
        item, _ = evening_writer.verify_talk({"ref": "T1", "title": "x", "line": "이직 준비 힘들지? 응원해", "note": ""}, warm, sent, [])
        self.assertEqual((item["kind"], item["ref"], item["title"]), ("cheer", "", ""))
        bare = {"partner_memory": [], "partner_notes": [], "recent_lines": []}   # Nothing said about her: no specifics.
        self.assertEqual(evening_writer.verify_talk({"line": "내일 수술 잘 받고 와"}, {"talk_kind": "cheer"}, bare, []),
                         (None, "no grounds about the partner"))
        for line, problem in (("성공은 포기하지 않는 것이다 — 처칠", "attributed quotation"), ("오늘도 고생 많았어!", "repeats a recent line")):
            with self.subTest(line=line):
                self.assertEqual(evening_writer.verify_talk({"ref": "", "title": "", "line": line, "note": ""},
                                                            {"talk_kind": "cheer"}, sent, []), (None, problem))


class EveningSendTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "smith.db"
        self.sent = []

    def sender(self, credentials, *, recipient, subject, html, text, **kwargs):
        self.sent.append((subject, html, kwargs.get("images", {})))
        return f"m{len(self.sent)}"

    def run_due(self, now):
        return evening_send.run_due(self.db, CONFIG, now=now, recipient="client@example.com", credentials=CREDS,
                                    sender=self.sender, collector=collector)

    def test_due_after_the_slot_once_and_apart_from_the_morning_mail(self):
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(evening_send.due(conn, CONFIG, at(TONIGHT, 20, 50)))
            # The morning mail of the same day is another product: it neither blocks nor is blocked.
            ledger.claim_daily_run(conn, run_key=DAY.isoformat(), day=DAY, run_id="x", trigger="scheduled",
                                   now=at(TONIGHT, 21), lease=evening_send.LEASE, max_attempts=3)
            self.assertEqual(evening_send.due(conn, CONFIG, at(TONIGHT, 21, 5)), DAY)
        self.assertEqual(self.run_due(at(TONIGHT, 21, 5)), ["evening 2026-11-20#evening: sent"])
        subject, html, images = self.sent[0]
        self.assertEqual(subject, "[Smith][내일 준비] 11/20 (금) · 일정 2건 · 첫 일정 09:00 · 어머니 생신 내일")
        self.assertIn("오늘 밤 아내에게", html)
        self.assertEqual(set(images), {"fairy", "weather", "mood"})
        self.assertEqual(self.run_due(at(TONIGHT, 21, 20)), [])                     # Sent: not due again tonight.
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(evening_send.due(conn, CONFIG, at(DAY, 0, 30)))         # After midnight: the morning covers it.
            stored = ledger.daily_snapshot(conn, "2026-11-20#evening")
            self.assertEqual((stored["payload"]["date"], stored["evening_of"], stored["talk"]["kind"]),
                             ("2026-11-20", "2026-11-19", "cheer" if talk.kind_for(DAY) in talk.RESEARCHED else talk.kind_for(DAY)))
            self.assertEqual(daily_send_due(conn), DAY)                              # The morning mail is still due.
        self.assertEqual(stored["prose"]["talk"]["line"], stored["talk"]["line"])   # A reply sees the code's own line too.
        recent = talk.recent(self.db, now=at(TONIGHT, 22), product="evening")
        self.assertEqual(recent[0]["line"], stored["talk"]["line"])

    def test_late_marker_manual_send_and_uncertain_outcome(self):
        late = evening_send.publish(self.db, CONFIG, now=at(TONIGHT, 22, 0), day=DAY, trigger="scheduled",
                                    recipient="client@example.com", credentials=CREDS, sender=self.sender, collector=collector)
        self.assertEqual(late["status"], "sent")
        self.assertTrue(self.sent[0][0].startswith("[Smith][내일 준비 · 지연 발송] 11/20"))
        again = evening_send.publish(self.db, CONFIG, now=at(TONIGHT, 22, 5), day=DAY, trigger="manual",
                                     recipient="client@example.com", credentials=CREDS, sender=self.sender, collector=collector)
        self.assertEqual((again["status"], again["error_code"]), ("skipped", "already-sent"))

        def lost(*args, **kwargs):
            raise gmail.MailError("timeout", uncertain=True)
        past = evening_send.publish(self.db, CONFIG, now=at(DAY + timedelta(days=3), 0, 1), day=DAY + timedelta(days=3), trigger="scheduled",
                                    recipient="client@example.com", credentials=CREDS, sender=self.sender, collector=collector)
        self.assertEqual((past["status"], past["error_code"], len(self.sent)), ("skipped", "past-midnight", 1))   # Not sent.
        other = DAY + timedelta(days=1)
        result = evening_send.publish(self.db, CONFIG, now=at(DAY, 21, 1), day=other, trigger="scheduled",
                                      recipient="client@example.com", credentials=CREDS, sender=lost, collector=collector)
        self.assertEqual(result["status"], "unknown")
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(evening_send.due(conn, CONFIG, at(DAY, 21, 20)))       # Possibly delivered: never resent.

    def test_render_escapes_and_falls_back_without_a_writer(self):
        inputs = daily.Inputs(None, [agenda.Event("e", "<b>회의</b>", datetime(2026, 11, 20, 10, 0, tzinfo=SEOUL),
                                                  datetime(2026, 11, 20, 11, 0, tzinfo=SEOUL), False, None, "", "default", True)],
                              [], BOOK, None, [], {"calendar-x": "y"})
        data = evening.assemble(inputs, day=DAY, tz="Asia/Seoul", now=at(TONIGHT, 21), airways_sensitive=False, kinds=("drama",))
        subject, html, text = evening_html.render(data, partner="여보")
        self.assertNotIn("<b>회의</b>", html)
        self.assertIn("&lt;b&gt;회의&lt;/b&gt;", html)
        self.assertIn("오늘 밤 여보에게", html)
        item = evening_html.talk_item(data)       # A current kind with no verified candidate falls back to a warm line.
        self.assertEqual((item["kind"], item["line"] in evening_html.FALLBACK_LINES), ("cheer", True))
        data["talk_candidates"] = [candidate()]
        item = evening_html.talk_item(data)
        self.assertEqual((item["kind"], item["title"], item["url"]), ("drama", "환승연애4", "https://www.osen.co.kr/article/1"))


def daily_send_due(conn):
    from smith import daily_send
    return daily_send.due(conn, {"app": {"timezone": "Asia/Seoul"}, "daily": {"time": "06:00"}}, at(DAY, 6, 5))


def pick(title="브레이킹 배드", url="https://www.netflix.com/title/70143836", source="https://www.imdb.com/title/tt0903747/"):
    return {"title": title, "year": "2008", "kind": "시리즈", "genre": "범죄 드라마", "why": "에미상 작품상을 받은 범죄 드라마입니다.",
            "netflix_url": url, "source_url": source, "source_title": "IMDb", "publisher": "IMDb"}


NETFLIX_OK = '<html><h1 class="x">브레이킹 배드</h1><ul data-uia="metadata" class="m"><li>브레이킹 배드</li><li>2013</li></ul>'.encode()
NETFLIX_BLOCKED = ('<div data-uia="locally-unavailable">이런! 현재 거주하시는 국가에서는 이 콘텐츠를 시청하실 수 없습니다.</div>'
                   '<h1>인셉션</h1><ul data-uia="metadata"><li>2010</li></ul>').encode()


class NetflixPickTests(unittest.TestCase):
    def test_select_needs_a_netflix_title_and_a_listed_source(self):
        raw = [pick(),
               pick(title="다른 제목", url="https://www.netflix.com/kr/title/70143836"),        # Same Netflix id.
               pick(title="가짜", url="https://netflix.example.com/title/1234567"),
               pick(title="블로그 평", url="https://www.netflix.com/kr/title/81040344", source="https://blog.example.com/x"),
               pick(title="지난주 작품", url="https://www.netflix.com/kr/title/80057281")]
        kept, reasons = netflix_picks.select(raw, avoid=["지난주 작품"])
        self.assertEqual([(k["title"], k["netflix_url"]) for k in kept], [("브레이킹 배드", "https://www.netflix.com/kr/title/70143836")])
        self.assertEqual(reasons, {"repeat": 2, "not-a-netflix-title-url": 1, "bad-or-unlisted-source": 1})

    def test_only_titles_playable_in_korea_stay_under_the_page_s_own_name(self):
        self.assertEqual(netflix_picks.parse_page(NETFLIX_OK), {"title": "브레이킹 배드", "year": "2013", "playable": True})
        self.assertFalse(netflix_picks.parse_page(NETFLIX_BLOCKED)["playable"])
        self.assertFalse(netflix_picks.parse_page(b"<html>" + b"x" * 100)["playable"])   # Cut before the heading: not a yes.
        pages = {"https://www.netflix.com/kr/title/1": {"status": 200, "body": NETFLIX_OK},
                 "https://www.netflix.com/kr/title/2": {"status": 200, "body": NETFLIX_BLOCKED}}

        def fetcher(url):
            if url.endswith("/3"):
                raise TimeoutError("slow")
            return pages[url]
        reasons = {}
        items = [dict(pick(title="모델이 쓴 제목"), netflix_url=f"https://www.netflix.com/kr/title/{n}") for n in (1, 2, 3)]
        kept = netflix_picks.on_netflix(items, fetcher, reasons)
        self.assertEqual([(k["title"], k["year"]) for k in kept], [("브레이킹 배드", "2008~2013")])   # First year to latest season.
        self.assertEqual(reasons, {"netflix-unavailable-here": 1, "netflix-unchecked-TimeoutError": 1})

    def test_acclaim_page_must_name_the_work_and_its_awards(self):
        item = dict(pick(), original_title="Breaking Bad", why="에미상 16개를 받은 범죄 드라마입니다.")
        page = "Breaking Bad is an American crime drama. It won 16 Primetime Emmy Awards."
        cases = ((page, True),
                 ("An American crime drama. It won 16 Primetime Emmy Awards.", False),          # Another work's page.
                 ("Breaking Bad is an American crime drama.", False),                         # Award not on the page.
                 ("Breaking Bad won 12 Primetime Emmy Awards.", False))                       # The number is not.
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(netflix_picks.supports(item, text), expected)
        self.assertFalse(netflix_picks.supports(dict(item, why="아카데미 작품상을 받은 시리즈입니다."), page))

        def fetcher(url):
            return {"status": 200, "final_url": url, "headers": {"content-type": "text/html; charset=utf-8"},
                    "body": f"<html><body>{page}</body></html>".encode()}
        reasons = {}
        self.assertEqual(len(netflix_picks.acclaimed([item, dict(item, why="아카데미 작품상 수상작")], fetcher, reasons)), 1)
        self.assertEqual(reasons, {"acclaim-not-on-page": 1})

    def test_repeat_is_checked_again_under_the_page_title_and_by_netflix_id(self):
        kept, reasons = netflix_picks.select([pick(title="Breaking Bad")], avoid=[], avoid_ids={"70143836"})
        self.assertEqual((kept, reasons), ([], {"repeat": 1}))                          # A past pick's Netflix id.
        reasons = {}
        renamed = [dict(pick(), title="브레이킹 배드")]                                   # The page's title, after on_netflix.
        self.assertEqual(netflix_picks.still_new(renamed, ["브레이킹 배드"], reasons), [])
        self.assertEqual(reasons, {"repeat": 1})

    def test_card_links_each_pick_and_says_when_none_was_confirmed(self):
        inputs = daily.Inputs(None, [], [], BOOK, None, [], {})
        data = evening.assemble(inputs, day=DAY, tz="Asia/Seoul", now=at(TONIGHT, 21), airways_sensitive=False)
        self.assertNotIn("오늘 밤 넷플릭스", evening_html.render(data)[1])             # Not asked for: no card.
        data["netflix"] = []
        self.assertIn("오늘은 확인된 추천을 받지 못했습니다", evening_html.render(data)[1])
        data["netflix"] = [dict(pick(), netflix_url="https://www.netflix.com/kr/title/70143836")]
        html = evening_html.render(data)[1]
        self.assertIn('href="https://www.netflix.com/kr/title/70143836"', html)
        self.assertIn("평가 출처", html)
        stored = evening_send.snapshot(data, "s", evening_html.talk_item(data))
        self.assertEqual(stored["netflix"][0]["title"], "브레이킹 배드")
        # A reply reads the snapshot's prose (daily_qa.context_from_snapshot passes it on as mail_prose), so the picks
        # travel there in order: "두 번째 거 어떤 내용이야?" is answerable.
        self.assertEqual([(p["order"], p["title"]) for p in stored["prose"]["netflix"]], [(1, "브레이킹 배드")])


class SleepCornerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.health = Path(folder.name) / "health"
        (self.health / "activity").mkdir(parents=True)
        days = {(TONIGHT - timedelta(days=n)).isoformat(): {"sleep_minutes": 430, "main_minutes": 430, "bed_at": "23:30",
                                                             "wake_at": "07:00", "in_bed_minutes": 450} for n in range(5)}
        (self.health / "activity" / "days.json").write_text(json.dumps(days), encoding="utf-8")

    def corner(self, state):
        (self.health / "activity" / "sync.json").write_text(json.dumps(state), encoding="utf-8")
        data = evening.assemble(daily.Inputs(None, [], [], BOOK, None, [], {}), day=DAY, tz="Asia/Seoul", now=at(TONIGHT, 21),
                                airways_sensitive=False)
        evening.add_sleep(data, health_folder=self.health, now=at(TONIGHT, 21))
        return data

    def test_a_failing_sleep_source_is_not_read_as_fresh(self):
        fresh = at(TONIGHT, 9).isoformat()
        self.assertEqual(self.corner({"synced_at": fresh, "sources": {"sleep": fresh}, "failed": []})["sleep"].bedtime, "23:30")
        old = at(TONIGHT - timedelta(days=4), 9).isoformat()
        stale = self.corner({"synced_at": fresh, "sources": {"sleep": old, "steps": fresh}, "failed": ["sleep"]})["sleep"]
        self.assertIn("갱신되지 않았습니다", stale.data_note)                     # Steps succeeding does not refresh sleep.
        partial = self.corner({"synced_at": fresh, "sources": {"sleep": at(TONIGHT - timedelta(days=1), 9).isoformat()},
                               "failed": ["sleep"]})["sleep"]
        self.assertIn("수면 자료를 받지 못해", partial.data_note)

    def test_sleep_figures_never_reach_a_model_through_the_reply(self):
        fresh = at(TONIGHT, 9).isoformat()
        data = self.corner({"synced_at": fresh, "sources": {"sleep": fresh}, "failed": []})
        self.assertIn("권장 취침 23:30", evening_html.render(data)[1])            # The mail shows it ...
        stored = evening_send.snapshot(data, "s", evening_html.talk_item(data))
        seen = json.dumps({"payload": stored["payload"], "prose": stored["prose"]}, ensure_ascii=False, default=str)
        self.assertNotIn("23:30", seen)                                          # ... the reply answerer's input does not.
        self.assertNotIn("sleep", stored["prose"])


class ConfigTests(unittest.TestCase):
    def test_evening_settings(self):
        _check_evening({"enabled": True, "time": "21:00", "partner": "아내", "talk_kinds": ["cheer", "drama"]})
        for bad, message in (({"time": "9pm"}, "evening.time"), ({"partner": ""}, "evening.partner"),
                             ({"talk_kinds": ["gossip"]}, "evening.talk_kinds"), ({"weekdays": ["friday", "x"]}, "evening.weekdays")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, message):
                _check_evening(bad)
        self.assertEqual(evening_send.settings({"evening": {"talk_kinds": ["slang", "cheer"]}})["kinds"], ("cheer", "slang"))


if __name__ == "__main__":
    unittest.main()
