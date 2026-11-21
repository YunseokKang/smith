"""The letter review (FR-63): the week's letters are read back as sent and measured against the week before, the
outside reader's quotes are checked against the letter they name, the review is due every second week from its first
slot, and a failed layer is named in the mail instead of stopping it. Synthetic letters and mailbox; nothing is sent
and no model runs."""
import base64
import json
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from smith import config as config_module, gmail, ledger, letter_review
from smith.headless import HeadlessError

try:
    import fast_ledger
except ImportError:   # Run as `python -m unittest tests.test_letter_review`.
    from tests import fast_ledger

SEOUL = ZoneInfo("Asia/Seoul")
ME = "me@example.com"
CONFIG = {"app": {"timezone": "Asia/Seoul"}, "mail": {"recipient": ME}, "letter_review": {"enabled": True}}
CREDS = ("a", "b", "c")
SUNDAY = date(2026, 11, 22)
LAST_WEEK = ("<html><head><style>p{}</style></head><body><div>이번 주 판단</div>"
             "<div>변동금리 대출의 이자는 월 120만 원입니다.</div><div>기준금리는 2.50%로 유지됐습니다.</div>"
             "<div>연금 납입 한도가 300만 원 남았습니다.</div></body></html>")
THIS_WEEK = ("<html><body><div>이번 주 판단</div><div>변동금리 대출의 이자는 월 125만 원입니다.</div>"
             "<div>기준금리는 2.50%로 유지됐습니다.</div><div>금통위가 다음 주에 열립니다. 결정은 예측하지 않습니다.</div></body></html>")
READING = {"verdict": "목요일 레터는 지난주와 거의 같았습니다.", "strengths": ["목요일 금리·대출 레터가 이자 변화를 원 단위로 보였습니다."],
           "findings": [{"letter": "W1", "quote": "기준금리는 2.50%로  유지됐습니다.", "kind": "repetition", "size": "small",
                         "why": "지난주와 같은 문장입니다.", "fix": "바뀌지 않았으면 한 줄로 줄입니다."},
                        {"letter": "W1", "quote": "레터에 없는 문장입니다.", "kind": "quality", "size": "medium", "why": "근거가 없습니다.",
                         "fix": "출처를 붙입니다."},
                        {"letter": "W9", "quote": "기준금리", "kind": "stale", "size": "large", "why": "…", "fix": "…"}],
           "add": [{"what": "다음 금통위 날짜", "why": "변동금리 대출이 있습니다.", "where": "목요일 금리·대출"}],
           "drop": [{"letter": "W1", "quote": "결정은 예측하지 않습니다.", "why": "매주 같은 말입니다."}]}


CONTEXT = {"overall": "두 호출 모두 목적은 분명합니다.", "calls": [
    {"use_case": "report-narrative", "persona": "partly", "persona_note": "역할이 과제보다 큽니다.", "fix": "역할을 좁힙니다.",
     "missing": [{"what": "지난주 레터의 문장", "why": "반복을 피하려면 필요합니다."}],
     "unnecessary": [{"where": "household.net_worth", "why": "부동산 레터에는 쓰이지 않습니다."},
                     {"where": "입력에 없는 항목", "why": "근거가 없습니다."}]},
    {"use_case": "daily-writer", "persona": "fit", "persona_note": "맞습니다.", "fix": "없습니다.", "missing": [], "unnecessary": []}]}
INPUT = json.dumps({"household": {"net_worth": "987654321", "note": "비밀 메모"}, "holdings": {"AAPL": {"value": "1"}, "005930": {"value": "2"}},
                    "brief": []}, ensure_ascii=False)


def setUpModule():
    fast_ledger.install()


