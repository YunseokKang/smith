"""Card statements into spending (FR-60): the page's own totals gate a reading, offsets and refunds keep their meaning,
and the statement attachment opens only with the right password and only towards the card company's host."""
import base64
import hashlib
import unittest

from smith import samsungcard, spending


def _row(merchant: str, amount: str, date: str = "26. 10. 9", detail: str = "") -> dict[str, str]:
    return {"merchant": merchant, "amount": amount, "date": date, "detail": detail or f"이용금액 {amount}원"}


def _raw(rows: list[dict[str, str]], total: str, **more: object) -> dict[str, object]:
    return {"settled": more.get("settled", "20261121"), "period": more.get("period", "26. 10. 9 ~ 26. 11. 8"),
            "sections": [{"key": "D0", "total": total, "rows": rows}, *more.get("extra", [])]}


def _two_months(folder: object) -> None:
    """September: two dinners of 20,000. October: three, one of them 60,000."""
    spending.save_raw(folder, _raw([_row("동네식당", "20,000", "26. 9. 10"), _row("동네식당", "20,000", "26. 9. 20")], "40,000",
                                   settled="20261021", period="26. 9. 9 ~ 26. 10. 8"))
    spending.save_raw(folder, _raw([_row("동네식당", "20,000"), _row("동네식당", "20,000", "26. 10. 16"),
                                    _row("큰 식당", "60,000", "26. 10. 23")], "100,000"))


class ParseTest(unittest.TestCase):
    def test_discount_prepayment_and_refund(self) -> None:
        rows = [_row("스타벅스코리아", "11,000", detail="이용금액 13,000원 ⓦ 청구할인 -2,000원"),
                _row("동네고깃집", "179,000"), _row("버스타고_APP", "-9,900", detail="이용금액 -9,900원"),
                {"merchant": "미리입금/할인/포인트자동납부 금액 등", "amount": "-179,000", "date": "", "detail": "이용금액"}]
        statement = spending.parse(_raw(rows, "1,100"))
        cafe, meat, refund = statement.entries
        self.assertEqual((cafe.spent, cafe.billed, cafe.benefits), (13_000, 11_000, (("청구할인", 2_000),)))
        # The offset belongs to the purchase, not to the refund row between them: spent stays, billed goes to zero.
        self.assertEqual((meat.spent, meat.billed, meat.prepaid), (179_000, 0, True))
        self.assertEqual((refund.spent, refund.prepaid), (-9_900, False))

    def test_rows_that_do_not_add_up_are_refused(self) -> None:
        with self.assertRaises(spending.SpendingError) as caught:
            spending.parse(_raw([_row("스타벅스코리아", "11,000")], "21,000"))
        self.assertEqual(caught.exception.code, "totals-mismatch")

    def test_a_reading_without_its_total_or_without_a_purchase_section_is_refused(self) -> None:
        cases = {"total-missing": _raw([_row("스타벅스코리아", "11,000")], ""),
                 "statement-shape": {"settled": "20261121", "period": "26. 10. 9 ~ 26. 11. 8", "sections": []},
                 # A fee section alone is a partly rendered page, not a month without purchases.
                 "statement-shape ": {"settled": "20261121", "period": "26. 10. 9 ~ 26. 11. 8", "sections": [
                     {"key": "D5", "total": "30,000", "rows": [_row("가상카드 연회비", "30,000")]}]}}
        for code, raw in cases.items():
            with self.subTest(code), self.assertRaises(spending.SpendingError) as caught:
                spending.parse(raw)
            self.assertEqual(caught.exception.code, code.strip())

    def test_overseas_detail_is_not_counted_twice(self) -> None:
        detail = "접수일 26.10.20 업종/국가 택시요금/미국 현지 이용금액 4 USD 접수금액 4.05 USD"
        extra = [{"key": "DE", "total": "5,662", "rows": [_row("UBER TRIP", "5,662", detail=detail)]}]
        statement = spending.parse(_raw([_row("UBER TRIP", "5,662")], "5,662", extra=extra))
        self.assertEqual(len(statement.entries), 1)
        self.assertEqual(statement.entries[0].foreign, "4 USD")
        self.assertEqual(spending.categorize(statement.entries[0], {}), "transport")


