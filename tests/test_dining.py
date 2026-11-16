"""Restaurant picks (FR-55): page parsing, the food rules, ranking with bad ratings and Google's word, the visit log,
tomorrow's places in the evening mail and the counsel food store."""
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from smith import (agenda, aside, counsel, daily_qa, dining, dining_command, dining_evening, dining_log, dining_view,
                   evening_html, google_places, naver_place)
from smith.naver_place import Detail, Menu, Place, Stars

EXE = Path("aside.exe")
FISH = dining.Rules.from_constraints(["생선 알레르기"])
NONE = dining.Rules.from_constraints([])
NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
PASTA = (Menu("크림 파스타", 18000, True), Menu("토마토 파스타", 16000, False))


def lines(*records: dict) -> str:
    return "\n".join(["noise from the page", *(naver_place.MARK + json.dumps(r, ensure_ascii=False) for r in records)])


def runner_for(out: str, status: int = 0):
    calls = []

    def run(args: list[str], timeout: float) -> tuple[int, str]:
        calls.append(args)
        return status, out
    return run, calls


def place(pid: str, name: str, category: str = "양식", reviews: int | None = 500, **extra) -> Place:
    fields = dict(id=pid, name=name, category=category, area="서울 성동구 가상동", visitor_reviews=reviews, rating=None,
                  blog_reviews=10, saves=None, status="영업 중", michelin=None, broadcasts=0, booking=False, options=(),
                  blurb=None, distance=None)
    fields.update(extra)
    return Place(**fields)


