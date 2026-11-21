"""Card statements into spending (FR-60): the page's own totals gate a reading, offsets and refunds keep their meaning,
and the statement attachment opens only with the right password and only towards the card company's host."""
import base64
import hashlib
import unittest

from smith import samsungcard, spending


def _row(merchant: str, amount: str, date: str = "26. 10. 9", detail: str = "") -> dict[str, str]:
    return {"merchant": merchant, "amount": amount, "date": date, "detail": detail or f"이용금액 {amount}원"}


def _raw(rows: list[dict[str, str]], total: str, **more: object) -> dict[str, object]:
    return {"settled": "20261121", "period": "26. 10. 9 ~ 26. 11. 8",
            "sections": [{"key": "D0", "total": total, "rows": rows}, *more.get("extra", [])]}


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
        sent = '{"amount": 179000, "count": 2}'
        note = spending_letter.verify({"headline": "고깃집 18만 원이 가장 컸습니다", "points": [
            {"title": "근거 있음", "text": "동네고깃집에서 18만 원을 쓰셨습니다."},
            {"title": "근거 없음", "text": "커피에 55만 원을 쓰셨습니다."},
            {"title": "공개 조건", "text": "전월 실적 50만 원부터 적립됩니다.", "source_url": "https://www.samsungcard.com/x"}]}, sent)
        self.assertEqual([p["title"] for p in note["points"]], ["근거 있음", "공개 조건"])

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


if __name__ == "__main__":
    unittest.main()
