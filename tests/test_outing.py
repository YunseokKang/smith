"""Weekend outing picks (FR-57, daily design §18): the dates, the range, the taste from the client's own words, the
weather fit and the official sources' shapes (as called 2026-11-18). Every event and place here is synthetic."""
import io
import json
import tempfile
import time
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from smith import config, memory, outing, outing_command, outing_sources
from smith.outing_sources import DayWeather, Event

SAT, SUN = date(2031, 3, 8), date(2031, 3, 9)
HOME = (37.50, 127.00)


def event(title: str, *, source: str = "seoul", category: str = "전시/미술", start: date = SAT, end: date = SUN,
          district: str = "중구", place: str = "시립 전시관", lat: float | None = 37.56, lon: float | None = 126.98,
          audience: str = "누구나", id_: str | None = None) -> Event:
    return Event(source, id_ or title, title, start, end, place, district, category, lat, lon, "무료", audience)


def sky(day: date, pop: int = 10, air: str | None = "보통", tmax: float = 15.0) -> DayWeather:
    return DayWeather(day, "흐리고 비" if pop >= 60 else "맑음", pop, 5.0, tmax, "단기예보", air)


ART = outing.Taste({"art": outing.LIKED}, {"art": "미술 전시 관람을 즐겨요"})
SEOUL = outing.Range(("서울",), ())


def rank(events: list[Event], **kw: object) -> outing.Outcome:
    args = {"days": [SAT, SUN], "weather": [sky(SAT), sky(SUN)], "taste": ART, "scope": SEOUL, "home": HOME,
            "airways_sensitive": False, "top": 3}
    args.update(kw)
    return outing.rank(events, **args)   # type: ignore[arg-type]


