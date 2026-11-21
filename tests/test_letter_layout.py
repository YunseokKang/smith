"""What a themed letter leaves for its end and how it fits a phone (report design §46): the reference group, the short
standing card, the small holdings in one block, and no table wider than a phone."""
import json
import re
import tempfile
import unittest
import unittest.mock
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from smith import editions, fmt, korean, ledger, narrative, report_html, research
from smith.importer import parse_import
from smith.proposals import STALE_AFTER, Proposal
from smith.report_data import build_report

import fast_ledger

AT = datetime(2026, 11, 19, tzinfo=timezone.utc)


def asset(record_id, **fields):
    return {"id": record_id, "kind": "asset", "owner_id": "self", "effective_at": AT.isoformat(), "revision": 1,
            "status": "active", "currency": "KRW", **fields}


def stock(symbol, quantity, price):
    return asset(symbol.lower(), category="stock", account_type="brokerage", value=str(quantity * price),
                 valuation_method="market", liquidity="days", symbol=symbol, market="KR", quantity=str(quantity),
                 unit_price=str(price), average_cost=str(price), instrument_name=symbol)


RECORDS = [
    asset("cash", category="cash", account_type="bank", value="10000000", valuation_method="manual", liquidity="immediate"),
    stock("BIG", 900, 100_000),        # 90M
    stock("MID", 80, 100_000),         # 8M, 8% of the listed holdings
    stock("TINYA", 10, 100_000),       # 1M, 1%
    stock("TINYB", 10, 100_000),       # 1M, 1%
]