class SummaryTest(unittest.TestCase):
    def test_own_table_wins_and_unknown_names_are_listed(self) -> None:
        rows = [_row("동네고깃집", "50,000"), _row("처음 보는 가게", "7,000", "26. 10. 10"),
                _row("아파트관리비_09월", "200,000")]
        month = spending.month(spending.parse(_raw(rows, "257,000")), {"동네고깃집": "dining"})
        self.assertEqual(month["categories"]["dining"]["amount"], 50_000)
        self.assertEqual(month["unsorted"], ["처음 보는 가게"])
        self.assertEqual((month["spent"], month["fixed"]), (257_000, 200_000))
        self.assertEqual(month["small"], {"amount": 7_000, "count": 1})   # 2026-10-10 is a Saturday
        self.assertEqual(month["weekend"], {"amount": 7_000, "count": 1})


def _attachment(password: str, page: str) -> str:
    """A synthetic VestMail attachment, encrypted the way the real one is."""
    from cryptography.hazmat.decrepit.ciphers.algorithms import SEED
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    once = hashlib.sha256(password.encode()).digest()
    key, iv = hashlib.sha256(once).digest()[:16], once[:16]
    plain = key + page.encode()
    plain += bytes([16 - len(plain) % 16]) * (16 - len(plain) % 16)
    encryptor = Cipher(SEED(key), modes.CBC(iv)).encryptor()
    return 'var s= new Array();s[0] = "%s";' % base64.b64encode(b"\0" * 16 + encryptor.update(plain) + encryptor.finalize()).decode()


class AttachmentTest(unittest.TestCase):
    PAGE = '<form name=f1 method="POST" action="%s"><input type=hidden name=pvalue value="AB12cd"/><input type=hidden name=_command value=login></form>'

    def test_form_opens_with_the_password_only(self) -> None:
        sealed = _attachment("000101", self.PAGE % "https://bill.samsungcard.com/ehub/rpc/LSECCommand")
        self.assertEqual(samsungcard.login_form(sealed, "000101"),
                         {"action": "https://bill.samsungcard.com/ehub/rpc/LSECCommand", "pvalue": "AB12cd", "_command": "login"})
        with self.assertRaises(samsungcard.StatementError) as caught:
            samsungcard.login_form(sealed, "000102")
        self.assertEqual(caught.exception.code, "wrong-password")

    def test_form_to_another_host_is_refused(self) -> None:
        sealed = _attachment("000101", self.PAGE % "https://bill.samsungcard.com.example.org/steal")
        with self.assertRaises(samsungcard.StatementError):
            samsungcard.login_form(sealed, "000101")



class LetterTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        spending.save_raw(self.folder, _raw([_row("스타벅스코리아", "11,000"), _row("동네고깃집", "179,000")], "190,000"))

    def test_each_week_takes_the_next_angle_and_bars_come_once(self) -> None:
        from datetime import date
        from smith import spending_letter
        first = spending_letter.picture(self.folder, date(2026, 11, 28))
        self.assertEqual((first["angle"], first["first_week"], first["note"]), ("feel", True, None))
        spending_letter._keep(self.folder, {"week": "2026-W48", "settled": "2026-11-21", "angle": "feel",
                                            "note": {"headline": "h", "points": [{"title": "t", "text": "x"}]}})
        same_week = spending_letter.picture(self.folder, date(2026, 11, 28))
        self.assertEqual((same_week["angle"], same_week["first_week"], same_week["note"]["headline"]), ("feel", True, "h"))
        next_week = spending_letter.picture(self.folder, date(2026, 12, 5))
        self.assertEqual((next_week["angle"], next_week["first_week"], next_week["note"]), ("benefits", False, None))

    def test_a_paragraph_with_an_amount_the_payload_lacks_is_dropped(self) -> None:
        from smith import spending_letter
        page = "https://www.samsungcard.com/x"
        sent = '{"amount": 179000, "count": 2, "public_card_terms": [{"text": "전월 실적 50만 원부터 적립됩니다."}]}'
        note = spending_letter.verify({"headline": "고깃집 18만 원이 가장 컸습니다", "points": [
            {"title": "근거 있음", "text": "동네고깃집에서 18만 원을 쓰셨습니다."},
            {"title": "근거 없음", "text": "커피에 55만 원을 쓰셨습니다."},
            # A link the payload did not hold excuses no amount, and an amount in a title is checked like any other.
            {"title": "링크만 붙임", "text": "커피에 55만 원을 쓰셨습니다.", "source_url": "https://example.invalid/not-fetched"},
            {"title": "제목에 77만 원", "text": "동네고깃집에서 18만 원을 쓰셨습니다."},
            {"title": "공개 조건", "text": "전월 실적 50만 원부터 적립됩니다.", "source_url": page},
            {"title": "모르는 출처", "text": "18만 원을 쓰셨습니다.", "source_url": "https://example.invalid/other"}]},
            sent, {page: True})
        self.assertEqual([p["title"] for p in note["points"]], ["근거 있음", "공개 조건", "모르는 출처"])
        self.assertEqual([p.get("source_url") for p in note["points"]], [None, page, None])

    def test_an_unconfirmed_term_passes_only_beside_its_link_and_is_marked(self) -> None:
        from smith import spending_letter
        page = "https://www.samsungcard.com/y"
        output = {"headline": "확인할 조건이 있습니다", "points": [
            {"title": "확인할 조건", "text": "전월 실적 70만 원 조건이 안내되어 있는지 확인해 보시기 바랍니다.", "source_url": page},
            {"title": "링크 없이", "text": "전월 실적 70만 원부터 적립됩니다."}]}
        note = spending_letter.verify(output, '{"amount": 179000}', {page: False}, "전월 실적 70만 원부터 적립됩니다.")
        self.assertEqual([(p["title"], p.get("unconfirmed")) for p in note["points"]], [("확인할 조건", True)])

    def test_the_web_lookup_knows_the_card_name_only_and_the_note_has_no_tools(self) -> None:
        import json
        import tempfile
        from datetime import date, datetime, timezone
        from pathlib import Path
        from unittest import mock
        from smith import spending_letter
        with tempfile.TemporaryDirectory() as tmp:
            db, where = Path(tmp) / "smith.db", Path(tmp) / "spending"
            fee = [{"key": "D5", "total": "30,000", "rows": [_row("가상카드 연회비", "30,000")]}]
            spending.save_raw(where, _raw([_row("스타벅스코리아", "11,000"), _row("동네고깃집", "179,000")], "190,000", extra=fee))
            spending_letter._keep(where, {"week": "2026-W48", "settled": "2026-11-21", "angle": "feel",
                                          "note": {"headline": "h", "points": [{"title": "t", "text": "x"}]}})
            calls = []

            def run(prompt: str, **options: object):
                calls.append((prompt, tuple(options.get("tools", ()))))
                if options["schema"] is spending_letter.TERMS_SCHEMA:
                    return {"terms": [{"text": "연회비는 3만 원입니다.", "source_url": "https://www.samsungcard.com/card"},
                                      {"text": "매달 99만 원을 돌려줍니다.", "source_url": "https://blog.example.org/card"},
                                      {"text": "매달 88만 원을 돌려줍니다.", "source_url": "https://www.samsungcard.com/invented"}]}, None
                return {"headline": "연회비 3만 원을 따져 볼 때입니다", "points": [
                    {"title": "연회비", "text": "연회비는 3만 원입니다.", "source_url": "https://www.samsungcard.com/card"},
                    {"title": "외식", "text": "동네고깃집에서 179,000원을 쓰셨습니다."}]}, None

            def check(item: dict, *, now: object) -> dict:   # code reads the cited page; the invented one says nothing
                fetched.append(item["source_url"])
                return {"status": "mismatch" if "invented" in item["source_url"] else "matched", "final_url": item["source_url"]}

            fetched: list[str] = []
            with mock.patch.object(spending_letter.headless, "run", run), \
                    mock.patch.object(spending_letter.sources, "check", check), \
                    mock.patch.object(spending_letter.headless, "find_claude", lambda: Path("claude.exe")):
                result = spending_letter.write(db, today=date(2026, 12, 5), now=datetime(2026, 12, 5, tzinfo=timezone.utc),
                                               secrets=[])
            self.assertEqual(result["outcome"], "written")
            (lookup, lookup_tools), (note, note_tools) = calls
            self.assertEqual((lookup_tools, note_tools), (spending_letter.headless.WEB_TOOLS, ()))
            self.assertEqual(json.loads(lookup), {"cards": [{"issuer": "삼성카드", "card_as_printed": "가상카드 연회비"}]})
            self.assertNotIn("blog.example.org", note)   # a term from another host never reaches the note
            self.assertNotIn("blog.example.org", " ".join(fetched))   # nor is its page fetched
            self.assertNotIn("88만", note)   # a term whose page does not hold its figure is not evidence
            self.assertIn("스타벅스코리아", note)   # the household's figures go to the call without tools only

    def test_payload_leaves_out_names_that_point_at_a_home(self) -> None:
        import json
        from datetime import date
        from smith import spending_letter
        spending.save_raw(self.folder, _raw([_row("아파트관리비_09월어느마을", "200,000"), _row("처음 보는 건물", "20,000")], "220,000"))
        view = spending_letter.picture(self.folder, date(2026, 11, 28))
        text = json.dumps(spending_letter.payload(view, today=date(2026, 11, 28), client_notes=[], health_constraints=[],
                                                  client_memory=[], card=[]), ensure_ascii=False)
        self.assertNotIn("어느마을", text)
        self.assertNotIn("처음 보는 건물", text)
        self.assertIn("200000", text)   # the amount stays; only the name goes


