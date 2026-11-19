"""The subject, the status and the proposal cards follow what presses today, and a reply records a decision
(report design §45, FR-58)."""
import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from smith import ledger, proposal_reply, proposals, report_html
from smith.importer import parse_import
from smith.proposals import Proposal
from smith.report_data import build_report

import fast_ledger

AT = datetime(2026, 11, 19, tzinfo=timezone.utc)
TODAY = AT.date()


def proposal(key, priority, *, shown=0, due=None, headline=""):
    return Proposal(key=key, priority=priority, title=f"{key} 제안입니다.", why="이유입니다.", effect="효과입니다.",
                    risks="위험입니다.", timing="이번 달", reconsider="조건입니다.", certainty="계산", identity=key,
                    times_shown=shown, renewed=not shown, due=due, headline=headline or f"{key} 요약")


class UrgencyTests(unittest.TestCase):
    def test_a_proposal_presses_by_its_deadline_then_as_new_then_asks_for_a_decision(self):
        soon, far, past = TODAY + timedelta(days=30), TODAY + timedelta(days=31), TODAY - timedelta(days=1)
        cases = [
            (proposal("a", 1), "new"),                        # Act-now, just raised.
            (proposal("a", 1, shown=3), "new"),
            (proposal("a", 1, shown=4), "decide"),            # Said STALE_AFTER times: the decision is asked for.
            (proposal("a", 2), ""),                           # This month's action is introduced without pressing.
            (proposal("a", 2, shown=4), "decide"),
            (proposal("a", 3, shown=25), ""),                 # Keep-or-review never asks for a decision.
            (proposal("a", 3, shown=25, due=TODAY), ""),
            (proposal("a", 2, shown=9, due=soon), "due"),     # A near deadline presses again, however often it was said.
            (proposal("a", 1, shown=9, due=TODAY), "due"),
            (proposal("a", 1, shown=9, due=far), "decide"),
            (proposal("a", 1, due=past), "new"),              # A deadline that passed presses no more.
        ]
        for case, expected in cases:
            with self.subTest(priority=case.priority, shown=case.times_shown, due=case.due):
                self.assertEqual(proposals.urgency(case, TODAY), expected)

    def test_a_put_off_proposal_returns_once_when_its_deadline_nears(self):
        due = TODAY + timedelta(days=20)
        asked = proposal("a", 2, due=due)
        before_window = {"status": "declined", "status_at": (due - timedelta(days=60)).isoformat()}
        inside_window = {"status": "declined", "status_at": (due - timedelta(days=25)).isoformat()}
        self.assertTrue(proposals._due_again(asked, before_window, TODAY))
        self.assertFalse(proposals._due_again(asked, inside_window, TODAY))        # Declined with the deadline in view.
        self.assertFalse(proposals._due_again(proposal("a", 2), before_window, TODAY))   # No deadline: the decision stands.
        # The decision's day is the household's, not UTC's: 01:00 in Seoul on the window's first day is inside it.
        seoul = timezone(timedelta(hours=9))
        year_end = proposal("a", 2, due=date(2026, 12, 31))
        first_day = {"status": "declined", "status_at": "2026-11-30T16:00:00+00:00"}   # 12월 1일 01:00 KST
        self.assertFalse(proposals._due_again(year_end, first_day, date(2026, 12, 2), seoul))
        self.assertTrue(proposals._due_again(year_end, first_day, date(2026, 12, 2), timezone.utc))

    def test_the_decision_grammar_reads_only_replies_that_are_decisions(self):
        cases = {
            "1번 할게": [(1, "accepted")], "2번 보류할게요.": [(2, "declined")], "3번은 완료했습니다!": [(3, "done")],
            "1번 할게, 2번 보류": [(1, "accepted"), (2, "declined")],
            "1, 2번 보류\n3번 했어": [(1, "declined"), (2, "declined"), (3, "done")],
            "1번 안 해": [(1, "declined")], "1번 나중에 할게": [(1, "declined")],
            "1번 할게 1번 보류": [(1, "declined")],            # The last word on a number stands.
            # A question, a reason, a name instead of a number, a date: left to the answering model, nothing recorded.
            "2번 안 해도 돼?": None, "1번 할게 근데 왜 25%야?": None, "삼성전자 정리할게": None, "3월 20일에 할게": None,
            "": None, "1번 할게 " + "가" * 200: None,
            # A longer number is never cut into card numbers, and a run of digits is rejected at once (no backtracking).
            "123번 할게": None, "1" * 150 + "?": None, "1 " * 90 + "?": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text[:20]):
                self.assertEqual(proposal_reply.read(text), expected)