def tearDownModule():
    fast_ledger.uninstall()


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=SEOUL).astimezone(timezone.utc)


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "smith.db"
        self.mailbox = {}
        self.sent = []
        with closing(ledger.connect(self.db)):
            pass
        self.letter("rates1", "rates", date(2026, 11, 12), LAST_WEEK)
        self.letter("rates2", "rates", date(2026, 11, 19), THIS_WEEK)
        self.letter("manual1", "rates", date(2026, 11, 20), THIS_WEEK, trigger="manual")   # An on-demand copy: not reviewed.
        for use_case in ("report-narrative", "report-reader", "mail-answer", "daily-writer"):
            self.call(use_case)

    def call(self, use_case: str, *, outcome: str = "success") -> None:
        with closing(ledger.connect(self.db)) as conn:
            ledger.record_advice_run(conn, run_id=f"{use_case}-{outcome}", created_at=at(date(2026, 11, 19), 6), use_case=use_case,
                                     question="", payload=INPUT, prompt_version=f"{use_case}-v1", model="sonnet", cost_usd=None,
                                     outcome=outcome)

    def letter(self, message_id: str, kind: str, day: date, html: str, *, trigger: str = "scheduled") -> None:
        sent = at(day, 6, 2)
        with closing(ledger.connect(self.db)) as conn:
            conn.execute("INSERT INTO report_runs (report_id, slot, kind, trigger, created_at, as_of, missed_slots, subject, status, "
                         "attempts, message_id) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, 'sent', 1, ?)",
                         (message_id, sent.isoformat() if trigger == "scheduled" else None, kind, trigger,
                          sent.isoformat(timespec="microseconds"), sent.isoformat(), f"[Smith] {kind}", message_id))
        self.mailbox[message_id] = html

    def fetch(self, message_id: str) -> str:
        if message_id not in self.mailbox:
            raise gmail.MailError("http-404")
        return self.mailbox[message_id]

    def adapters(self, reading=READING, changes=(), context=None):
        context = CONTEXT if context is None else context
        def consultant(payload):
            if isinstance(reading, Exception):
                raise reading
            self.payload = payload
            return {"output": reading, "model": "gpt-test", "fallback_from": None, "cost_usd": None, "prompt": "{}"}

        def researcher(today):
            return {"items": list(changes), "gaps": [], "cost_usd": None, "prompt": "{}"}

        def context_reader(payload):
            if isinstance(context, Exception):
                raise context
            self.context_payload = payload
            return {"output": context, "model": "gpt-test", "fallback_from": None, "cost_usd": None, "prompt": "{}"}
        return lambda config, **_: (self.fetch, researcher, consultant, context_reader)

    def sender(self, credentials, *, recipient, subject, html, text, **kwargs):
        self.sent.append({"recipient": recipient, "subject": subject, "html": html, "text": text})
        return f"reviewletter{len(self.sent):02d}"

    def publish(self, day: date = SUNDAY, **kwargs) -> dict:
        return letter_review.publish(self.db, CONFIG, now=at(day, 9, 5), day=day, trigger="scheduled", recipient=ME,
                                     credentials_=CREDS, sender=self.sender, **{"adapters": self.adapters(), **kwargs})


class LetterTests(Fixture):
    def test_a_letter_is_read_as_lines_and_measured_against_the_week_before(self):
        self.assertEqual(letter_review.letter_lines(LAST_WEEK)[:2], ["이번 주 판단", "변동금리 대출의 이자는 월 120만 원입니다."])
        this, last = letter_review.letter_lines(THIS_WEEK), letter_review.letter_lines(LAST_WEEK)
        measured = letter_review.overlap(this, last)
        counted = [line for line in this if len(line) >= letter_review.MIN_LINE]   # The heading is too short to count.
        self.assertEqual(measured, {"same_pct": round(100 * len(this[2]) / sum(map(len, counted))),
                                    "numbers_only_pct": round(100 * len(this[1]) / sum(map(len, counted)))})
        self.assertEqual(letter_review.overlap(this, None), {"same_pct": None, "numbers_only_pct": None})   # Unknown, not zero.

    def test_only_the_weeks_scheduled_letters_are_collected_and_an_unreadable_one_is_named(self):
        self.letter("tax2", "tax", date(2026, 11, 20), THIS_WEEK)
        del self.mailbox["tax2"]
        with closing(ledger.connect_read_only(self.db)) as conn:
            letters, unread = letter_review.collect(conn, start=at(SUNDAY, 9) - timedelta(days=7), end=at(SUNDAY, 9),
                                                    tz="Asia/Seoul", fetch=self.fetch)
        self.assertEqual([(l["id"], l["name"], l["sent_on"]) for l in letters], [("W1", "목요일 금리·대출", "2026-11-19")])
        self.assertIsNotNone(letters[0]["same_pct"])
        self.assertEqual(unread, ["금요일 세금·연금(http-404)"])

    def test_gmail_returns_only_a_letter_smith_itself_sent(self):
        html = base64.urlsafe_b64encode(THIS_WEEK.encode("utf-8")).decode("ascii")
        message = {"labelIds": ["SENT"], "payload": {"headers": [{"name": "X-Smith-Message", "value": "1"}], "parts": [
            {"mimeType": "text/plain", "body": {"data": "eA=="}}, {"mimeType": "text/html", "body": {"data": html}}]}}
        self.assertEqual(gmail.sent_letter_html("t", "abc123", getter=lambda url, headers: (200, message)), THIS_WEEK)
        for case, other in (("no header", {**message, "payload": {**message["payload"], "headers": []}}),
                            ("not sent", {**message, "labelIds": ["INBOX"]})):
            with self.subTest(case), self.assertRaises(gmail.MailError) as raised:
                gmail.sent_letter_html("t", "abc123", getter=lambda url, headers, other=other: (200, other))
            self.assertEqual(raised.exception.code, "not-smith-mail")


