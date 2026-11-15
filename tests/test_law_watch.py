"""Tax-law watch (FR-54) against synthetic 법제처 responses shaped like the official guide (no live call)."""
import shutil
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from smith import law_watch, report_html

NOW = datetime(2026, 11, 20, 1, tzinfo=timezone.utc)


def row(name, promulgated, effective, status="현행", kind="일부개정", serial="1000", number="20000"):
    return (f"<law id=\"1\"><법령일련번호>{serial}</법령일련번호><현행연혁코드>{status}</현행연혁코드>"
            f"<법령명한글><![CDATA[{name}]]></법령명한글><법령ID>001</법령ID><공포일자>{promulgated}</공포일자>"
            f"<공포번호>{number}</공포번호><제개정구분명>{kind}</제개정구분명><시행일자>{effective}</시행일자>"
            f"<법령상세링크>/DRF/lawService.do?MST={serial}</법령상세링크></law>")


def answer(*rows):
    body = f"<LawSearch><target>eflaw</target><totalCnt>{len(rows)}</totalCnt><page>1</page>{''.join(rows)}</LawSearch>"
    return lambda url: (200, body.encode("utf-8"))


class VersionTests(unittest.TestCase):
    def test_only_the_exact_law_is_kept_newest_promulgation_first(self):
        found = law_watch.versions("종합부동산세법", "oc", get=answer(
            row("종합부동산세법", "20250101", "20250101", serial="1"),
            row("종합부동산세법", "20261210", "20270101", status="시행예정", serial="2"),
            row("종합부동산세법 시행령", "20261230", "20270101", serial="3")))
        self.assertEqual([(v["serial"], v["promulgated"], v["effective"], v["status"]) for v in found],
                         [("2", "2026-12-10", "2027-01-01", "시행예정"), ("1", "2025-01-01", "2025-01-01", "현행")])

    def test_the_request_asks_for_versions_in_force_and_scheduled(self):
        seen = []

        def get(url):
            seen.append(url)
            return answer(row("지방세법", "20251231", "20260101"))(url)

        law_watch.versions("지방세법", "my-oc", get=get)
        self.assertIn("target=eflaw", seen[0])
        self.assertIn("nw=2%2C3", seen[0])
        self.assertTrue(seen[0].startswith("https://www.law.go.kr/DRF/lawSearch.do?"))

    def test_failures_are_named(self):
        refused = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><result>사용자 정보 검증에 실패하였습니다.</result>"
                   "<msg>OPEN API 호출 시 사용자 검증을 위하여 정확한 서버장비의 IP주소 및 도메인주소를 등록해 주세요.</msg></Response>")
        cases = ((lambda url: (200, refused.encode()), "auth-failed"),   # As answered live on 2026-11-15.
                 (lambda url: (200, "<html>사용자 정보 검증에 실패하였습니다</html".encode()), "auth-failed"),
                 (lambda url: (200, b"not xml"), "invalid-response"),
                 (lambda url: (200, b"<LawSearch><page>1</page></LawSearch>"), "invalid-response"),
                 (lambda url: (503, b""), "http-503"),
                 (answer(row("지방세법 시행령", "20251231", "20260101")), "no-match"),
                 (answer(row("지방세법", "2025-12-31", "20260101")), "invalid-row"))
        for get, code in cases:
            with self.subTest(code=code), self.assertRaises(law_watch.LawWatchError) as caught:
                law_watch.versions("지방세법", "oc", get=get)
            self.assertEqual(caught.exception.code, code)


    def test_the_detail_link_with_the_key_is_never_kept(self):
        # Codex review 2026-11-15 (P0): the API's 법령상세링크 carries the OC.
        body = ("<LawSearch><totalCnt>1</totalCnt><law id=\"1\"><법령일련번호>9</법령일련번호><법령명한글>지방세법</법령명한글>"
                "<공포일자>20251231</공포일자><시행일자>20260101</시행일자><제개정구분명>일부개정</제개정구분명>"
                "<법령상세링크>/DRF/lawService.do?OC=secret-oc&amp;target=eflaw&amp;MST=9</법령상세링크></law></LawSearch>")
        found = law_watch.versions("지방세법", "secret-oc", get=lambda url: (200, body.encode()))
        self.assertNotIn("secret-oc", found[0]["link"])
        result = law_watch.check("secret-oc", now=NOW, get=lambda url: (200, body.replace("지방세법", "x").encode()))
        self.assertNotIn("secret-oc", repr(result))

    def test_a_short_or_paged_answer_is_counted(self):
        # Codex review 2026-11-15: totalCnt 2 with one row read as complete.
        short = "<LawSearch><totalCnt>2</totalCnt>" + row("지방세법", "20251231", "20260101") + "</LawSearch>"
        with self.assertRaises(law_watch.LawWatchError) as caught:
            law_watch.versions("지방세법", "oc", get=lambda url: (200, short.encode()))
        self.assertEqual(caught.exception.code, "incomplete")
        pages = {"1": [row("지방세법 시행령", "20250101", "20250101")] * 100, "2": [row("지방세법", "20251231", "20260101")] * 50}

        def get(url):
            page = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["page"][0]
            return 200, f"<LawSearch><totalCnt>150</totalCnt>{''.join(pages[page])}</LawSearch>".encode()

        self.assertEqual(len(law_watch.versions("지방세법", "oc", get=get)), 50)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def test_an_amendment_promulgated_after_the_rules_were_checked_is_pending(self):
        def get(url):
            name = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
            rows = [row(name, "20250101", "20250101")]
            if name == "종합부동산세법":
                rows.append(row(name, "20261210", "20270101", status="시행예정"))
            return answer(*rows)(url)

        result = law_watch.check("oc", now=NOW, get=get)
        found = law_watch.pending(result)
        self.assertEqual([(a["name"], a["promulgated"], a["rules"]) for a in found],
                         [("종합부동산세법", "2026-12-10", "holding")])
        summary = law_watch.summary(result)
        self.assertEqual((summary["failed"], summary["no_key"]), ([], False))
        law_watch.save(self.folder, result)
        self.assertFalse(law_watch.stale(self.folder, NOW + timedelta(days=6)))
        self.assertTrue(law_watch.stale(self.folder, NOW + timedelta(days=8)))

    def test_a_refused_key_is_asked_once_not_for_every_law(self):
        calls = []
        refused = "<Response><result>사용자 정보 검증에 실패하였습니다.</result><msg>IP주소 등록</msg></Response>"

        def get(url):
            calls.append(url)
            return 200, refused.encode()

        result = law_watch.check("oc", now=NOW, get=get)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(entry["error"] == "auth-failed" for entry in result["laws"].values()))
        self.assertEqual(len(law_watch.summary(result)["failed"]), len(law_watch.WATCHED))

    def test_a_failed_week_keeps_the_amendment_found_before(self):
        # Codex review 2026-11-15 (P1): a 503 the week after erased the warning.
        def found(url):
            name = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
            rows = [row(name, "20250101", "20250101")]
            if name == "종합부동산세법":
                rows.append(row(name, "20261210", "20270101", status="시행예정"))
            return answer(*rows)(url)

        first = law_watch.check("oc", now=NOW, get=found)
        second = law_watch.check("oc", now=NOW + timedelta(days=7), get=lambda url: (503, b""), previous=first)
        self.assertEqual([a["name"] for a in law_watch.pending(second)], ["종합부동산세법"])
        entry = second["laws"]["종합부동산세법"]
        self.assertEqual((entry["error"], entry["read_at"]), ("http-503", NOW.isoformat()))

    def test_an_older_failed_attempt_saved_last_keeps_the_newer_reading(self):
        # Codex review round 2: `smith law check` and the scheduled check read the same state; the one that failed
        # and saved last wrote back its stale view and erased the amendment the other had just found.
        def found(url):
            name = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
            rows = [row(name, "20250101", "20250101")]
            if name == "종합부동산세법":
                rows.append(row(name, "20261210", "20270101", status="시행예정"))
            return answer(*rows)(url)

        before = law_watch.check(None, now=NOW)
        failed = law_watch.check("oc", now=NOW + timedelta(minutes=1), get=lambda url: (503, b""), previous=before)
        good = law_watch.check("oc", now=NOW + timedelta(minutes=2), get=found, previous=before)
        law_watch.save(self.folder, good)
        law_watch.save(self.folder, failed)
        stored = law_watch.load(self.folder)
        self.assertEqual([a["name"] for a in law_watch.pending(stored)], ["종합부동산세법"])
        self.assertIsNone(stored["laws"]["종합부동산세법"]["error"])
        self.assertEqual(stored["checked_at"], (NOW + timedelta(minutes=2)).isoformat())

    def test_without_the_key_every_law_is_unread_and_retried_a_day_later(self):
        result = law_watch.check(None, now=NOW)
        self.assertTrue(all(entry["error"] == "no-key" for entry in result["laws"].values()))
        self.assertTrue(law_watch.summary(result)["no_key"])
        self.assertEqual(law_watch.pending(result), [])
        law_watch.save(self.folder, result)
        self.assertFalse(law_watch.stale(self.folder, NOW + timedelta(hours=20)))
        self.assertTrue(law_watch.stale(self.folder, NOW + timedelta(hours=25)))
        self.assertTrue(law_watch.stale(Path(self.folder, "missing"), NOW))

    def test_an_article_is_asked_for_by_its_six_digit_number(self):
        seen = []

        def get(url):
            seen.append(url)
            if "lawSearch.do" in url:
                return answer(row("지방세법 시행령", "20251230", "20260101", serial="777"))(url)
            return 200, "<법령><조문><조문내용>제110조의2(재산세 세율 특례 대상 1세대 1주택의 범위)</조문내용></조문></법령>".encode()

        self.assertEqual(law_watch.article("지방세법 시행령", "110의2", "oc", get=get),
                         ["제110조의2(재산세 세율 특례 대상 1세대 1주택의 범위)"])
        self.assertIn("JO=011002", seen[1])
        self.assertIn("MST=777", seen[1])
        self.assertIn("efYd=20260101", seen[1])
        with self.assertRaises(law_watch.LawWatchError):
            law_watch.article("지방세법 시행령", "110조", "oc", get=get)