class LetterTests(unittest.TestCase):
    """One small ledger; the proposals under test are put into the built report."""

    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.db = Path(directory.name) / "smith.db"
        doc = {"schema_version": 1, "import_id": "u1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": [
                   {"id": "cash", "kind": "asset", "owner_id": "self", "effective_at": AT.isoformat(), "revision": 1,
                    "status": "active", "category": "cash", "account_type": "bank", "currency": "KRW", "value": "10000000",
                    "valuation_method": "manual", "liquidity": "immediate"}]}
        with closing(ledger.connect(cls.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
        with closing(ledger.connect_read_only(cls.db)) as conn:
            cls.built = {kind: build_report(conn, as_of=AT, known_at=AT, baseline=None, kind=kind)
                         for kind in ("rates", "overview")}

    def letter(self, kind, found, **over):
        data = dict(self.built[kind], **over)
        data["advice"] = {**data["advice"], "proposals": found, "undetermined": [], "household_urgent": []}
        return data

    def test_the_subject_and_the_hero_say_what_presses_today(self):
        year_end = date(2026, 12, 15)
        cases = [
            # (the day's proposals, whether the subject says 대응 필요, the subject's last words, the hero line)
            ([proposal("variable-rate", 3, shown=25)], False, "할 일 없음", "오늘 새로 실행하실 일은 없습니다"),
            ([proposal("prepayment", 2, shown=1)], False, "제안 1건", "이번 달 제안: prepayment 요약"),
            ([proposal("prepayment", 1, shown=22), proposal("refinance", 2, shown=9)], False, "결정 요청 2건",
             "결정을 기다립니다: prepayment 요약"),
            ([proposal("prepayment", 1, shown=22), proposal("refinance", 1)], True, "새 제안 1건",
             "⚡ 금주 핵심 액션: refinance 요약"),
            ([proposal("prepayment", 1, shown=22, due=year_end), proposal("refinance", 1)], True, "12/15까지 1건",
             "⚡ 금주 핵심 액션: prepayment 요약"),
        ]
        for found, act, lead, hero in cases:
            with self.subTest(lead=lead):
                subject, html = report_html.render(self.letter("rates", found))
                self.assertTrue(subject.endswith(f" · {lead}"), subject)
                self.assertEqual("대응 필요" in subject, act, subject)
                self.assertNotIn("안정", subject)           # A quiet day does not announce itself.
                self.assertIn(hero, html)
                self.assertNotIn("번째 보고", html)        # The count of deliveries is said only on a decision request.
        # Another area presses on this day's letter only by a deadline within days, and the letter says where.
        data = self.letter("rates", [proposal("variable-rate", 3, shown=25)])
        data["advice"]["household_urgent"] = ["pension-credit"]
        subject, html = report_html.render(data)
        self.assertTrue(subject.endswith("· 대응 필요 · 다른 분야 기한 임박"), subject)
        self.assertIn("세금·연금 보고의 기한이 가까운 제안 확인", html)

    def test_a_decision_request_card_says_how_to_answer_and_cards_are_numbered_through_the_letter(self):
        own = [proposal("concentration-trim", 1, shown=22), proposal("insurance-review", 2, shown=1)]
        shared = [proposal("emergency-reserve", 2, shown=5)]    # A household proposal: the letter's second block.
        data = self.letter("overview", own + shared)
        self.assertEqual(report_html.card_numbers(data),
                         {"concentration-trim": 1, "insurance-review": 2, "emergency-reserve": 3})
        html = report_html.render(data)[1]
        self.assertIn("22번 안내드렸습니다.", html)
        self.assertIn("「1번 할게」, 「1번 보류」, 「1번 했어」", html)
        self.assertIn("「3번 할게」, 「3번 보류」, 「3번 했어」", html)          # The household card keeps counting.
        self.assertNotIn("「2번 할게」, 「2번 보류」", html)                   # A proposal still being introduced asks nothing.
        self.assertEqual(html.count("제안은 답장으로 정하실 수 있습니다."), 1)   # Said once in a letter.
        # The spouse's replies record nothing: that letter asks to pass the decision on and promises no record.
        spouse = report_html.render(self.letter("overview", own + shared, audience="partner"))[1]
        self.assertIn("배우자분께 전해 주십시오", spouse)
        self.assertNotIn("번 할게", spouse)
        self.assertNotIn("답장으로", spouse.split("22번 안내드렸습니다.")[1][:200])

    def test_a_reply_decides_the_card_it_names_and_nothing_else(self):
        asked = proposal("concentration-trim", 1, headline="삼성전자 초과분 정리")
        with closing(ledger.connect(self.db)) as conn:
            ledger.record_proposals_shown(conn, report_id="r1", at=AT, shown=[
                proposals.snapshot(asked, 1), proposals.snapshot(proposal("variable-rate", 3))])   # The second has no card.
            first = ledger.asked_proposal(conn, report_id="r1", number=1)
            self.assertEqual((first["key"], first["label"], first["status"]), ("concentration-trim", "삼성전자 초과분 정리", "open"))
            self.assertIsNone(ledger.asked_proposal(conn, report_id="r1", number=2))
            self.assertIsNone(ledger.asked_proposal(conn, report_id="other", number=1))
        self.assertIsNone(proposal_reply.answer(self.db, "r1", "1번은 왜 권하는 거야?", now=AT))      # A question: untouched.
        html, text, result = proposal_reply.answer(self.db, "r1", "1번 보류, 2번 했어", now=AT)
        self.assertEqual([(d["number"], d["outcome"]) for d in result["decisions"]], [(1, "recorded"), (2, "missing")])
        self.assertIn("1번(삼성전자 초과분 정리): 보류로 기록했습니다.", text)
        self.assertIn("2번: 이 보고서에서 그 번호의 제안을 찾지 못해 기록하지 않았습니다.", text)
        self.assertIn("Smith 결정 기록", html)
        with closing(ledger.connect(self.db)) as conn:
            active = ledger.active_proposals(conn)
            self.assertEqual((active["concentration-trim"]["status"], active["variable-rate"]["status"]), ("declined", "open"))
            # A retried reply adds no second event; a decision on a card raised anew since is refused.
            again = proposal_reply.apply(self.db, "r1", [(1, "declined")], now=AT)
            self.assertEqual(again[0]["outcome"], "unchanged")
            ledger.record_proposals_shown(conn, report_id="r2", at=AT + timedelta(days=7),
                                          shown=[proposals.snapshot(replace(asked, renewed=True), 1)])
            late = proposal_reply.apply(self.db, "r1", [(1, "accepted")], now=AT + timedelta(days=8))
            self.assertEqual(late[0]["outcome"], "superseded")
            self.assertEqual(ledger.active_proposals(conn)["concentration-trim"]["status"], "open")
            events = [e["event"] for e in ledger.proposal_events(conn) if e["key"] == "concentration-trim"]
        self.assertEqual(events.count("declined"), 1)
        self.assertIn("최근 보고서의 번호로 답장해 주십시오", proposal_reply.render("1번 할게", late)[1])

    def test_a_retried_reply_never_undoes_a_later_one_and_a_card_stays_with_the_instance_it_was_built_on(self):
        first = proposal("insurance-review", 2, headline="보험료 점검")
        with closing(ledger.connect(self.db)) as conn:
            ledger.record_proposals_shown(conn, report_id="s1", at=AT, shown=[proposals.snapshot(first, 1)])
            built_on = ledger.active_proposals(conn)["insurance-review"]["instance_id"]
        # The first reply's confirmation failed and is retried after a second reply decided otherwise.
        self.assertEqual(proposal_reply.apply(self.db, "s1", [(1, "accepted")], now=AT, source="q1")[0]["outcome"], "recorded")
        self.assertEqual(proposal_reply.apply(self.db, "s1", [(1, "done")], now=AT, source="q2")[0]["outcome"], "recorded")
        self.assertEqual(proposal_reply.apply(self.db, "s1", [(1, "accepted")], now=AT, source="q1")[0]["outcome"], "unchanged")
        with closing(ledger.connect(self.db)) as conn:
            self.assertEqual(ledger.active_proposals(conn)["insurance-review"]["status"], "done")
            # A report built on that instance is delivered after another report renewed the proposal: its card number
            # stays with the instance whose figures it printed, so the answer is refused, not put on the new figures.
            ledger.record_proposals_shown(conn, report_id="s2", at=AT + timedelta(days=1),
                                          shown=[proposals.snapshot(replace(first, renewed=True), 1)])
            slow = replace(first, renewed=False, instance_id=built_on)
            ledger.record_proposals_shown(conn, report_id="s3", at=AT + timedelta(days=1, minutes=5),
                                          shown=[proposals.snapshot(slow, 1)])
            self.assertEqual(ledger.asked_proposal(conn, report_id="s3", number=1)["instance_id"], built_on)
            newest = ledger.active_proposals(conn)["insurance-review"]
            self.assertEqual(ledger.asked_proposal(conn, report_id="s2", number=1)["instance_id"], newest["instance_id"])
            self.assertEqual(newest["times_shown"], 1)       # The late report is not counted on the new instance.
        self.assertEqual(proposal_reply.apply(self.db, "s3", [(1, "done")], now=AT, source="q3")[0]["outcome"], "superseded")
        with closing(ledger.connect(self.db)) as conn:
            self.assertEqual(ledger.active_proposals(conn)["insurance-review"]["status"], "open")
        # A reply that changed nothing the first time is remembered too: its retry cannot undo a later reply.
        for source, status, outcome in (("q4", "accepted", "recorded"), ("q5", "accepted", "unchanged"),
                                        ("q6", "done", "recorded"), ("q5", "accepted", "unchanged")):
            self.assertEqual(proposal_reply.apply(self.db, "s2", [(1, status)], now=AT, source=source)[0]["outcome"], outcome)
        with closing(ledger.connect(self.db)) as conn:
            self.assertEqual(ledger.active_proposals(conn)["insurance-review"]["status"], "done")


def setUpModule():  # noqa: N802 - unittest hook
    fast_ledger.install()


def tearDownModule():  # noqa: N802 - unittest hook
    fast_ledger.uninstall()


if __name__ == "__main__":
    unittest.main()