class VerdictTest(unittest.TestCase):
    """The note judges every week, against figures code computed (client 2026-11-21: figures alone are not feedback)."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        _two_months(self.folder)

    def test_yardsticks_are_the_own_past_the_price_of_one_purchase_and_the_income_share(self) -> None:
        sticks = spending.yardsticks(spending.load(self.folder), {}, income=1_000_000)
        (dining,) = sticks["areas"]
        self.assertEqual((dining["amount"], dining["count"], dining["each"], dining["per_week"]), (100_000, 3, 33_333, 0.7))
        self.assertEqual(dining["large"], {"from": 50_000, "count": 1, "amount": 60_000})
        self.assertEqual(dining["usual"], {"months": 1, "amount": 40_000, "count": 2.0, "each": 20_000})
        self.assertEqual((dining["over_usual"], dining["over_usual_pct"]), (60_000, 150))
        self.assertEqual((sticks["income"]["share"], sticks["income"]["average_share"]), (0.1, 0.07))
        self.assertNotIn("income", spending.yardsticks(spending.load(self.folder), {}))   # unknown income: no share

    def test_one_verdict_a_kind_with_figures_and_percentages_the_payload_holds(self) -> None:
        import json
        from smith import spending_letter
        sent = json.dumps({"yardsticks": {"areas": [{"amount": 100000, "over_usual": 60000, "over_usual_pct": 150}],
                                          "income": {"share": 0.1}}})
        verdict = lambda kind, text: {"kind": kind, "subject": "외식", "category": "식비·외식", "text": text}   # noqa: E731
        note = spending_letter.verify({"headline": "외식이 평소보다 6만 원 늘었습니다", "points": [], "verdicts": [
            verdict("adjust", "외식이 평소보다 6만 원, 150% 늘었습니다. 큰 식사를 한 번 줄여 보시기를 권합니다."),
            verdict("adjust", "같은 종류의 두 번째 판정입니다."),
            verdict("keep", "월 소득의 10%로 유지하셔도 좋습니다."),
            verdict("check", "월 소득의 45%에 이릅니다."),          # a share the payload does not hold
            verdict("check", "외식에 77만 원을 쓰셨습니다.")]}, sent)   # an amount it does not hold
        self.assertEqual([(v["kind"], v["category"]) for v in note["verdicts"]], [("keep", "dining"), ("adjust", "dining")])

    def test_figures_written_in_words_are_checked_like_the_rest(self) -> None:
        import json
        from smith import spending_letter
        sent = json.dumps({"yardsticks": {"areas": [{"each": 6274}, {"each": 50571}], "income": {"share": 0.1}}})
        cases = {"한 잔에 약 6천 원이고 한 끼는 5만 1천 원, 월 소득의 10퍼센트입니다.": True,
                 "한 잔에 약 9천 원입니다.": False, "한 끼에 7만 2천 원입니다.": False, "월 소득의 99퍼센트입니다.": False,
                 "월 소득의 99프로입니다.": False}
        for text, kept in cases.items():
            with self.subTest(text):
                note = spending_letter.verify({"headline": "h", "points": [], "verdicts": [
                    {"kind": "keep", "subject": "커피", "category": "커피·간식", "text": text}]}, sent)
                self.assertEqual(note is not None, kept)

    def test_the_next_statement_shows_what_the_last_one_was_asked_to_adjust(self) -> None:
        from datetime import date
        from smith import report_html, spending_letter
        asked = {"headline": "h", "points": [], "verdicts": [
            {"kind": "adjust", "subject": "큰 식사", "category": "dining", "text": "한 번 줄여 보시기를 권합니다."},
            {"kind": "keep", "subject": "외식", "category": "dining", "text": "유지하셔도 좋습니다."}]}
        spending_letter._keep(self.folder, {"week": "2026-W43", "settled": "2026-10-21", "angle": "trim", "note": asked})
        view = spending_letter.picture(self.folder, date(2026, 11, 28), income=1_000_000)
        self.assertEqual(view["follow_up"], [{"category": "dining", "subject": "큰 식사",
                                              "then": {"label": "9/9~10/8", "amount": 40_000, "count": 2},
                                              "now": {"label": "10/9~11/8", "amount": 100_000, "count": 3}}])
        view["note"] = {**asked, "headline": "큰 식사가 늘었습니다"}
        page = report_html._my_spending(view)
        for shown in ("조정", "유지", "큰 식사", "월 소득의 <b>10%</b>", "1회 평균 33,333원", "지난 명세서에서 짚은 것"):
            self.assertIn(shown, page)


class LookupTest(unittest.TestCase):
    def test_place_types(self) -> None:
        self.assertEqual(spending.category_of_place(["barbecue_restaurant", "restaurant"]), "dining")
        self.assertEqual(spending.category_of_place(["japanese_izakaya_restaurant"]), "drinks")
        self.assertIsNone(spending.category_of_place(["corporate_office", "point_of_interest"]))

    def test_unknown_merchants_are_asked_once_and_the_own_table_wins(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from smith import spending_command
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "spending"
            spending.save_raw(folder, _raw([_row("처음 보는 술집", "40,000"), _row("스타벅스코리아", "5,000")], "45,000"))
            asked = []
            def lookup(name: str, *, key: str):
                asked.append(name)
                return (name, ["bar"], "")
            self.assertEqual(len(spending_command.classify(folder, lookup=lookup, key="k")), 1)
            self.assertEqual(spending_command.classify(folder, lookup=lookup, key="k"), [])
            self.assertEqual(asked, ["처음 보는 술집"])
            entry = spending.load(folder)[0].entries[0]
            self.assertEqual(spending.categorize(entry, spending.load_own(folder)), "drinks")
            (folder / "merchants.json").write_text(json.dumps({"처음 보는 술집": "dining"}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(spending.categorize(entry, spending.load_own(folder)), "dining")


class SyncDueTest(unittest.TestCase):
    def test_checked_daily_from_the_8th_until_the_months_statement_is_stored(self) -> None:
        import tempfile
        from datetime import date
        from pathlib import Path
        from smith import spending_command
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertTrue(spending_command.sync_due(folder, date(2026, 11, 3)))    # nothing stored yet
            spending.save_raw(folder, _raw([_row("스타벅스코리아", "5,000")], "5,000"))   # paid 2026-11-21
            self.assertFalse(spending_command.sync_due(folder, date(2026, 11, 25)))
            self.assertFalse(spending_command.sync_due(folder, date(2026, 12, 5)))
            self.assertTrue(spending_command.sync_due(folder, date(2026, 12, 12)))
            (folder / "sync.json").write_text('{"checked": "2026-12-12"}', encoding="utf-8")
            self.assertFalse(spending_command.sync_due(folder, date(2026, 12, 12)))
            self.assertTrue(spending_command.sync_due(folder, date(2026, 12, 13)))

    def test_the_scheduled_classification_retries_daily_and_logs_counts_only(self) -> None:
        import tempfile
        from datetime import date
        from pathlib import Path
        from smith import google_places, spending_command
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "spending"
            spending.save_raw(folder, _raw([_row("처음 보는 술집", "40,000")], "40,000"))

            def failing(name: str, *, key: str):
                raise google_places.GoogleError("timeout")
            line = spending_command.classify_scheduled(folder, date(2026, 12, 12), lookup=failing, key="k")
            self.assertNotIn("술집", line)
            self.assertIn("0 merchant(s) looked up", line)
            found = lambda name, *, key: (name, ["bar"], "")   # noqa: E731
            self.assertIsNone(spending_command.classify_scheduled(folder, date(2026, 12, 12), lookup=found, key="k"))
            line = spending_command.classify_scheduled(folder, date(2026, 12, 13), lookup=found, key="k")
            self.assertEqual(line, "1 merchant(s) looked up, 1 sorted")
            self.assertIsNone(spending_command.classify_scheduled(folder, date(2026, 12, 14), lookup=found, key="k"))


if __name__ == "__main__":
    unittest.main()
