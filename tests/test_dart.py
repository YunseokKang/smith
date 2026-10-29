import io
import json
import tempfile
import unittest
import urllib.parse
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from smith import dart

NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)
KEY = "k" * 40


def zipped(name, text):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text.encode("utf-8"))
    return buffer.getvalue()


def form(common="361", preferred="361", classes=(("가상전자우", "우선주", "361"),), record="2026-09-30", pay="2026-11-27",
         kind="현금배당"):
    """A dividend decision in the DART form's shape (synthetic company and amounts)."""
    def row(*cells):
        return "<tr>" + "".join(f'<td><span class="xforms_input">{c}</span></td>' for c in cells) + "</tr>"
    rows = [row("1. 배당구분", "분기배당"), row("2. 배당종류", kind), row("- 현물자산의 상세내역", "-"),
            row("3. 1주당 배당금(원)", "보통주식", common), row("종류주식", preferred), row("- 차등배당 여부", "미해당"),
            row("4. 시가배당률(%)", "보통주식", "1.7"), row("종류주식", "2.2"), row("6. 배당기준일", record),
            row("7. 배당금지급 예정일자", pay), row("10. 이사회결의일(결정일)", "2026-10-29")]
    table = [row("종류주식명", "종류주식구분", "1주당 배당금(원)", "시가배당률(%)", "배당금총액(원)")]
    table += [row(name, kind, amount, "2.2", "1,000,000") for name, kind, amount in classes]
    return f"<html><body><table>{''.join(rows)}</table><table>{''.join(table)}</table></body></html>"


class FakeDart:
    def __init__(self, listing=None, document=None, documents=None):
        self.documents = documents or {}
        self.calls = []
        self.listing = listing if listing is not None else {"status": "000", "list": [
            {"rcept_no": "20261029800101", "rcept_dt": "20261029", "report_nm": "현금ㆍ현물배당결정", "corp_name": "가상전자"},
            {"rcept_no": "20260730800050", "rcept_dt": "20260730", "report_nm": "현금ㆍ현물배당결정", "corp_name": "가상전자"},
            {"rcept_no": "20261029800087", "rcept_dt": "20261029", "report_nm": "영업(잠정)실적(공정공시)", "corp_name": "가상전자"}]}
        self.document = document or form()

    def __call__(self, url):
        path = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        self.calls.append((path, dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))))
        if path == "corpCode.xml":
            return zipped("CORPCODE.xml", "<result><list><corp_code>00999999</corp_code><corp_name>가상전자</corp_name>"
                                          "<stock_code>123450</stock_code></list><list><corp_code>00888888</corp_code>"
                                          "<corp_name>비상장</corp_name><stock_code> </stock_code></list></result>")
        if path == "list.json":
            return json.dumps(self.listing, ensure_ascii=False).encode("utf-8")
        number = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["rcept_no"]
        return zipped(f"{number}.xml", self.documents.get(number, self.document))


class DartTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name) / "dart" / "corp_codes.json"

    def test_the_newest_dividend_decision_is_read_from_the_filing(self):
        fake = FakeDart(documents={"20260730800050": form(common="350", record="2026-06-30", pay="2026-08-20")})
        corp = dart.corp_code("123455", KEY, self.cache, now=NOW, fetch=fake)    # The preferred share maps to its issuer.
        self.assertEqual(corp, "00999999")
        self.assertEqual(dart.corp_code("123450", KEY, self.cache, now=NOW, fetch=fake), "00999999")
        self.assertEqual([c for c, _ in fake.calls].count("corpCode.xml"), 1)     # Cached for a week.
        found = dart.latest_dividend(corp, KEY, today=NOW.date(), fetch=fake)
        self.assertEqual((found.filing.rcept_no, found.kind, found.per_share, found.record_date, found.pay_date),
                         ("20261029800101", "분기배당", {"common": Decimal("361"), "preferred": Decimal("361")},
                          date(2026, 9, 30), date(2026, 11, 27)))
        # Both decisions were read; the newer record date wins (July's paid the June quarter).
        self.assertEqual([c[1].get("rcept_no") for c in fake.calls if c[0] == "document.xml"],
                         ["20261029800101", "20260730800050"])
        self.assertTrue(found.filing.url.startswith("https://dart.fss.or.kr/"))

    def test_a_late_correction_of_an_earlier_quarter_does_not_replace_this_quarters_decision(self):
        listing = {"status": "000", "list": [
            {"rcept_no": "20261030800010", "rcept_dt": "20261030", "report_nm": "[기재정정]현금ㆍ현물배당결정", "corp_name": "가상전자"},
            {"rcept_no": "20261029800101", "rcept_dt": "20261029", "report_nm": "현금ㆍ현물배당결정", "corp_name": "가상전자"}]}
        fake = FakeDart(listing=listing, documents={"20261030800010": form(common="350", record="2026-06-30", pay="2026-08-20")})
        found = dart.latest_dividend("00999999", KEY, today=NOW.date(), fetch=fake)
        self.assertEqual((found.filing.rcept_no, found.per_share["common"]), ("20261029800101", Decimal("361")))

    def test_forms_and_statuses_that_cannot_be_trusted(self):
        filing = dart.Filing("20261029800101", date(2026, 10, 29), "현금ㆍ현물배당결정", "가상전자")
        for classes in ((("가상전자우", "우선주", "361"), ("가상전자2우B", "우선주", "411")),   # Which class gets what?
                        (("가상전자우", "우선주", "361"), ("가상전자2우B", "우선주", "-"))):     # One class gets nothing.
            with self.subTest(classes=classes):
                self.assertEqual(dart.parse_dividend(form(classes=classes), filing).per_share, {"common": Decimal("361")})
        self.assertFalse(dart.parse_dividend(form(kind="현물배당"), filing).cash)       # No cash amount to compute.
        self.assertTrue(dart.parse_dividend(form(), filing).cash)
        with self.assertRaises(dart.Error):
            dart.parse_dividend("<html><table><tr><td>1. 배당구분</td><td>분기배당</td></tr></table></html>", filing)
        self.assertEqual(dart.filings("00999999", KEY, since=date(2026, 7, 1), until=NOW.date(),
                                      fetch=FakeDart(listing={"status": "013", "message": "조회된 데이타가 없습니다."})), [])
        with self.assertRaises(dart.Error) as caught:
            dart.filings("00999999", KEY, since=date(2026, 7, 1), until=NOW.date(),
                         fetch=FakeDart(listing={"status": "020", "message": "요청 제한을 초과하였습니다."}))
        self.assertEqual(caught.exception.code, "status-020")
        self.assertNotIn(KEY, str(caught.exception))                               # The key never rides an error.
        with self.assertRaises(dart.Error):                                         # A JSON array is no answer.
            dart.filings("00999999", KEY, since=date(2026, 7, 1), until=NOW.date(), fetch=lambda url: b"[]")
        blocked = Path(self.cache.parent.parent, "file")                            # The cache cannot be written:
        blocked.write_text("x", encoding="utf-8")                                   # the downloaded codes still answer.
        self.assertEqual(dart.corp_code("123450", KEY, blocked / "corp_codes.json", now=NOW, fetch=FakeDart()), "00999999")


if __name__ == "__main__":
    unittest.main()