def proposal(key, priority, shown):
    return Proposal(key=key, priority=priority, title=f"{key} 제안입니다.", why="이유입니다.", effect="효과입니다.",
                    risks="위험입니다.", timing="이번 달", reconsider="조건입니다.", certainty="계산", identity=key,
                    times_shown=shown, renewed=not shown, headline=f"{key} 요약")


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        db = Path(directory.name) / "smith.db"
        doc = {"schema_version": 1, "import_id": "l1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": RECORDS}
        with closing(ledger.connect(db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
        with closing(ledger.connect_read_only(db)) as conn:
            cls.built = {kind: build_report(conn, as_of=AT, known_at=AT, baseline=None, kind=kind)
                         for kind in editions.EDITIONS}

    def test_every_themed_letter_keeps_its_standing_tables_in_a_reference_group_at_the_end(self):
        for edition in editions.EDITIONS.values():
            with self.subTest(edition=edition.key):
                self.assertTrue(edition.reference)
                self.assertFalse(set(edition.reference) & set(edition.personal + edition.household))
        # The rates letter: the year's rate table is below everything the reader acts on, under the group's title.
        page = report_html.render(self.built["rates"])[1]
        self.assertLess(page.index("우리 집 금리·대출"), page.index("매주 크게 바뀌지 않는 기준과 표입니다."))
        self.assertLess(page.index("매주 크게 바뀌지 않는 기준과 표입니다."), page.index("거시 환경"))

    def test_the_reference_group_is_whole_in_the_months_first_letter_and_one_line_in_the_others(self):
        # (the last letter on the same theme, whether this letter carries the group in full)
        cases = [(None, True), (AT - timedelta(days=7), False), (AT - timedelta(days=18), False),
                 (AT - timedelta(days=21), True), (AT - timedelta(days=400), True)]   # 10/29 and last year: an earlier month.
        for last, whole in cases:
            with self.subTest(last=last):
                page = report_html.render({**self.built["rates"], "series_last": last})[1]
                self.assertEqual("매주 크게 바뀌지 않는 기준과 표입니다." in page, whole)
                self.assertEqual("출처·관측일" in page, whole)                      # The rate table itself.
                self.assertEqual("매달 첫 금리·대출 레터에만 싣습니다. 이번 달 첫 레터에 전체가 있고" in page, not whole)
                self.assertIn("금리 표와 공식 발표", page) if not whole else None      # The line names what was left out.
        # What counts is a letter sent on the same theme: the comparison baseline of a theme's first letter is another
        # theme's letter and says nothing of this theme's tables (Codex review 2026-11-19).
        page = report_html.render({**self.built["rates"], "baseline": AT - timedelta(days=1), "series_last": None})[1]
        self.assertIn("출처·관측일", page)

    def test_the_last_letter_of_the_same_theme_is_read_from_what_was_sent(self):
        from smith.report_data import _series_last
        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)
        tz = timezone(timedelta(hours=9))
        now = datetime(2026, 11, 19, 6, 0, tzinfo=tz)

        def sent(report_id, kind, at, status="sent"):
            ledger.claim_report(conn, report_id=report_id, slot=None, kind=kind, trigger="manual", created_at=at, as_of=at,
                                baseline=None, missed_slots=[])
            if status == "sent":
                ledger.start_sending(conn, report_id=report_id, now=at, subject="s", html_sha256="x")
                ledger.finish_report(conn, report_id=report_id, status="sent", finished_at=at)
        sent("p1", "property", datetime(2026, 11, 18, 21, 0, tzinfo=timezone.utc))
        self.assertIsNone(_series_last(conn, "rates", now))                      # Another theme's letter does not count.
        sent("r1", "rates", datetime(2026, 11, 11, 21, 0, tzinfo=timezone.utc))
        sent("r2", "rates", datetime(2026, 11, 14, 21, 0, tzinfo=timezone.utc), status="claimed")   # Never sent.
        last = _series_last(conn, "rates", now)
        self.assertEqual((last.month, last.day, last.hour), (11, 12, 6))         # Household-local: 21:00 UTC is the 12th.
        self.assertIsNone(_series_last(conn, "rates", datetime(2026, 11, 11, 21, 0, tzinfo=timezone.utc)))   # Not itself.
        # The spouse has their own history: the client's letters say nothing of what the spouse received.
        self.assertIsNone(_series_last(conn, "rates", now, "partner"))
        for key, status in (("report-partner:rates:2026-11-12", "sent"), ("report-partner:rates:2026-11-13#manual-ab12cd", "failed"),
                            ("report-partner:tax:2026-11-14", "sent")):
            at = datetime.fromisoformat(key.split(":")[2][:10] + "T21:00:00+00:00") - timedelta(days=1)
            token, _ = ledger.claim_daily_run(conn, run_key=key, day=at.date(), run_id=key, trigger="manual", now=at,
                                              lease=timedelta(minutes=45), max_attempts=3, product="report-partner")
            ledger.advance_daily_run(conn, run_key=key, token=token, from_status="building", status=status, finished_at=at)
        last = _series_last(conn, "rates", now, "partner")
        self.assertEqual((last.month, last.day), (11, 12))                       # The failed one and the tax letter are not it.

    def test_the_life_letter_leads_with_what_changes_and_keeps_the_standing_cards_for_the_months_first_letter(self):
        """User review 2026-11-21 (report design §51): the household part of the Saturday letter repeated itself."""
        data = self.built["life"]
        standing = ("현금·유동성", "남는 돈의 쓰임", "목표까지의 준비", "전략 방향")
        first = report_html.render({**data, "series_last": None})[1]
        for title in standing:
            self.assertIn(title, first, title)
        self.assertLess(first.index("우리 집 생활·목표"), first.index("현금·유동성"))     # After the household's group: 참고.
        self.assertNotIn("한 달 소비의 균형", first)                                  # The hero's bar says the month's split.
        # Later in the month none of them is a card: one line names them and says what moved since the last letter.
        later = {**data, "series_last": AT - timedelta(days=7)}
        for moved, said in ((None, "물어 주십시오.</td>"), ([], "지난 레터 이후 달라진 것은 없습니다."),
                            (["비상금 확보율 60% → 72%"], "지난 레터 이후 달라진 것: 비상금 확보율 60% → 72%.")):
            with self.subTest(moved=moved):
                page = report_html.render({**later, "reference_changes": moved})[1]
                self.assertIn(said, page)
                self.assertIn("매주 크게 바뀌지 않는 표(현금·유동성, 남는 돈의 쓰임, 목표까지의 준비", page)
                self.assertNotIn("비상금 기준 대비 즉시 가용 자금", page)           # The reserve card's gauge.
                self.assertNotIn("목표까지 지금 궤도에 있습니까", page)             # The long-range band.
        # A request the judgement cannot do without is never left for the month's first letter.
        from smith.checklist import Request
        must = Request("must-have", "must", "꼭 필요한 자료", "판단할 수 없습니다.", "답장으로 알려 주십시오.")
        page = report_html.render({**later, "requests": [must]})[1]
        self.assertIn("꼭 필요한 자료", page)

    def test_proposals_left_undecided_shrink_to_one_line_and_open_again_when_they_press(self):
        """§51: an undecided proposal was a full card every Saturday; a deadline or a new proposal is still a card."""
        data = self.built["life"]
        waiting = [proposal("emergency-reserve", 2, STALE_AFTER), proposal("surplus-plan", 2, STALE_AFTER + 3)]
        fresh = proposal("spending-room", 1, 0)
        page = report_html.render({**data, "advice": {**data["advice"], "proposals": [fresh, *waiting]}})[1]
        block = page[page.index("결정 대기 2건"):]
        self.assertIn(f"2번 emergency-reserve 요약({STALE_AFTER}번 안내)", block)
        self.assertIn(f"3번 surplus-plan 요약({STALE_AFTER + 3}번 안내)", block)
        self.assertIn("「2번 할게」", block)
        self.assertIn("spending-room 제안입니다.", page)                                # The new one keeps its card ...
        self.assertNotIn("emergency-reserve 제안입니다.", page)                          # ... the waiting ones do not.
        self.assertIn("<details open", page[page.index("spending-room 제안입니다.") - 900:page.index("spending-room 제안입니다.")])
        # The same proposals on a day that does not ask for it stay cards (the rule is the life day's).
        tax = self.built["tax"]
        credit = proposal("pension-credit", 2, STALE_AFTER)
        self.assertNotIn("결정 대기", report_html.render({**tax, "advice": {**tax["advice"], "proposals": [credit]}})[1])

    def test_the_plan_is_set_beside_what_the_card_statement_shows(self):
        month = {"inflow": Decimal(9_000_000), "discretionary": Decimal(3_560_000)}
        latest = {"label": "10/9~11/8", "spent": 2_580_000, "fixed": 230_000}
        line = report_html._plan_vs_card({"own_month": month, "own_spending": {"latest": latest}})
        for said in ("계획과 실제", "재량 지출은 월 <b>356만 원</b>", "실제 소비는 <b>235만 원</b>", "계획보다 121만 원 적습니다",
                     "카드 한 장만 본 것"):
            self.assertIn(said, line, said)
        over = report_html._plan_vs_card({"own_month": month, "own_spending": {"latest": {**latest, "spent": 4_000_000}}})
        self.assertIn("계획보다 21만 원 많습니다", over)
        # No statement, or no planned figure: nothing is said (unknown is not zero).
        self.assertEqual(report_html._plan_vs_card({"own_month": month, "own_spending": None}), "")
        self.assertEqual(report_html._plan_vs_card({"own_month": None, "own_spending": {"latest": latest}}), "")

    def test_what_moved_since_the_last_letter_is_read_from_the_same_build_at_that_time(self):
        from smith import report_data
        before = {"kind": "life", "as_of": AT, "sectors": {"cash": {"reserve_target": Decimal(100), "immediate": Decimal(60)}},
                  "advice": {"spending_room": None, "strategy": []}, "kpis": {"home_progress": None}, "requests": [1, 2]}
        after = {**before, "sectors": {"cash": {"reserve_target": Decimal(100), "immediate": Decimal(72)}}, "requests": [1]}
        self.assertEqual(report_data._life_digest(before)["비상금 확보율"], "60%")
        with closing(ledger.connect(":memory:")) as conn, unittest.mock.patch.object(report_data, "_build_report",
                                                                                     return_value=before) as built:
            def moved(data, last):
                return report_data._reference_changes(conn, {**data, "series_last": last}, household=None, schedule=None,
                                                      audience="self", client_household=None)
            self.assertEqual(moved(after, AT - timedelta(days=7)), ["비상금 확보율 60% → 72%", "확인할 자료 2건 → 1건"])
            self.assertEqual(moved(before, AT - timedelta(days=7)), [])
            # A figure that can no longer be computed moved too: never "nothing changed" (Codex review 2026-11-21).
            lost = {**before, "sectors": {"cash": {"reserve_target": Decimal(100), "immediate": None}}}
            self.assertEqual(moved(lost, AT - timedelta(days=7)), ["비상금 확보율 60% → 계산 불가"])
            self.assertTrue(all(call.kwargs["nested"] for call in built.call_args_list))   # It never looks further back.
            # The month's first letter prints the cards in full and a theme without a digest claims nothing.
            self.assertIsNone(moved(after, None))
            self.assertIsNone(moved(after, AT - timedelta(days=40)))
            self.assertIsNone(moved({**after, "kind": "rates"}, AT - timedelta(days=7)))

    def test_a_standing_card_delivered_for_two_weeks_says_only_why_and_what_would_change_it(self):
        for shown, short in ((STALE_AFTER - 1, False), (STALE_AFTER, True)):
            with self.subTest(shown=shown):
                data = dict(self.built["rates"])
                data["advice"] = {**data["advice"], "proposals": [proposal("variable-rate", 3, shown)], "undetermined": []}
                page = report_html.render(data)[1]
                self.assertEqual("왜 유지" in page, short)
                self.assertEqual("예상 효과" in page, not short)
                self.assertIn("다시 판단할 조건", page)
        # An action still open is never cut short, however often it was delivered.
        data = dict(self.built["rates"])
        data["advice"] = {**data["advice"], "proposals": [proposal("prepayment", 2, 1)], "undetermined": []}
        self.assertIn("예상 효과", report_html.render(data)[1])

    def test_small_holdings_share_one_block_and_the_heavy_ones_keep_their_cards(self):
        data = self.built["markets"]
        positions = {p["name"]: p for p in data["holdings"]["positions"]}
        opinion = {"stance": "exit", "reason": "첫 문장입니다. 둘째 문장입니다.", "alternative": "지수펀드로 옮깁니다.",
                   "condition": "", "reconsider": "실적이 돌아서면 다시 봅니다.", "links": [], "by_code": False}
        story = {"holdings": {positions[name]["key"]: opinion for name in ("MID", "TINYA", "TINYB")}}
        page = report_html._holdings(data, story, True, quantities=False)
        self.assertIn("소액 종목 2개", page)
        self.assertIn("합계 200만 원 · 비중 2.0%", page)
        self.assertEqual(page.count("지수펀드로 옮깁니다."), 1)        # Only MID (8%) has a whole card.
        self.assertEqual(page.count("둘째 문장입니다."), 1)            # A small holding keeps the reason's first sentence.
        self.assertIn("답장으로 물어 주시면", page)
        # The spouse's replies are not answered: that letter does not offer the rest by reply.
        self.assertNotIn("답장으로", report_html._holdings({**data, "audience": "partner"}, story, True, quantities=False))
        # One small holding alone keeps its card, and so does the one Smith must opine on, whatever its share.
        tiny, mid = positions["TINYA"], positions["MID"]
        cases = [([tiny, positions["TINYB"]], 2), ([tiny, mid], 0), ([tiny], 0),
                 ([tiny, {**positions["TINYB"], "must_opine": True}], 0), ([tiny, {**tiny, "share": None}], 0)]
        for active, expected in cases:
            with self.subTest(names=[p["name"] for p in active]):
                self.assertEqual(len(report_html._small_holdings(active, story["holdings"])), expected)
        # An opinion with a precondition keeps its card: one line would turn a conditional "add" into a plain one.
        bound = {**opinion, "stance": "add", "condition": "비상금 3개월분을 먼저 채운 뒤에 삽니다."}
        conditional = {"holdings": {**story["holdings"], positions["TINYB"]["key"]: bound}}
        page = report_html._holdings(data, conditional, True, quantities=False)
        self.assertNotIn("소액 종목", page)                       # One small holding left: it keeps its card too.
        self.assertIn("비상금 3개월분을 먼저 채운 뒤에 삽니다.", page)

    def test_no_table_in_a_themed_letter_is_wider_than_a_phone(self):
        # Four columns of short cells is what a 375px screen holds (the markets, property and tax letters each had a
        # five- or six-column table that widened the whole letter).
        for kind, data in self.built.items():
            with self.subTest(kind=kind):
                page = report_html.render(data)[1]
                widest = max((row.count("<th") for row in re.findall(r"<tr>(?:<th.*?</th>)+</tr>", page, flags=re.S)), default=0)
                self.assertLessEqual(widest, 4)
        tiers = [{"retire_age": age, "years_without_pension": 65 - age, "first_income_age": 65, "total_net": Decimal("3000000"),
                  "ratio": Decimal("0.6"), "later_net": Decimal("2000000"), "later_age": 80, "unknown": False,
                  "outside_need": {"upper_bound": False, "total": Decimal("500000000")}} for age in (50, 55, 60)]
        cards = report_html._tier_table(tiers)
        self.assertNotIn("<th", cards)
        for text in ("50세 은퇴", "준비율 60%", "15년(65세까지)", "기간 연금 끝난 뒤 세후 월", "200만 원(80세~)", "5.0억 원"):
            self.assertIn(text, cards, text)


    def test_a_holdings_share_reads_the_same_wherever_it_is_printed_and_names_whose_holdings_it_is_of(self):
        data = self.built["markets"]
        trim = next(p for p in data["advice"]["proposals"] if p.key == "concentration-trim")
        self.assertIn("BIG 비중이 고객님 명의 주식의 90.0%로", trim.title)
        self.assertIn("고객님 명의 주식의 90.0%입니다.", trim.why)
        page = report_html.render(data)[1]
        self.assertEqual(report_html._tiles(data, editions.EDITIONS["markets"])[3][:2], ("가장 큰 종목 비중", "90.0%"))
        self.assertIn('BIG <span style="font-family:' + report_html.MONO + '">90.0%</span>', page)      # The bar's legend.
        self.assertNotIn(">90%<", page)

    def test_a_single_item_is_named_with_its_amount_instead_of_a_full_bar(self):
        one = report_html._stacked([("주택담보대출", Decimal("230000000"), ""), ("신용대출", None, "")])
        self.assertIn("주택담보대출", one)
        self.assertIn("2.3억 원", one)
        self.assertNotIn("100%", one)
        self.assertNotIn("<table", one)
        two = report_html._stacked([("가", Decimal(395), ""), ("나", Decimal(605), "")], places=1)
        self.assertIn("39.5%", two)
        self.assertIn("<table", two)


class WordingTests(unittest.TestCase):
    def test_a_particle_follows_the_last_sound_of_the_word_before_it(self):
        cases = [("삼성전자", "은는", "삼성전자는"), ("셀트리온", "은는", "셀트리온은"), ("150만 원", "로", "150만 원으로"),
                 ("서울", "로", "서울로"), ("국민연금", "이가", "국민연금이"), ("주택담보대출(본인)", "을를", "주택담보대출(본인)을"),
                 ("VOO", "은는", "VOO는"), ("SMR", "은는", "SMR은"), ("연금보험 1", "로", "연금보험 1로"),
                 ("후보 2", "과와", "후보 2와"), ("후보 3", "과와", "후보 3과"), ("", "은는", "는")]
        for word, pair, expected in cases:
            with self.subTest(word=word, pair=pair):
                self.assertEqual(fmt.josa(word, pair), expected)

    def test_a_spelled_out_percent_takes_the_sign_the_tables_use(self):
        found = []
        self.assertEqual(korean.tidy("금리가 1퍼센트포인트 오르면 0.5 퍼센트 포인트, 3퍼센트입니다. 몇 퍼센트인지", found),
                         "금리가 1%p 오르면 0.5%p, 3%입니다. 몇 퍼센트인지")
        self.assertEqual({f["kind"] for f in found}, {"unit"})
        # The number check reads a source that spells the unit out the same way, so the tidied prose stays grounded.
        self.assertEqual(narrative._written_percents("기준금리를 0.25퍼센트포인트 내렸고 물가는 2.1퍼센트입니다."),
                         {Decimal("0.25"), Decimal("2.1")})

    def test_a_source_is_named_by_its_publisher_never_by_a_masked_name_and_a_personal_page_is_marked(self):
        cases = [("24/7 Wall St.", "https://www.247wallst.com/a", "247wallst.com"),       # The identifier rules cut the name.
                 ("", "https://www.example.co.kr/a", "example.co.kr"),
                 ("한국은행", "https://www.bok.or.kr/a", "한국은행"),
                 ("Sky Atlas", "https://skyatlas.tistory.com/1", "Sky Atlas(개인 글)"),
                 ("네이버 블로그", "https://blog.naver.com/x/1", "네이버 블로그"),           # Already says what it is.
                 ("매일경제", "https://www.mk.co.kr/news/1", "매일경제"),
                 # A host that reads as an identifier is never put back: a fixed name, with the personal mark.
                 ("", "https://01012345678.tistory.com/1", "이름 미확인 매체(개인 글)"),
                 ("문의 010-1234-5678", "https://01012345678.tistory.com/1", "이름 미확인 매체(개인 글)"),
                 ("경제지", "https://blog.naver.com.example.org/1", "경제지")]             # Not under a personal host.
        for name, url, expected in cases:
            with self.subTest(name=name, url=url):
                self.assertEqual(research.publisher(name, url), expected)
        kept, _ = research.clean_items([{"topic": "T1", "headline": "금리 전망", "detail": "내년 인상 전망.",
                                         "published_on": "2026-11-10", "publisher": "24/7 Wall St.", "source_title": "Outlook",
                                         "source_url": "https://www.247wallst.com/a", "kind": "forecast"}],
                                       {"T1"}, AT.date())
        self.assertEqual(kept[0]["publisher"], "247wallst.com")
        for prompt in (research.SYSTEM_PROMPT, research.HOLDINGS_PROMPT):     # Both research calls carry the rule.
            self.assertIn(research.NO_PERSONAL_PAGES, prompt)

    def test_the_letter_keeps_labels_small_and_sets_sentences_and_table_cells_at_reading_size(self):
        table = report_html._table(["항목", "금액"], [["월 수입", "456만 원"]])
        self.assertIn('<td style="font-size:13px', table)
        self.assertIn('<th style="text-align:left;font-size:12px', table)            # A column head is a label.
        self.assertIn("font-size:13px", report_html._sources([("한국은행 2026-11-10", "https://www.bok.or.kr/a")]))
        self.assertIn("font-size:12px", report_html._pill("유지"))

    def test_a_table_amount_reads_short_from_a_million_won_and_exact_below(self):
        cases = [(Decimal("230000000"), "2.3억 원"), (Decimal("4560000"), "456만 원"), (Decimal("240000"), "240,000원"),
                 (Decimal("-1250000"), "-125만 원"), (None, "미상")]
        for amount, expected in cases:
            with self.subTest(amount=amount):
                self.assertEqual(report_html._won(amount), expected)

    def test_the_letter_explains_only_the_terms_it_uses_at_its_end(self):
        bands = ["<tr><td>코픽스가 오르면 <b>대환</b>을 검토합니다. 1%p 오를 때</td></tr>", '<a title="LTV">한도</a>']
        self.assertEqual([term for term, _ in report_html.glossary(bands)], ["코픽스", "대환", "%p"])   # Not the attribute.
        self.assertEqual(report_html.glossary(["<tr><td>오늘은 변동이 없습니다.</td></tr>"]), [])
        every = ["<p>" + " ".join(term for term, _ in report_html.GLOSSARY) + "</p>"]
        self.assertEqual(len(report_html.glossary(every)), report_html.GLOSSARY_LINES)
        footer = report_html._footer(terms=report_html.glossary(bands))
        self.assertIn("용어 풀이", footer)
        self.assertLess(footer.index("코픽스"), footer.index("Smith는 조회 전용 자문입니다"))
        self.assertNotIn("용어 풀이", report_html._footer())


def setUpModule():  # noqa: N802 - unittest hook
    fast_ledger.install()


def tearDownModule():  # noqa: N802 - unittest hook
    fast_ledger.uninstall()


if __name__ == "__main__":
    unittest.main()