class ReadingTests(Fixture):
    def test_a_quote_is_checked_against_its_letter_and_an_unfound_one_is_marked_not_deleted(self):
        letters = [{"id": "W1", "name": "목요일 금리·대출", "lines": letter_review.letter_lines(THIS_WEEK)}]
        reading = letter_review.clean_reading(READING, letters, letter_review.Scrub([]))
        self.assertEqual([(f["letter"], f["found"]) for f in reading["findings"]],
                         [("목요일 금리·대출", True), ("목요일 금리·대출", False)])   # W9 names no letter of the week.
        self.assertEqual(reading["dropped"], 1)
        self.assertTrue(reading["drop"][0]["found"])

    def test_what_the_client_typed_reaches_the_reader_as_a_placeholder_and_the_client_as_typed(self):
        typed = "Alex Kim 12345678901234 연금보험"   # An unlisted product named with its holder and account number.
        scrub = letter_review.Scrub([typed, "푸른마을", "KB"])   # Two letters would cut into other words: not replaced.
        letters = [{"id": "W1", "name": "금요일 세금·연금", "day": "금", "edition": "세금·연금", "sent_on": "2026-11-20",
                    "subject": "[Smith]", "lines": [f"{typed}은 유지를 권합니다.", "푸른마을 시세는 KB 기준입니다.",
                                                    "문의는 010-1234-5678, 계좌 99887766554433.", "잔액 1,234,567,890원"]}]
        text = letter_review.reader_payload(letters, {}, [], SUNDAY, scrub)["letters"][0]["text"]
        for private in ("Alex Kim", "12345678901234", "푸른마을", "010-1234-5678", "99887766554433"):
            self.assertNotIn(private, text)
        self.assertIn("[이름 1]은 유지를 권합니다.", text)
        self.assertIn("KB 기준", text)
        self.assertIn("1,234,567,890원", text)   # A figure with commas is not a number to hide.
        reading = letter_review.clean_reading({**READING, "findings": [
            {"letter": "W1", "quote": "[이름 1]은 유지를 권합니다.", "kind": "quality", "size": "small", "why": "[이름 1]의 근거가 없습니다.",
             "fix": "근거를 붙입니다."}], "drop": []}, letters, scrub)
        self.assertEqual((reading["findings"][0]["quote"], reading["findings"][0]["found"]), (f"{typed}은 유지를 권합니다.", True))
        self.assertIn(typed, reading["findings"][0]["why"])

    def test_the_local_config_names_of_homes_are_private_terms(self):
        config = {**CONFIG, "properties": {"home": {"apt_name": "푸른마을아파트", "dong": "정자동", "lawd_cd": "41135"}},
                  "household": {"occupational_institution": "가나다생명"}}
        self.assertEqual(sorted(letter_review.private_terms(self.db, config)), ["가나다생명", "정자동", "푸른마을아파트"])

    def test_a_typed_name_is_private_whatever_its_symbol_and_after_its_record_changed(self):
        history = [("toss", {"symbol": "AAPL", "instrument_name": "애플", "category": "stock"}),
                   ("manual-app", {"symbol": "AAPL", "instrument_name": "애플"}),             # The broker's own name: public.
                   ("manual-app", {"symbol": "AAPL", "instrument_name": "김민수 개인계좌"}),    # Typed over a listed symbol.
                   ("manual-sheet", {"instrument_name": "김민수 개인연금", "institution": "가나다생명", "value": "100",
                                     "maturity": "2030-01-01"})]                               # A revision of a closed record.
        original, ledger.record_field_history = ledger.record_field_history, lambda conn: history
        self.addCleanup(setattr, ledger, "record_field_history", original)
        self.assertEqual(sorted(letter_review.private_terms(self.db, CONFIG)), ["가나다생명", "김민수 개인계좌", "김민수 개인연금"])

    def test_a_source_that_cannot_be_opened_is_kept_unconfirmed_and_never_reads_as_no_change(self):
        raw = {"topic": "L1", "headline": "연금계좌 세액공제 한도 변경", "detail": "900만 원", "published_on": "2026-11-18",
               "publisher": "기획재정부", "source_title": "보도자료", "source_url": "https://www.moef.go.kr/x", "kind": "policy"}
        output = {"items": [raw, {**raw, "source_url": "http://insecure.example/x"}], "gaps": ["L3: 바뀐 것을 찾지 못했습니다."]}
        items, gaps = letter_review.rule_changes(output, SUNDAY, lambda items, **_: ([], {"dead": len(items)}))
        self.assertEqual([(i["ref"], i["confirmed"]) for i in items], [("R1", False)])
        self.assertEqual(gaps[0], "건강보험료: 바뀐 것을 찾지 못했습니다.")
        self.assertIn("1건은 출처 주소나 날짜가 맞지 않아", gaps[1])

    def test_a_rule_change_is_confirmed_only_on_an_official_page(self):
        item = {"ref": "R1", "topic": "L1", "headline": "연금계좌 세액공제 한도 변경", "detail": "900만 원", "published_on": "2026-11-18",
                "publisher": "기획재정부", "source_url": "https://www.moef.go.kr/x", "kind": "policy", "tier": "official",
                "check": {"status": "matched"}}
        for case, changed, confirmed in (("official and matched", {}, True), ("numbers not on the page", {"check": {"status": "mismatch"}}, False),
                                         ("a newspaper", {"tier": "secondary"}, False)):
            with self.subTest(case):
                change = letter_review._change({**item, **changed})
                self.assertEqual((change["confirmed"], change["after_check"]), (confirmed, True))   # After the tax rules' checked date.