class RenderTests(unittest.TestCase):
    def data(self, **watch):
        return {"as_of": datetime(2026, 11, 20, 6, tzinfo=ZoneInfo("Asia/Seoul")),
                "law_watch": {"checked_at": NOW.isoformat(), "failed": [], "no_key": False, "pending": [], **watch}}

    def test_the_source_line_says_when_the_laws_were_read(self):
        self.assertEqual(report_html._law_watch(self.data(), "holding"), ("", " · 법제처 확인 11월 20일: 그 뒤 공포된 개정 없음"))
        _, note = report_html._law_watch(self.data(failed=["지방세법"]), "holding")
        self.assertIn("읽은 법령에서는 그 뒤 공포된 개정 없음, 1개 법령은 이번에 읽지 못함", note)
        # Codex review 2026-11-15: a check that read nothing never says "no amendment".
        every = [name for name, _, of in law_watch.WATCHED if of == "holding"]
        self.assertEqual(report_html._law_watch(self.data(failed=every), "holding")[1], " · 법제처 확인 실패(11월 20일)")
        self.assertEqual(report_html._law_watch({"as_of": NOW, "law_watch": None}, "tax")[1], " · 법제처 개정 확인 전")
        self.assertEqual(report_html._law_watch(self.data(no_key=True), "tax")[1], " · 법제처 개정 확인 전")

    def test_a_pending_amendment_leads_its_own_rule_set_only(self):
        amendment = {"name": "종합부동산세법", "kind": "일부개정", "promulgated": "2026-12-10", "effective": "2027-01-01",
                     "about": "종합부동산세", "rules": "holding"}
        notice, note = report_html._law_watch(self.data(pending=[amendment]), "holding")
        self.assertIn("Smith 계산에 아직 반영 전", notice)
        self.assertIn("2026-12-10 공포, 2027-01-01 시행", notice)
        self.assertIn("반영 전 개정 1건", note)
        self.assertEqual(report_html._law_watch(self.data(pending=[amendment]), "tax")[0], "")
        # Codex review round 2: removing the key does not hide what was found before.
        notice, note = report_html._law_watch(self.data(pending=[amendment], no_key=True,
                                                         failed=[n for n, _, _ in law_watch.WATCHED]), "holding")
        self.assertIn("Smith 계산에 아직 반영 전", notice)
        self.assertEqual(note, " · 법제처 키 없음, 지난 확인 기준 반영 전 개정 1건")


if __name__ == "__main__":
    unittest.main()