class DatesAndRangeTest(unittest.TestCase):
    def test_this_weekend_joins_adjacent_holidays(self) -> None:
        wed, thu, fri, mon = date(2031, 3, 5), date(2031, 3, 6), date(2031, 3, 7), date(2031, 3, 10)
        for today, rest, expected in (
                (wed, set(), [SAT, SUN]), (SAT, set(), [SAT, SUN]), (SUN, set(), [SUN]),
                (thu, {fri}, [fri, SAT, SUN]), (wed, {mon}, [SAT, SUN, mon]),
                (SAT, {fri}, [SAT, SUN])):                  # A holiday already past is not this weekend.
            with self.subTest(today=today, rest=rest):
                self.assertEqual(outing.weekend(today, rest), expected)

    def test_range_from_question_or_default(self) -> None:
        default = ["서울", "고양시", "구리시"]
        scope = outing.area_range(default)
        self.assertEqual((scope.provinces, scope.cities), (("서울",), ("고양시", "구리시")))
        self.assertTrue(scope.contains(event("서울 전시")))
        self.assertTrue(scope.contains(event("고양 축제", source="tour", district="고양시 덕양구", place="경기도 고양시 덕양구 1")))
        self.assertFalse(scope.contains(event("수원 축제", source="tour", district="수원시 권선구", place="경기도 수원시 권선구 1")))
        self.assertEqual(outing.provinces_to_read(scope), ["11", "28", "41"])
        narrowed = outing.area_range(default, "종로구")   # A 구 the question names replaces the default.
        self.assertFalse(narrowed.contains(event("중구 전시", district="중구")))
        self.assertTrue(narrowed.contains(event("종로 전시", district="종로구")))
        self.assertEqual(outing.area_range(default, "성수역").cities, ("고양시", "구리시"))   # A 역 does not narrow (yet).
        for words, found in (("종로구", "종로구"), ("과천시에서 주말", "과천시"), ("성수역 근처", None), ("친구 만나서", None),
                             ("입구에서", None), ("역시", None), ("", None)):
            with self.subTest(words=words):
                self.assertEqual(outing_command.district_named(words), found)

    def test_config_default_areas(self) -> None:
        outing_check = getattr(config, "_check_outing")
        outing_check({"default_areas": ["서울", "과천시"], "evening": True})
        for bad in ({"default_areas": []}, {"default_areas": ["서울 강남"]}, {"evening": "yes"}, {"default_areas": "서울"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                outing_check(bad)


class TasteTest(unittest.TestCase):
    def test_only_the_clients_leisure_words_count(self) -> None:
        facts = [memory.Fact("preference", "경험 소비 취향: 미술·역사 전시 관람, 테니스 등 웰니스", None, "메일"),
                 memory.Fact("preference", "역사적 저점에서는 분할 매수한다", None, "메일"),    # Finance, no leisure cue.
                 memory.Fact("plan", "주말에 공원 산책을 즐겨요", None, "메일")]           # Not a preference fact.
        taste = outing.taste_from_memory(facts)
        self.assertEqual(set(taste.liked()), {"art", "history", "wellness"})
        self.assertNotIn("nature", taste.weights)
        self.assertEqual(outing.taste_from_memory([]).liked(), [])

    def test_a_dislike_is_never_a_liking(self) -> None:
        def taste(text: str) -> outing.Taste:
            return outing.taste_from_memory([memory.Fact("preference", text, None, "메일")])
        for text, liked, disliked in (
                ("미술 전시를 좋아하지 않습니다", [], ["art"]),
                ("미술 전시는 싫고 클래식을 좋아합니다", ["music"], ["art"]),
                ("클래식 공연은 좋아하지만 뮤지컬은 별로예요", ["music"], ["stage"]),
                ("미술 전시를 좋아해요. 현대 미술 전시는 싫어요", [], [])):      # Both ways: unclear, neutral.
            with self.subTest(text=text):
                found = taste(text)
                self.assertEqual((found.liked(), found.disliked()), (liked, disliked))
        disliked = rank([event("가을 기획전"), event("시민 음악회", category="클래식")],
                        taste=taste("미술 전시는 싫고 클래식을 좋아합니다")).picks
        self.assertEqual(disliked[0].event.title, "시민 음악회")


class RankTest(unittest.TestCase):
    def test_taste_weather_and_filters(self) -> None:
        events = [
            event("[시립미술관] 봄 기획전"),                                                   # liked, indoor
            event("동네 봄꽃 축제", category="축제-자연/경관"),                                  # outdoor, neutral
            event("어린이 그림책 전시"),                                                      # liked but for kids
            event("다음 달 전시", start=SUN + timedelta(days=20), end=SUN + timedelta(days=40)),   # not this weekend
            event("시민 오케스트라 단원 모집", category="클래식"),                               # a call for members
            event("안양 전시", source="tour", category="EV030400", district="안양시 동안구", place="경기도 안양시"),
            event("이미 보낸 전시", id_="sent-1"),
        ]
        out = rank(events, recent={"seoul:sent-1"})
        titles = [p.event.title for p in out.picks]
        self.assertEqual(titles[0], "[시립미술관] 봄 기획전")
        self.assertIn("좋아하시는 미술·전시", out.picks[0].reasons)
        self.assertLess(titles.index("동네 봄꽃 축제"), titles.index("어린이 그림책 전시"))   # Variety, then kids last.
        self.assertEqual(out.ruled_out, {"이번 주말에 열리지 않음": 1, "모집·공모 공고": 1, "범위 밖": 1,
                                         f"최근 {outing.REPEAT_DAYS}일 안에 추천함": 1})

    def test_outdoor_needs_a_dry_day_and_clean_air_for_weak_airways(self) -> None:
        fair = event("한강 야외 축제", category="축제-기타")
        wet_sun = [sky(SAT, pop=70), sky(SUN, pop=10)]
        pick = rank([fair], weather=wet_sun).picks[0]
        self.assertEqual(pick.day, SUN)                                                  # The dry day.
        self.assertTrue(any("일요일 맑음" in r for r in pick.reasons))
        exhibit = rank([event("실내 기획전")], weather=wet_sun).picks[0]
        self.assertIn("실내라 토요일 비와 상관없음", exhibit.reasons)
        dusty = [sky(SAT, air="나쁨"), sky(SUN, air="매우나쁨")]
        self.assertEqual(rank([fair], weather=dusty, airways_sensitive=True).picks, [])
        self.assertEqual(len(rank([fair], weather=dusty, airways_sensitive=False).picks), 1)
        cold = rank([fair], weather=[sky(SAT, tmax=2.0), sky(SUN, tmax=2.0)]).picks[0]
        self.assertTrue(any("쌀쌀함(낮 최고 2도)" in r for r in cold.reasons))
        # A concert category held on an outdoor stage is outdoors (Codex review 2026-11-18).
        stage = event("가을 음악회", category="클래식", place="한빛공원 야외무대")
        self.assertEqual(rank([stage], weather=dusty, airways_sensitive=True).picks, [])
        hall = event("가을 음악회", category="클래식", place="한빛공원 음악홀")
        self.assertEqual(len(rank([hall], weather=dusty, airways_sensitive=True).picks), 1)

    def test_stated_hours_decide_weekend_opening(self) -> None:
        for hours, open_ in (("월-금 10:30~18:30", False), ("평일 10:00~17:00", False), ("화-일 10:00~18:00", True),
                             ("평일 10시, 토 11시", True), ("매일 10:00~20:00", True), ("", True),
                             ("매월 셋째주 토요일 15:00", True)):
            with self.subTest(hours=hours):
                item = Event("seoul", hours or "none", "가을 기획전", SAT, SUN, "시립 전시관", "중구", "전시/미술",
                             37.56, 126.98, "무료", "누구나", hours)
                out = rank([item])
                self.assertEqual(bool(out.picks), open_)
                if not open_:
                    self.assertEqual(out.ruled_out, {"운영 시간이 평일뿐": 1})

    def test_a_day_the_hours_close_or_leave_out_is_never_the_day_to_go(self) -> None:
        cases = [
            # (the hours text, the weekdays it closes, the weekdays it gives times for; 0 = Monday)
            ("9:00~18:00 / 일, 월 및 한글날 휴무", {0, 6}, set()),       # The card of 2026-11-19 sent the client on a Sunday.
            ("매주 월요일 휴관", {0}, set()),
            ("토·일 운영, 월 휴관", {0}, set()),
            ("주말 및 공휴일 휴관", {5, 6}, set()),
            ("공휴일 휴관", set(), set()),                               # The 일 of 공휴일 is not Sunday.
            ("화-금 10:30~20:00 / 토 10:30~19:00", set(), {1, 2, 3, 4, 5}),
            ("토·일 15:00", set(), {5, 6}),
            ("일 2회 공연(14:00, 17:00)", set(), set()),                 # Twice a day, not Sunday.
            ("매일 10:00~20:00", set(), set()),
            ("14:00", set(), set()),
            # A wrongly removed open day is worse than a missed closure (Codex review 2026-11-19): a list joined by a
            # word is read whole, and a closure on some weeks of the month or a day not understood limits nothing.
            ("토요일 및 일요일 10:00~18:00", set(), {5, 6}),
            ("토요일과 일요일은 쉽니다", {5, 6}, set()),
            ("매월 첫째 일요일 휴관", set(), set()),
            ("매월 셋째주 토요일 15:00", set(), set()),
            ("월 1회 14:00", set(), set()),
            ("월요일, 1월 1일 휴관", set(), set()),
            ("11/21(토) 17:00", set(), {5}),
            # Second review pass: a range is closed by a closure word like a list, a week of the month in figures and a
            # negated closure close nothing.
            ("토~일 휴관", {5, 6}, set()),
            ("화~일 운영", set(), {1, 2, 3, 4, 5, 6}),
            ("매월 2, 4주 일요일 휴관", set(), set()),
            ("일요일 휴무 없음", set(), set()),
        ]
        for hours, closed, listed in cases:
            with self.subTest(hours=hours):
                self.assertEqual(outing.closed_weekdays(hours), closed)
                self.assertEqual(outing.listed_weekdays(hours), listed)
        for hours, days in (("9:00~18:00 / 일, 월 및 한글날 휴무", [SAT]), ("화-금 10:30~20:00 / 토 10:30~19:00", [SAT]),
                            ("토 휴무, 일 10:00~17:00", [SUN]), ("주말 휴관", []), ("토~일 휴관", []),
                            ("매월 2, 4주 일요일 휴관", [SAT, SUN]), ("일요일 휴무 없음", [SAT, SUN])):
            with self.subTest(hours=hours):
                item = Event("seoul", "h", "가을 기획전", SAT, SUN, "시립 전시관", "중구", "전시/미술", 37.56, 126.98, "무료",
                             "누구나", hours)
                self.assertEqual(outing.open_days(item, [SAT, SUN]), days)
                self.assertEqual([p.day for p in rank([item]).picks], days[:1])
        # The reader is one pass over the text: a long line of day names with no closure costs no more than its length
        # (the first pattern took a second for eighteen of them).
        began = time.perf_counter()
        self.assertEqual(outing.read_hours("토 " * 5000 + "안내"), (set(), set()))
        self.assertLess(time.perf_counter() - began, 0.5)

    def test_duplicates_keep_seouls_row_and_standing_programmes_sink(self) -> None:
        tour = event("봄 기획전", source="tour", category="EV030400", place="서울특별시 중구 1", id_="7")
        seoul = event("[시립] 봄 기획전")
        out = rank([tour, seoul])
        self.assertEqual([p.event.source for p in out.picks], ["seoul"])
        standing = event("상설 전통 의식", source="tour", category="EV020100", place="서울특별시 중구",
                         start=date(2031, 1, 1), end=date(2031, 12, 31))
        out = rank([standing, event("주말 시장 축제", category="축제-기타")], taste=outing.Taste({}, {}))
        self.assertEqual(out.picks[0].event.title, "주말 시장 축제")
        self.assertIn("늘 하는 상설 행사", out.picks[1].reasons)

    def test_history_round_trip_and_damage(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "outing" / "history.json"
            pick = rank([event("봄 기획전", id_="42")]).picks[0]
            outing.remember(path, outing.sent_entries([pick]), SAT)
            recent = outing.recent_keys(path, SAT + timedelta(days=10))
            self.assertIn("seoul:42", recent)
            self.assertEqual(outing.recent_keys(path, SAT + timedelta(days=outing.REPEAT_DAYS)), set())
            # Next time the same event comes only from the other source: still not repeated (Codex review 2026-11-18).
            other = event("봄 기획전", source="tour", category="EV030400", place="서울특별시 중구 1", id_="7")
            self.assertEqual(rank([other], recent=recent).ruled_out, {f"최근 {outing.REPEAT_DAYS}일 안에 추천함": 1})
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(outing.OutingUnavailable):
                outing.recent_keys(path, SAT)


class SourcesTest(unittest.TestCase):
    def test_tour_and_seoul_shapes(self) -> None:
        tour = outing_sources._tour_event({"contentid": "4100001", "title": "봄 디저트 페스타", "eventstartdate": "20310308",
                                           "eventenddate": "20310309", "addr1": "경기도 수원시 권선구 예시로 1",
                                           "lclsSystm3": "EV030200", "mapy": "37.25", "mapx": "127.0"})
        assert tour is not None
        self.assertEqual((tour.district, tour.start, tour.lat), ("수원시 권선구", SAT, 37.25))
        self.assertIsNone(outing_sources._tour_event({"contentid": "1", "title": "x", "eventstartdate": "2031",
                                                      "eventenddate": "20310309"}))
        seoul = outing_sources._seoul_event({
            "CODENAME": "전시/미술", "GUNAME": "중구", "TITLE": "[시립] 봄 기획전", "PLACE": "시립 전시관",
            "STRTDATE": "2031-03-01 00:00:00.0", "END_DATE": "2031-04-30 00:00:00.0", "IS_FREE": "무료",
            "LAT": "37.56", "LOT": "126.97", "USE_TRGT": "누구나", "PRO_TIME": "10:00~18:00",
            "HMPG_ADDR": "https://culture.seoul.go.kr/culture/culture/cultureEvent/view.do?cultcode=160001&menuNo=200008"})
        assert seoul is not None
        self.assertEqual((seoul.id, seoul.fee, seoul.district), ("160001", "무료", "중구"))
        self.assertTrue(seoul.link.startswith("https://culture.seoul.go.kr/"))

    def test_seoul_answers_and_failures_never_show_the_key(self) -> None:
        key = "abcdef0123456789abcd"

        def opener_for(body: object) -> object:
            def opener(url: str, timeout: int) -> io.BytesIO:
                return io.BytesIO(json.dumps(body).encode("utf-8"))
            return opener

        self.assertEqual(outing_sources.seoul_events(key, "전시/미술", opener=opener_for(
            {"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}})), [])
        for body, code in (({"RESULT": {"CODE": "INFO-100"}}, "seoul-INFO-100"), ([], "seoul-bad-body")):
            with self.subTest(code=code), self.assertRaises(outing_sources.SourceError) as caught:
                outing_sources.seoul_events(key, "전시/미술", opener=opener_for(body))
            self.assertEqual(caught.exception.code, code)

        def refused(url: str, timeout: int) -> io.BytesIO:
            raise urllib.error.URLError(f"refused {url}")
        with self.assertRaises(outing_sources.SourceError) as caught:
            outing_sources.seoul_events(key, "전시/미술", opener=refused)
        self.assertNotIn(key, str(caught.exception))

    def test_dust_outage_is_named_not_shown_as_not_yet_published(self) -> None:
        from smith import weather

        location = weather.Location(37.5, 127.0, "예시측정소", "서울", "1100000000", ("서울",))

        def get(url: str) -> tuple[int, dict[str, object]]:
            if "ArpltnInforInqireSvc" in url:
                return 200, {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": "22"}}}
            return 200, {"response": {"header": {"resultCode": "03"}}}
        kst = timezone(timedelta(hours=9))
        found = outing_sources.weekend_weather("k", location, [SAT, SUN], now=datetime(2031, 3, 7, 21, 0, tzinfo=kst),
                                               get=get)
        self.assertEqual(found.failures.get("air"), "portal-22")
        out = rank([event("가을 기획전")], weather=found.days)
        out.failures = found.failures
        self.assertIn("미세먼지 못 읽음", outing.render(out, datetime(2031, 3, 7, 21, 0, tzinfo=kst)))

    def test_mid_term_days(self) -> None:
        issued = date(2031, 3, 4)
        land = {"wf4Am": "맑음", "wf4Pm": "흐리고 비", "rnSt4Am": 20, "rnSt4Pm": 60, "wf8": "맑음", "rnSt8": 10}
        temp = {"taMin4": -1, "taMax4": 9}
        day4 = outing_sources._mid_day((land, temp, issued, "중기예보"), issued + timedelta(days=4))
        assert day4 is not None
        self.assertEqual((day4.sky, day4.pop, day4.tmax, day4.wet), ("맑음 → 흐리고 비", 60, 9.0, True))
        self.assertIsNone(outing_sources._mid_day((land, temp, issued, "x"), issued + timedelta(days=3)))
        kst = timezone(timedelta(hours=9))
        self.assertEqual(outing_sources.mid_run(datetime(2031, 3, 4, 6, 10, tzinfo=kst)).hour, 18)   # Yesterday's.
        self.assertEqual(outing_sources.mid_run(datetime(2031, 3, 4, 19, 0, tzinfo=kst)).hour, 18)


class SearchTest(unittest.TestCase):
    def test_end_to_end_without_seoul_key(self) -> None:
        festival = {"contentid": "4100002", "title": "봄 시장 축제", "eventstartdate": "20310308",
                    "eventenddate": "20310309", "addr1": "서울특별시 중구 예시로 1", "lclsSystm3": "EV030200",
                    "mapy": "37.56", "mapx": "126.98"}
        calls: list[str] = []

        def get(url: str) -> tuple[int, dict[str, object]]:
            calls.append(url.split("?")[0].rsplit("/", 1)[1])
            if "searchFestival2" in url:
                items: object = {"item": [festival]} if "lDongRegnCd=11" in url else ""
            elif "detailCommon2" in url:
                items = {"item": [{"homepage": '<a href="https://example.org/spring">홈페이지</a>'}]}
            else:
                return 200, {"response": {"header": {"resultCode": "03"}}}   # Weather: no data.
            return 200, {"response": {"header": {"resultCode": "0000"}, "body": {"items": items}}}

        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "smith.db"
            settings = {"app": {"timezone": "Asia/Seoul"}, "outing": {"default_areas": ["서울"]}}
            now = datetime(2031, 3, 5, 21, 0, tzinfo=timezone(timedelta(hours=9)))
            out = outing_command.search(db, settings, now, key_loader=lambda p: "k" if p == "datagokr" else None,
                                        get=get, record=True)
            self.assertEqual([p.event.title for p in out.picks], ["봄 시장 축제"])
            self.assertEqual(out.picks[0].event.link, "https://example.org/spring")
            self.assertTrue(any("smith outing login" in n for n in out.notes))
            self.assertEqual(outing.recent_keys(outing.history_path(db), now.date()), {"tour:4100002", "title:봄시장축제"})
            self.assertIn("searchFestival2", calls)
            text = outing.render(out, now)
            self.assertIn("1. 봄 시장 축제\n   [축제] 중구 · 3/8~3/9", text)
            self.assertIn("가기 전 확인: https://example.org/spring", text)
            with self.assertRaises(outing.OutingUnavailable):
                outing_command.search(db, settings, now, key_loader=lambda p: None, get=get)
            opened: list[str] = []

            def down(url: str, timeout: int) -> io.BytesIO:
                opened.append(url)
                raise urllib.error.URLError("down")
            out = outing_command.search(db, settings, now, key_loader=lambda p: "k" * 20, get=get, opener=down)
            self.assertEqual(len(opened), 1)                    # Seoul down: one wait, not one per category.
            self.assertEqual(out.failures["seoul"], "network-error")


class EveningCardTest(unittest.TestCase):
    def test_thursday_only_optional_and_used_up_after_sending(self) -> None:
        from unittest import mock

        from smith import evening_html, outing_evening

        found = rank([event("[시립] 봄 기획전", id_="9")])
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(outing_command, "search", return_value=found) as search:
            db = Path(folder) / "smith.db"
            settings = {"app": {"timezone": "Asia/Seoul"}}
            now = datetime(2031, 3, 6, 12, 0, tzinfo=timezone.utc)                     # 21:00 in Seoul.
            wednesday = {"day": date(2031, 3, 6), "failures": {}}
            outing_evening.step(db, settings, wednesday, now)
            self.assertNotIn("outing", wednesday)
            thursday = {"day": date(2031, 3, 7), "failures": {}}
            outing_evening.step(db, {**settings, "outing": {"evening": False}}, thursday, now)
            self.assertNotIn("outing", thursday)
            outing_evening.step(db, settings, thursday, now)
            self.assertEqual(search.call_count, 1)
            self.assertEqual(search.call_args.args[2].utcoffset(), timedelta(hours=9))   # Household-local.
            card = getattr(evening_html, "_outing")(thursday)
            self.assertIn("이번 주말 나들이", card)
            # The card reads as labelled rows (user review 2026-11-19): the organizer leaves the title for its own row,
            # the kind and the day to go are pills, and the distance stands with the place instead of among the reasons.
            self.assertIn(">봄 기획전</", card)
            self.assertNotIn("[시립] 봄 기획전", card)
            for label in ("어디", "언제", "추천 이유", "주최"):
                self.assertIn(f">{label}</td>", card, label)
            self.assertIn("요일 추천</span>", card)
            self.assertIn(">시립</td>", card)
            self.assertIn("찾은 범위: ", card)
            self.assertNotIn("추천 날:", card)
            self.assertEqual(outing.recent_keys(outing.history_path(db), now.date()), set())   # Not sent yet.
            outing_evening.after_send(db, thursday, now.date())
            self.assertEqual(outing.recent_keys(outing.history_path(db), now.date()), {"seoul:9", "title:봄기획전"})
            search.side_effect = outing.OutingUnavailable("no-datagokr-key")
            failed = {"day": date(2031, 3, 7), "failures": {}}
            outing_evening.step(db, settings, failed, now)
            self.assertEqual(failed["failures"], {"outing": "no-datagokr-key"})
            self.assertIn("읽지 못했습니다", getattr(evening_html, "_outing")(failed))


if __name__ == "__main__":
    unittest.main()