class ScheduleTests(Fixture):
    def due(self, day: date, hour: int, minute: int = 0) -> date | None:
        with closing(ledger.connect(self.db)) as conn:
            return letter_review.due(conn, CONFIG, at(day, hour, minute))

    def test_the_review_is_due_every_second_week_and_a_missed_slot_only_for_two_days(self):
        self.assertIsNone(self.due(SUNDAY, 8, 59))
        self.assertEqual(self.due(SUNDAY, 9), SUNDAY)
        self.assertEqual(self.due(SUNDAY + timedelta(days=1), 7), SUNDAY)          # The laptop was off on Sunday.
        self.assertIsNone(self.due(SUNDAY + timedelta(days=3), 7))                 # Too late: the next Sunday instead.
        with closing(ledger.connect(self.db)) as conn:   # A run cut off after Gmail may have accepted the mail.
            token, _ = ledger.claim_daily_run(conn, run_key="letter-review:crashed", day=SUNDAY - timedelta(days=7), run_id="x",
                                              trigger="scheduled", now=at(SUNDAY, 8), lease=letter_review.LEASE, max_attempts=2,
                                              product=letter_review.PRODUCT)
            ledger.advance_daily_run(conn, run_key="letter-review:crashed", token=token, from_status="building", status="sending")
        self.assertIsNone(self.due(SUNDAY, 9))                                     # Possibly delivered a week ago: not yet.
        with closing(ledger.connect(self.db)) as conn:
            conn.execute("DELETE FROM daily_runs WHERE run_key = 'letter-review:crashed'")
        self.assertEqual(self.publish()["status"], "sent")
        self.assertIsNone(self.due(SUNDAY, 10))
        self.assertIsNone(self.due(SUNDAY + timedelta(days=7), 9, 5))              # One week later: not yet.
        self.assertEqual(self.due(SUNDAY + timedelta(days=14), 9, 5), SUNDAY + timedelta(days=14))

    def test_every_weeks_is_validated(self):
        for value, valid in ((1, True), (2, True), (0, False), (9, False), ("2", False), (True, False)):
            with self.subTest(value=value):
                if valid:
                    config_module._check_letter_review({"every_weeks": value})
                else:
                    with self.assertRaises(ValueError):
                        config_module._check_letter_review({"every_weeks": value})