def detail(pid: str, tasty: int, voters: int, menus: tuple[Menu, ...] = PASTA, stars: Stars | None = None,
           hidden: bool = False) -> Detail:
    return Detail(id=pid, menus=menus, keywords=((dining.TASTE, tasty), ("친절해요", voters // 3)),
                  voting_reviews=voters, visitor_reviews=voters, conveniences=(), stars=stars, stars_hidden=hidden)


def stars(low: int, high: int) -> Stars:
    return Stars(average=Decimal("4.5"), buckets=((Decimal("1.0"), low), (Decimal("2.0"), 0), (Decimal("5.0"), high)))


class PageParsingTest(unittest.TestCase):
    def test_list_records_are_checked_and_bounded(self) -> None:
        run, calls = runner_for(lines(
            {"kind": "list", "total": "1,234"},
            {"kind": "place", "id": "1000001", "name": "가나 파스타", "category": "양식", "visitor_reviews": "1,025",
             "rating": "4.89", "options": "예약,주차", "booking": True, "broadcasts": 2},
            {"kind": "place", "id": "not-an-id", "name": "버려질 곳"},
            {"kind": "place", "id": "1000002", "name": "다라 식당", "rating": "9.5", "visitor_reviews": "많음"}))
        places, total = naver_place.search("가상동 파스타", runner=run, executable=EXE)
        self.assertEqual(total, 1234)
        self.assertEqual([p.id for p in places], ["1000001", "1000002"])
        self.assertEqual((places[0].visitor_reviews, places[0].rating, places[0].options), (1025, Decimal("4.89"), ("예약", "주차")))
        self.assertEqual((places[1].rating, places[1].visitor_reviews), (None, None))   # Out of range / not a count.
        self.assertIn("%EA%B0%80%EC%83%81%EB%8F%99", calls[0][2])                        # The query travels URL-encoded.

    def test_detail_records_stars_and_missing_pages(self) -> None:
        run, _ = runner_for(lines(
            {"kind": "detail", "id": "1000001", "voting_reviews": 300, "visitor_reviews": "320",
             "menus": [{"name": "명란 크림 파스타", "price": "17,900원", "signature": True}, {"name": "샐러드", "price": "변동"}],
             "keywords": [[dining.TASTE, 250], ["bad"], ["친절해요", "90"]],
             "stars_shown": True, "star_average": 4.6, "star_buckets": [[0.5, 1], [1.0, 2], [2.5, 3], [5.0, 40]]},
            {"kind": "detail", "id": "1000003", "stars_shown": False, "star_buckets": [[1.0, 9]]},
            {"kind": "detail", "id": "1000004", "stars_shown": True, "star_buckets": [[1.0, 2], [1.3, 1]]},
            {"kind": "miss", "id": "1000002", "code": "no-data"}))
        read, missing = naver_place.details(["1000001", "1000002", "1000003", "1000004", "../etc"], runner=run, executable=EXE)
        self.assertEqual(missing, ["1000002"])
        got = read["1000001"]
        self.assertEqual(got.menus, (Menu("명란 크림 파스타", 17900, True), Menu("샐러드", None, False)))
        self.assertEqual(got.keywords, ((dining.TASTE, 250), ("친절해요", 90)))
        self.assertEqual((got.stars.average, got.stars.rated, got.stars.at_most(Decimal("2"))), (Decimal("4.6"), 46, 3))
        self.assertEqual((read["1000003"].stars, read["1000003"].stars_hidden), (None, True))    # The owner hides them.
        self.assertEqual((read["1000004"].stars, read["1000004"].stars_hidden), (None, False))   # Not a half-star score.

    def test_script_errors_become_codes(self) -> None:
        for out, status, code in ((lines({"kind": "error", "code": "signed-in"}), 0, "signed-in"),
                                  ("nothing marked", -1, "aside-timeout"), ("", 1, "aside-failed")):
            with self.subTest(code=code):
                run, _ = runner_for(out, status)
                with self.assertRaises(aside.AsideError) as caught:
                    naver_place.search("가상동", runner=run, executable=EXE)
                self.assertEqual(caught.exception.code, code)

    def test_details_reads_at_most_the_cap_with_a_pause_before_each_page(self) -> None:
        run, calls = runner_for(lines({"kind": "detail", "id": "1000001"}))
        naver_place.details([str(1000001 + i) for i in range(9)], runner=run, executable=EXE)
        sent = json.loads(calls[0][2].split("const ARGS = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual((len(sent["ids"]), sent["url"], sent["pause"]), (naver_place.MAX_DETAILS, "about:blank", 1500))

    def test_malformed_page_fields_become_missing_not_crashes(self) -> None:
        run, _ = runner_for(lines(
            {"kind": "place", "id": "1000001", "name": "가나", "rating": "NaN", "visitor_reviews": ",", "options": 7},
            {"kind": "place", "id": "1000002", "name": "다라", "rating": True, "blog_reviews": -3}))
        places, _ = naver_place.search("가상동", runner=run, executable=EXE)
        self.assertEqual([(p.rating, p.visitor_reviews, p.blog_reviews, p.options) for p in places],
                         [(None, None, None, ()), (None, None, None, ())])
        run, _ = runner_for(lines(
            {"kind": "detail", "id": "1000001", "menus": {"name": "x"}, "keywords": "x", "conveniences": None,
             "voting_reviews": "1,0,", "visitor_reviews": {}},
            {"kind": "detail", "id": "1000002", "menus": [{"name": "국수", "price": ",원"}]}))
        read, _ = naver_place.details(["1000001", "1000002"], runner=run, executable=EXE)
        first, second = read["1000001"], read["1000002"]
        self.assertEqual((first.menus, first.keywords, first.visitor_reviews), ((), (), None))
        self.assertEqual((first.menus_complete, first.keywords_complete), (False, False))   # Not lists: unread.
        self.assertEqual((second.menus, second.menus_complete), ((Menu("국수", None, False),), True))
        run, _ = runner_for(lines({"kind": "detail", "id": "1000001", "voting_reviews": 100,
                                   "menus": [{"name": None, "signature": True}, {"name": "샐러드"}],
                                   "keywords": [[dining.TASTE, "unavailable"], ["친절해요", 30]]}))
        partial = naver_place.details(["1000001"], runner=run, executable=EXE)[0]["1000001"]
        self.assertEqual((partial.menus_complete, partial.keywords_complete), (False, False))


class RulesTest(unittest.TestCase):
    def test_fish_dishes(self) -> None:
        for name, flagged in (("연어 크림 파스타", True), ("광어회", True), ("창란젓 들기름 파스타", True), ("어묵탕", True),
                              ("가쓰오 우동", True), ("회냉면", True), ("활어회정식", True), ("모둠회 한상", True),
                              ("사케동", True), ("타코야키", True), ("육회비빔밥", False), ("한우 육사시미", False),
                              ("까르보나라", False), ("회오리 감자", False), ("생맥주 2회 리필", False)):
            with self.subTest(dish=name):
                self.assertIs(FISH.dish(name), flagged)

    def test_fish_kitchens_drop_but_mixed_japanese_kitchens_are_read(self) -> None:
        for category, name, only, leaning in (("생선회", "가나상회", True, False), ("참치회", "다라", True, False),
                                              ("해물,생선요리", "사아", True, True), ("초밥,롤", "마바", False, True),
                                              ("일식당", "카타 라멘", False, True), ("돈가스", "하파", False, False),
                                              ("육류,고기요리", "자차 육회", False, False)):
            with self.subTest(kitchen=name):
                shop = place("1000001", name, category)
                self.assertEqual((FISH.fish_only(shop), FISH.fish_leaning(shop)), (only, leaning))
        for menu, leaning, edible in ((Menu("로스카츠", 15000, True), True, True), (Menu("런치 오마카세", 60000, True), True, False),
                                      (Menu("모둠초밥", 25000, False), True, False), (Menu("크림 파스타", 18000, True), False, True)):
            with self.subTest(menu=menu.name):
                self.assertIs(FISH.edible(menu, leaning), edible)

    def test_no_constraint_flags_nothing(self) -> None:
        shop = place("1000001", "가나 스시", "초밥,롤")
        self.assertEqual((NONE.dish("연어 파스타"), NONE.fish_only(shop), NONE.fish_leaning(shop)), (False, False, False))


class RankingTest(unittest.TestCase):
    def test_shortlist_order_floor_dedupe_and_limit(self) -> None:
        places = [place("1000001", "가나 횟집", "생선회"), place("1000002", "다라 식당", reviews=5),
                  place("1000003", "마바 스시", "초밥,롤"), place("1000003", "마바 스시", "초밥,롤"),
                  place("1000004", "사아 비스트로"), place("1000005", "자차 키친")]
        ruled: dict[str, list[str]] = {}
        chosen = dining.shortlist(places, FISH, 2, ruled)
        self.assertEqual([p.id for p in chosen], ["1000003", "1000004"])
        self.assertEqual(ruled, {"생선 요리 전문점": ["가나 횟집"], f"방문자 리뷰 {dining.MIN_REVIEWS}개 미만": ["다라 식당"]})

    def test_sushi_and_tonkatsu_place_stays_a_sushi_only_place_drops(self) -> None:
        mixed = (Menu("모둠초밥", 25000, True), Menu("로스카츠", 15000, True), Menu("히레카츠", 16000, False),
                 Menu("생맥주", 6000, False))
        sushi = (Menu("모둠초밥", 25000, True), Menu("런치 코스", 50000, True), Menu("치킨 가라아게", 9000, False))
        ruled: dict[str, list[str]] = {}
        picks = dining.rank([place("1000001", "가나 스시", "초밥,롤"), place("1000002", "다라 스시", "초밥,롤")],
                            {"1000001": detail("1000001", 80, 100, mixed), "1000002": detail("1000002", 90, 100, sushi)},
                            FISH, ruled)
        self.assertEqual([p.place.id for p in picks], ["1000001"])
        self.assertEqual(([m.name for m in picks[0].signature], picks[0].avoid, picks[0].broth),
                         (["로스카츠"], ("모둠초밥",), True))
        self.assertEqual(ruled, {f"드실 수 있는 메뉴가 {dining.MIN_EDIBLE}개 미만": ["다라 스시"]})
        extras = (Menu("모둠초밥", 25000, True), Menu("새우 추가", 3000, False), Menu("소고기 추가", 4000, False))
        ruled = {}
        self.assertEqual(dining.rank([place("1000003", "마바 스시", "초밥,롤")], {"1000003": detail("1000003", 90, 100, extras)},
                                     FISH, ruled), [])   # Extras never stand in for mains (review 2026-11-16, P2).
        self.assertEqual(list(ruled), [f"드실 수 있는 메뉴가 {dining.MIN_EDIBLE}개 미만"])

    def test_few_enthusiastic_votes_do_not_beat_many(self) -> None:
        chosen = [place("1000001", "가나 키친", reviews=12), place("1000002", "다라 키친", reviews=10000)]
        picks = dining.rank(chosen, {"1000001": detail("1000001", 12, 12), "1000002": detail("1000002", 9000, 10000)},
                            NONE, {})
        self.assertEqual([p.place.id for p in picks], ["1000002", "1000001"])
        self.assertEqual(picks[1].taste_share, 1.0)

    def test_many_bad_ratings_rank_below_an_even_place_and_read_as_split(self) -> None:
        chosen = [place("1000001", "가나"), place("1000002", "다라"), place("1000003", "마바")]
        details = {"1000001": detail("1000001", 93, 100, stars=stars(30, 170)),   # Loved by most, 15% one star.
                   "1000002": detail("1000002", 88, 100, stars=stars(2, 198)),
                   "1000003": detail("1000003", 88, 100, hidden=True)}
        picks = dining.rank(chosen, details, NONE, {})
        self.assertEqual([p.place.id for p in picks], ["1000002", "1000003", "1000001"])
        self.assertEqual([(p.rated, p.low, p.split) for p in picks], [(200, 2, False), (0, 0, False), (200, 30, True)])
        self.assertAlmostEqual(dining.score(0, 0, 0, 0), dining.PRIOR * (1 - dining.LOW_PENALTY * dining.LOW_PRIOR))

    def test_no_votes_or_no_menu_is_not_recommended_on_a_guess(self) -> None:
        bare = Detail(id="1000001", menus=(), keywords=(), voting_reviews=None, visitor_reviews=None, conveniences=())
        no_menu = detail("1000002", 90, 100, menus=())
        ruled: dict[str, list[str]] = {}
        picks = dining.rank([place("1000001", "가나"), place("1000002", "다라"), place("1000003", "마바")],
                            {"1000001": bare, "1000002": no_menu, "1000003": detail("1000003", 50, 100)}, FISH, ruled)
        self.assertEqual([p.place.id for p in picks], ["1000003"])
        self.assertEqual(ruled, {"리뷰 키워드 투표를 읽지 못함": ["가나"], "메뉴를 읽지 못해 못 드시는 음식 확인 불가": ["다라"]})
        self.assertEqual([p.place.id for p in dining.rank([place("1000002", "다라")], {"1000002": no_menu}, NONE, {})],
                         ["1000002"])
        good = detail("1000004", 90, 100)
        ruled = {}
        for item in (replace(good, menus_complete=False), replace(good, keywords_complete=False)):
            self.assertEqual(dining.rank([place("1000004", "사아")], {"1000004": item}, FISH, ruled), [])
        self.assertEqual(ruled, {"메뉴를 읽지 못해 못 드시는 음식 확인 불가": ["사아"], "리뷰 키워드 투표를 읽지 못함": ["사아"]})

    def test_a_bad_visit_drops_the_place_and_a_good_one_is_shown(self) -> None:
        visits = [dining_log.Visit(name="가나 키친", on=date(2026, 11, 1), rating=2),
                  dining_log.Visit(name="다라", on=date(2026, 11, 2), rating=4, place_id="1000002", note="크림 파스타 좋음")]
        ruled: dict[str, list[str]] = {}
        picks = dining.rank([place("1000001", "가나 키친 성수점"), place("1000002", "다라 성수")],
                            {"1000001": detail("1000001", 90, 100), "1000002": detail("1000002", 80, 100)}, NONE, ruled, visits)
        self.assertEqual(([p.place.id for p in picks], picks[0].visit.rating), (["1000002"], 4))
        self.assertEqual(ruled, {"지난번 별로였던 곳": ["가나 키친 성수점"]})

    def test_google_lower_demotes_and_a_failed_call_stops_the_checks(self) -> None:
        picks = dining.rank([place("1000001", "가나", rating=Decimal("4.8")), place("1000002", "다라", rating=Decimal("4.8"))],
                            {"1000001": detail("1000001", 90, 100), "1000002": detail("1000002", 88, 100)}, NONE, {})
        low = dining.Cross("구글", Decimal("4.1"), 250, None)
        fine = dining.Cross("구글", Decimal("4.6"), 250, None)
        checked, note = dining.cross_check(picks, 3, lambda p: low if p.id == "1000001" else fine)
        self.assertEqual(([p.place.id for p in checked], note), (["1000002", "1000001"], None))
        self.assertTrue(checked[1].cross.lower_than(checked[1].stars_average))
        calls = []

        def failing(p: Place) -> dining.Cross | None:
            calls.append(p.id)
            raise dining.CrossFailed("google-quota")
        checked, note = dining.cross_check(picks, 3, failing)
        self.assertEqual((calls, note, [p.cross for p in checked]), (["1000001"], "google-quota", [None, None]))

    def test_recommend_reports_unread_pages(self) -> None:
        places = [place("1000001", "가나 키친"), place("1000002", "다라 키친")]
        outcome = dining.recommend("가상동 파스타", FISH, search=lambda q: (places, 2),
                                   read=lambda ids: ({"1000001": detail("1000001", 50, 60)}, ["1000002"]))
        self.assertEqual(([p.place.id for p in outcome.picks], outcome.unread), (["1000001"], ["다라 키친"]))


class GoogleAndLogTest(unittest.TestCase):
    def test_lookup_matches_name_and_district(self) -> None:
        sent = []

        def poster(url, body, headers, timeout):
            sent.append((json.loads(body), headers))
            return {"places": [{"displayName": {"text": "가나 키친"}, "formattedAddress": "대한민국 서울특별시 강남구", "rating": 4.9},
                               {"displayName": {"text": "가나 키친 성수점"}, "formattedAddress": "대한민국 서울특별시 성동구 가상동",
                                "rating": 4.3, "userRatingCount": 812, "googleMapsUri": "https://maps.google.com/?cid=1"}]}
        found = google_places.lookup("가나 키친", "서울 성동구 가상동", key="k", poster=poster)
        self.assertEqual((found.rating, found.count, found.url), (Decimal("4.3"), 812, "https://maps.google.com/?cid=1"))
        self.assertEqual((sent[0][0]["textQuery"], sent[0][1]["X-Goog-FieldMask"]), ("가나 키친 서울 성동구 가상동", google_places.FIELD_MASK))
        self.assertIsNone(google_places.lookup("다라", "서울 성동구 가상동", key="k", poster=poster))
        self.assertIsNone(google_places.lookup("가나 키친", None, key="k", poster=poster))   # No district: no call.

    def test_google_branch_spelled_differently_matches_in_the_same_dong_only(self) -> None:
        """"다라마 서울숲 본점" on Naver is "다라마 성수" on Google (live 2026-11-16); a longer name or another 동 is not."""
        def answer(name: str, address: str):  # noqa: ANN202 - a fake poster
            return lambda url, body, headers, timeout: {"places": [
                {"displayName": {"text": name}, "formattedAddress": address, "rating": 4.2, "userRatingCount": 31}]}
        area = "서울 성동구 가상동1가"
        found = google_places.lookup("다라마 서울숲 본점", area, key="k", poster=answer("다라마 성수", "서울특별시 성동구 가상동1가 1-1"))
        self.assertEqual((found.rating, found.count), (Decimal("4.2"), 31))
        self.assertIsNone(google_places.lookup("다라마 서울숲 본점", area, key="k",
                                               poster=answer("다라마 성수", "서울특별시 성동구 가상동2가 1-1")))
        self.assertIsNone(google_places.lookup("돈카츠 가상점", area, key="k",
                                               poster=answer("돈카츠 히로 가상", "서울특별시 성동구 가상동1가 1-1")))

    def test_cross_is_off_without_a_key_and_counts_calls_up_to_the_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            where = Path(tmp)
            self.assertEqual(dining_command.google_cross(where, NOW, key_loader=lambda: None), (None, "google-no-key"))
            check, note = dining_command.google_cross(where, NOW, key_loader=lambda: "k",
                                                      lookup=lambda name, area, key: google_places.Rating(name, Decimal("4.5"), 40, None))
            self.assertEqual((check(place("1000001", "가나")).rating, note), (Decimal("4.5"), None))
            self.assertEqual(dining_log.google_used(where, "2026-11"), 1)
            (where / dining_log.USAGE_FILE).write_text(json.dumps({"2026-11": google_places.MONTHLY_CAP}), encoding="utf-8")
            self.assertEqual(dining_command.google_cross(where, NOW, key_loader=lambda: "k")[1], "google-cap")
            with self.assertRaises(dining.CrossFailed):
                check(place("1000001", "가나"))

    def test_visit_log_round_trip_and_broken_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            where = Path(tmp)
            dining_log.add_visit(where, dining_log.Visit(name="가나 키친", on=date(2026, 11, 1), rating=5, note="파스타"))
            dining_log.add_visit(where, dining_log.Visit(name="가나 키친", on=date(2026, 11, 9), rating=3))
            visits = dining_log.load_visits(where)
            self.assertEqual(dining_log.latest_visit(visits, place("1000001", "가나 키친 본점")).rating, 3)
            # Two runs adding at once both keep their visit (review 2026-11-16, round 5).
            threads = [threading.Thread(target=lambda n=n: [dining_log.add_visit(where, dining_log.Visit(
                name=f"가게{n}-{i}", on=date(2026, 11, 10), rating=2)) for i in range(3)]) for n in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(dining_log.load_visits(where)), 8)
            (where / dining_log.VISITS_FILE).write_text("[{\"name\": \"x\"}]", encoding="utf-8")
            with self.assertRaises(dining_log.DiningLogError):
                dining_log.load_visits(where)


class EveningTest(unittest.TestCase):
    def test_areas_in_titles(self) -> None:
        for text, area in (("광화문 보라쇼", "광화문"), ("에버랜드", "에버랜드"), ("성수동 브런치", "성수"), ("강남역 미팅", "강남역"),
                           ("DDP에서 전시", "DDP"), ("신사업 회의", None), ("미사 참석", None), ("치과", None)):
            with self.subTest(text=text):
                self.assertEqual(dining_evening.area_in(text), area)

    def test_outings_from_events_and_tasks(self) -> None:
        def event(title: str, location: str | None = None, hour: int | None = 19) -> agenda.Event:
            start = datetime(2026, 11, 17, hour or 0)
            return agenda.Event(id="e", title=title, start=start, end=start, all_day=hour is None, location=location,
                                description="", event_type="default", busy=True)
        events = [event("상담", "가나다 부부상담 라마센터"), event("에버랜드", hour=None), event("판교 출근", hour=9),
                  event("뮤지컬", "샤롯데씨어터"), event("치과")]
        task = type("Task", (), {"title": "광화문 보라쇼 예매 확인"})()
        found = dining_evening.outings(events, [task], skip=("판교",))
        self.assertEqual([(o.area, o.when) for o in found], [("에버랜드", "종일"), ("샤롯데씨어터", "19:00")])
        # A location's own words never become a query (review 2026-11-16, P0): only listed areas leave.
        self.assertEqual([o.area for o in dining_evening.outings(events[:1], [task])], ["광화문"])

    def test_step_reads_once_reuses_and_names_a_failure(self) -> None:
        result = {"query": "광화문 맛집", "picks": [{"name": "가나"}], "ruled_out": {}, "unread": [], "rules_known": True}
        calls = []

        def searcher(db: Path, query: str) -> dict:
            calls.append(query)
            return result
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "smith.db"
            first = dining_evening.cached_search(db, "광화문", now=NOW, searcher=searcher)
            again = dining_evening.cached_search(db, "광화문", now=NOW, searcher=searcher)
            self.assertEqual((calls, first[1], again), (["광화문 맛집"], True, (result, False)))
            # A new verdict changes the household's fingerprint: the stored picks are not reused (review P1).
            dining_log.add_visit(dining_log.folder(db), dining_log.Visit(name="가나", on=date(2026, 11, 16), rating=1))
            self.assertTrue(dining_evening.cached_search(db, "광화문", now=NOW, searcher=searcher)[1])
            self.assertFalse((Path(tmp) / "smith.db").exists())   # The shared ledger is not touched (review P2).

            def broken(db: Path, query: str) -> dict:
                raise aside.AsideError("aside-failed")
            start = datetime(2026, 11, 17, 19)
            data = {"events": [agenda.Event(id="e", title="뮤지컬", start=start, end=start, all_day=False, location="블루스퀘어",
                                            description="", event_type="default", busy=True)],
                    "tasks": {"due_today": []}, "failures": {}}
            dining_evening.step(db, {}, data, NOW, searcher=broken)
            self.assertEqual((data["dining"][0]["error"], data["failures"]), ("aside-failed", {"dining": "aside-failed"}))
            self.assertIn("네이버를 읽지 못했습니다", evening_html._dining(data))

            def unwritable(db: Path, query: str) -> dict:
                raise PermissionError("locked")
            data["failures"] = {}
            dining_evening.step(db, {"dining": {"skip_areas": []}}, data, NOW, searcher=unwritable)
            self.assertEqual(data["failures"], {"dining": "dining-PermissionError"})   # The mail goes on (review P1).


class RenderTest(unittest.TestCase):
    def pick(self) -> dict:
        shop = place("1000002", "다라 키친", "초밥,롤", rating=Decimal("4.8"), booking=True)
        menus = (Menu("로스카츠", 15000, True), Menu("히레카츠", 16000, False), Menu("모둠초밥", 25000, True))
        picks = dining.rank([shop], {"1000002": detail("1000002", 90, 100, menus, stars=stars(30, 170))}, FISH, {})
        return dining_view.pick_dict(replace(picks[0], cross=dining.Cross("구글", Decimal("3.9"), 120, None)))

    def test_console_text(self) -> None:
        result = {"query": "가상동 일식", "total": 653, "listed": 50, "ruled_out": {"생선 요리 전문점": ["가나 횟집"]},
                  "unread": [], "rules_known": True, "cross_note": None, "picks": [self.pick()]}
        text = dining_command.render(result, NOW)
        for expected in ("653곳 중 앞쪽 50곳", "생선 요리 전문점 1곳(가나 횟집)", "1. 다라 키친", "\"음식이 맛있어요\" 90% (키워드를 고른 리뷰 100건)",
                         "1~2점 15% (200건 중 30건)", "구글 3.9 (리뷰 120건)", "주의: 평이 갈리는 편", "주의: 구글 평점이 네이버보다",
                         "드실 메뉴: 로스카츠 15,000원", "본인은 피할 메뉴: 모둠초밥", "네이버 예약 가능", naver_place.link("1000002"),
                         dining_view.BROTH_NOTE, dining_view.FISH_NOTE):
            self.assertIn(expected, text)
        self.assertIn(dining_view.NO_RULES_NOTE, dining_command.render({**result, "rules_known": False}, NOW))

    def test_counsel_food_query_and_list_under_the_letter(self) -> None:
        result = {"query": "성수 파스타", "picks": [self.pick()], "ruled_out": {"생선 요리 전문점": ["가나"]}, "unread": [],
                  "rules_known": True}
        seen = []
        ledger = mock.Mock(constraints=[mock.Mock(text="생선 알레르기")])
        with mock.patch.object(dining_command, "search", side_effect=lambda db, q, now: seen.append(q) or result), \
                mock.patch.object(counsel.health, "load", return_value=ledger):
            for question in ("가나다랑 성수에서 파스타 먹을 곳?", "광화문 근처 저녁 어디서 먹지?", "오늘 저녁 뭐 먹지?"):
                for_model, full, food = counsel._food(Path("x.db"), question, now=NOW)
        # Only a listed area and food words reach Naver: never a name from the question (review 2026-11-16, P0).
        self.assertEqual(seen, ["성수 파스타", "광화문 맛집", "맛집"])
        self.assertTrue(for_model["around_this_computer"])
        self.assertNotIn("link", for_model["picks"][0])
        self.assertNotIn("area", for_model["picks"][0])   # An address-shaped field would (rightly) stop the payload.
        for sentence, kept in (("활어회정식을 추천합니다.", False), ("생선 알레르기가 있어도 연어를 추천합니다.", False),
                               ("모둠초밥을 드셔 보세요.", False), ("회부터 드셔 보세요.", False), ("연어가 좋겠습니다.", False),
                               ("연어동은 피하고 규카츠를 드세요.", False), ("규카츠를 드셔 보세요.", True),
                               ("다만 생선이 들어간 메뉴는 고르지 마세요.", True), ("좋은 기회를 드려요.", True),
                               ("회의 끝나고 가세요.", True), ("‘회’를 드셔 보세요.", False), ("**회**를 드셔 보세요.", False)):
            with self.subTest(sentence=sentence):
                text, _ = counsel.verify(sentence, allowed=set(), resolved=[], banned=set(), food=food)
                self.assertEqual(bool(text), kept)
        text = counsel._dining_text(full)
        self.assertIn("Smith가 읽은 가게", text)
        self.assertIn(naver_place.link("1000002"), text)

    def test_counsel_keeps_the_food_check_when_naver_fails(self) -> None:
        """A failed read must not take the letter check with it (review 2026-11-16, round 5)."""
        ledger = mock.Mock(constraints=[mock.Mock(text="생선 알레르기")])
        with mock.patch.object(counsel.health, "load", return_value=ledger), \
                mock.patch.object(dining_command, "search", side_effect=aside.AsideError("aside-failed")), \
                mock.patch.object(counsel.keep, "load_all", return_value=[]), \
                mock.patch.object(counsel.memory, "load", return_value=[]):
            ctx = counsel.assemble(Path("x.db"), {}, {"summary": "", "domains": ["food"], "search_terms": [],
                                                      "web": {"needed": False}, "asks_action": False},
                                   "성수 저녁 어디서 먹지?", now=NOW, tz="Asia/Seoul")
        self.assertEqual(ctx["data_status"]["food"], "unavailable: aside-failed")
        text, _ = counsel.verify("연어를 드셔 보세요.", allowed=set(), resolved=[], banned=set(), food=ctx["_food_meta"])
        self.assertEqual(text, "")
        with mock.patch.object(counsel.health, "load", side_effect=counsel.health.HealthError("broken")):
            self.assertTrue(dining.Rules.household(lambda: counsel.health.load(Path("x")).constraints).fish)

    def test_evening_reply_never_offers_a_flagged_dish(self) -> None:
        """A reply to the evening mail with picks passes the same food check (review 2026-11-16, P1)."""
        prose = {"dining": [{"area": "성수", "when": "19:00", "picks": [
            {"order": 1, "name": "다라 키친", "category": "초밥,롤", "signature": ["로스카츠"], "avoid": ["모둠초밥"], "link": "x"}]}]}
        ctx = {"today": {"mail_prose": prose}, "window": {"events": [], "tasks": []}, "upcoming_45_days": [], "news_links": {}}
        output = {"answer": {"text": "로스카츠를 드셔 보세요. 모둠초밥도 좋습니다.", "refs": []},
                  "follow_up": ["모둠초밥 가격도 볼까요?", "예약할까요?"], "remember": [],
                  "research": {"needed": "no", "subject": "", "kind": "none", "queries": [], "anchors": []}}
        ledger = mock.Mock(constraints=[mock.Mock(text="생선 알레르기")])
        with tempfile.TemporaryDirectory() as tmp, mock.patch("smith.health.load", return_value=ledger):
            result = daily_qa.answer(Path(tmp) / "smith.db", ctx, "첫 식당에서 저는 뭘 먹을까요?", now=NOW, received_at=NOW,
                                     history=[], secrets=[], tz="Asia/Seoul", executable=Path("claude"),
                                     caller=lambda *args: (output, "0.01"))
        self.assertEqual((result["text"], result["follow_up"]), ("로스카츠를 드셔 보세요.", ["예약할까요?"]))
        self.assertEqual(result["problems"], ["food: 1 sentence(s) dropped"])


if __name__ == "__main__":
    unittest.main()