class PublishTests(Fixture):
    def test_the_review_is_sent_once_with_its_findings_and_the_snapshot_keeps_no_letter_text(self):
        result = self.publish()
        self.assertEqual((result["status"], len(self.sent)), ("sent", 1))
        mail = self.sent[0]
        self.assertEqual(mail["subject"], "[Smith][레터 점검] 11/19~11/19 재무 레터 1통 · 고칠 것 2건")
        self.assertIn("기준금리는 2.50%로 유지됐습니다.", mail["text"])
        self.assertIn("이 문장을 레터에서 찾지 못했습니다", mail["text"])
        self.assertIn("목요일 금리·대출", mail["html"])
        self.assertEqual(self.payload["measures"]["period_weeks"], 2)
        with closing(ledger.connect_read_only(self.db)) as conn:
            stored = ledger.daily_snapshot(conn, letter_review.run_key(SUNDAY))
            audited = [row[0] for row in conn.execute("SELECT use_case FROM advice_runs ORDER BY use_case")]
        self.assertNotIn("lines", str(stored.get("measures")))
        self.assertEqual(stored["reading"]["verdict"], READING["verdict"])
        self.assertEqual([a for a in audited if a.startswith("letter-review")],
                         ["letter-review-context", "letter-review-reader", "letter-review-research"])
        self.assertEqual(stored["context"]["read"], ["report-narrative"])   # The one call that got a verdict.
        self.assertEqual(self.publish()["error_code"], "already-sent")
        self.assertEqual(len(self.sent), 1)

    def test_each_call_type_is_read_by_its_outline_never_its_values_and_in_rotation(self):
        self.assertEqual(self.publish()["status"], "sent")
        sent = json.dumps(self.context_payload, ensure_ascii=False)
        self.assertEqual([c["use_case"] for c in self.context_payload["calls"]], ["report-narrative", "report-reader"])
        for value in ("987654321", "비밀 메모", "AAPL", "005930"):
            self.assertNotIn(value, sent)                      # Paths, sizes and shapes only; a ticker key reads "<key>".
        paths = [row["path"] for row in self.context_payload["calls"][0]["input_outline"]]
        self.assertEqual(paths, ["household", "household.net_worth", "household.note", "holdings", "holdings.<key> x2", "brief"])
        wide = json.dumps({f"section_{n}": {"a": "x" * n, "b": 1} for n in range(letter_review.MAX_OUTLINE_ROWS)})
        listed = [row["path"] for row in letter_review.outline(wide)]
        self.assertEqual(len(listed), letter_review.MAX_OUTLINE_ROWS)   # Too many rows: no top-level section is lost.
        self.assertTrue(all(f"section_{n}" in listed for n in range(letter_review.MAX_OUTLINE_ROWS)))
        mail = self.sent[0]["text"]
        self.assertIn("레터 서술 · 역할 설정 일부만 맞음", mail)
        self.assertIn("필요 없는 정보: household.net_worth 부동산", mail)
        self.assertIn("입력에 없는 항목(이 항목을 입력이나 지침에서 찾지 못했습니다)", mail)
        self.assertNotIn("하루 일과 메일 · 역할", mail)            # A call the reader was not given is dropped.
        self.assertIn("판정 없음: 발송 전 독자", mail)             # No verdict came back for it: named, and not counted as read.
        with closing(ledger.connect_read_only(self.db)) as conn:
            self.assertEqual(ledger.daily_snapshot(conn, letter_review.run_key(SUNDAY))["context"]["read"], ["report-narrative"])
        self.assertIn("하루 일과 메일 1회", mail)                  # Every call type is still counted.
        later = SUNDAY + timedelta(days=14)
        self.letter("rates3", "rates", later - timedelta(days=3), THIS_WEEK)
        self.assertEqual(self.publish(day=later)["status"], "sent")
        self.assertEqual([c["use_case"] for c in self.context_payload["calls"]], ["report-reader", "mail-answer"])

    def test_a_failed_reader_is_named_and_the_measures_still_go_out(self):
        result = self.publish(adapters=self.adapters(reading=HeadlessError("usage-limit")))
        self.assertEqual(result["status"], "sent")
        self.assertIn("지표만(읽기 실패)", self.sent[0]["subject"])
        self.assertIn("usage-limit", self.sent[0]["text"])
        self.assertIn("지난주와 같음", self.sent[0]["text"])

    def test_a_week_without_letters_sends_nothing(self):
        result = self.publish(day=SUNDAY + timedelta(days=28))
        self.assertEqual((result["status"], result["error_code"], self.sent), ("skipped", "no-letters", []))

    def test_a_mailbox_that_cannot_be_read_is_a_retryable_failure(self):
        self.mailbox.clear()
        result = self.publish()
        self.assertEqual((result["status"], result["error_code"], self.sent), ("failed", "letters-unread", []))


if __name__ == "__main__":
    unittest.main()
