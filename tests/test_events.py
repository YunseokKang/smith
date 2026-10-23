import json
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from smith import config as config_module
from smith import events, gmail, ledger
from smith.importer import parse_import
from smith.records import ImportBatch, Observation

import fast_ledger

KST = ZoneInfo("Asia/Seoul")
T0 = datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)   # 16:00 KST, Monday
T1 = T0 + timedelta(days=1)
CONFIG = {"app": {"timezone": "Asia/Seoul"},
          "events": {"enabled": True, "check_time": "16:10", "portfolio_move_pct": 2.0}}


def rec(record_id, kind, effective, revision=1, **fields):
    return {"id": record_id, "kind": kind, "owner_id": "self", "effective_at": effective.isoformat(),
            "revision": revision, "status": "active", **fields}


def stock(record_id, effective, revision, symbol, market, currency, quantity, price, name):
    value = str(Decimal(quantity) * Decimal(price))
    return rec(record_id, "asset", effective, revision, category="stock", account_type="brokerage", currency=currency,
               value=value, valuation_method="market", liquidity="days", symbol=symbol, market=market,
               quantity=quantity, unit_price=price, instrument_name=name)


def researched(items=(), decision=None, page=None):
    """A fake researcher. `page` is the text of the official page the decision cites: code reads it like the real
    page (events.read_decision) and the result carries that check, as events.research_event does."""
    def researcher(question, *, decision_wanted, today, secrets, meeting=None):
        assert "200000000" not in question and "8300000" not in question and "VOO" not in question   # Public event only.
        check = None
        if page is not None:
            check = {"url": "https://www.bok.or.kr/x", "official": True,
                     **events.read_decision(page, meeting, quote=(decision or {}).get("guidance_quote", ""))}
        return {"items": list(items), "gaps": [], "decision": decision or {}, "decision_check": check, "cost_usd": "0.10",
                "prompt_version": "test", "dropped": {}}
    return researcher


# The Bank's own words, as its decision page carries them (synthetic rates).
HOLD_PAGE = ("통화정책방향 2026.10.22 총 재 - 오늘 금융통화위원회는 한국은행 기준금리를 현재의 2.75%로 유지하기로 결정하였습니다. "
             "이 결정은 금통위원 전원 일치였습니다. 질 문 - 오늘 만장일치가 나왔는데 지난번 기준금리를 2.50%에서 2.75%로 인상한 "
             "효과는? 총 재 - 물가오름세가 목표 수준까지 안정적으로 수렴한다는 확신이 들 때까지 계속 대응하겠습니다.")
HIKE_PAGE = "2026년 10월 22일 금융통화위원회는 한국은행 기준금리를 2.75%에서 3.00%로 상향 조정하여 통화정책을 운용하기로 하였다."
UNKNOWN = {"outcome": "unknown", "new_rate_pct": "", "previous_rate_pct": "", "guidance_quote": "", "source_url": ""}


def item(headline, url, *, tier="secondary", status="reachable", **extra):
    return {"headline": headline, "detail": "상세.", "source_url": url, "published_on": "2026-10-06", "kind": "fact",
            "tier": tier, "ref": "R1", "check": {"status": status}, **extra}


class EventTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "smith.db"
        self.apply("e0", T0, [
            stock("voo", T0, 1, "VOO", "US", "USD", "10", "100", "VOO"),
            stock("ss", T0, 1, "005930", "KR", "KRW", "100", "70000", "삼성전자"),
            rec("cash", "asset", T0, category="cash", account_type="bank", currency="KRW", value="5000000",
                valuation_method="manual", liquidity="immediate"),
            rec("loan", "liability", T0, category="mortgage", currency="KRW", outstanding_principal="200000000",
                annual_rate="0.035", rate_type="variable", repayment_method="equal_principal")], fx="1300")
        # A day later: VOO -10% in dollars, Samsung -5.7%; FX unchanged. Listed value 8,300,000 -> -530,000 (-6.4%).
        self.apply("e1", T1, [stock("voo", T1, 2, "VOO", "US", "USD", "10", "90", "VOO"),
                              stock("ss", T1, 2, "005930", "KR", "KRW", "100", "66000", "삼성전자")], fx="1300")
        self.sent = []

    def apply(self, import_id, at, records, fx=None):
        doc = {"schema_version": 1, "import_id": import_id, "source": "manual", "mode": "patch", "as_of": at.isoformat(),
               "owners": [{"id": "self"}], "records": records}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=at)
            if fx:
                ledger.apply_import(conn, ImportBatch(f"fx-{import_id}", "toss", "snapshot", at, ("self",), (), "x",
                                                      (Observation("USD", "fx_mid_rate", "KRW", fx, at),)), recorded_at=at)

    def sender(self, credentials, *, recipient, subject, html, text):
        self.sent.append((subject, html, text))
        return "msg-1"

    def check(self, now, researcher=None, config=CONFIG):
        return events.check(self.db, config, now=now, reason="manual", sync=False, researcher=researcher,
                            sender=self.sender, recipient="me@example.com", credentials=("a", "b", "c"))

    def test_a_move_beyond_the_threshold_is_an_event_with_its_movers(self):
        with closing(ledger.connect_read_only(self.db)) as conn:
            move = events.measure_move(conn, T0, T1)
            found = events.detect(conn, CONFIG, T1, T0)
            calm = events.detect(conn, {**CONFIG, "events": {**CONFIG["events"], "portfolio_move_pct": 10}}, T1, T0)
        self.assertEqual((move["start"], move["change"], move["fx"]), (Decimal(8_300_000), Decimal(-530_000), Decimal(0)))
        self.assertEqual(move["ratio"].quantize(Decimal("0.0001")), Decimal("-0.0639"))
        self.assertEqual([(e.kind, e.day, e.slot) for e, _ in found], [("portfolio_move", date(2026, 10, 6), "pm")])
        self.assertEqual(calm, [])                                        # Below the configured share: no event.

    def test_check_sends_one_note_per_event_and_records_the_check(self):
        researcher = researched([item("코스피 2.8% 하락, 중동 긴장과 AI 투자 우려", "https://news.example.com/kospi")])
        outcomes = self.check(T1, researcher)
        self.assertEqual([(o["status"], o["key"]) for o in outcomes], [("sent", "portfolio_move:2026-10-06:pm")])
        subject, html, text = self.sent[0]
        self.assertEqual(subject, "[Smith · 사건 안내] 10/06 보유 종목 -6.4% (-53만 원)")
        for needle in ("가장 크게 움직인 종목", "삼성전자", "-40만 원", "VOO", "-13만 원", "한국 상장 -40만 원 · 미국 상장 -13만 원",
                       "코스피 2.8% 하락", "참고", "스트레스 테스트가 가정하는 주식 30% 하락의 21%", "제안은 정기 보고에서만"):
            self.assertIn(needle, html)
        self.assertIn("보유 종목 -6.4%", text)
        # The next check measures from this one, so the same move is not an event again; the check is on record.
        self.assertEqual(self.check(T1 + timedelta(minutes=15), researcher), [])
        self.assertEqual(len(self.sent), 1)
        with closing(ledger.connect_read_only(self.db)) as conn:
            self.assertEqual(ledger.last_event_check(conn), T1 + timedelta(minutes=15))
            note = ledger.event_note(conn, "portfolio_move:2026-10-06:pm")
        self.assertEqual((note["status"], note["attempts"], note["details"]["refs"]), ("sent", 1, ["https://news.example.com/kospi"]))
        self.assertNotIn("items", json.dumps(note["details"]).replace('"items": 1', ""))   # Figures and refs only.

    def test_a_rate_decision_comes_from_the_statistics_or_a_confirmed_official_source(self):
        meeting_noon = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)                  # 12:00 KST on the meeting day.
        with closing(ledger.connect(self.db)) as conn:
            ledger.store_evidence(conn, [("ecos", "722Y001/0101000", date(2026, 10, 20), "2.75", None)], retrieved_at=T0)
            before_result = events.detect(conn, CONFIG, meeting_noon - timedelta(hours=2), meeting_noon - timedelta(days=1))
            found = events.detect(conn, CONFIG, meeting_noon, meeting_noon - timedelta(days=1))
        self.assertEqual(before_result, [])                                                 # 10:00: the result is not out.
        self.assertEqual([e.key for e, _ in found], ["bok_decision:2026-10-22"])
        # No statistics yet and the research cannot confirm: the note waits (pending), nothing is sent.
        self.assertEqual([o["detail"] for o in self.check(meeting_noon, researched([], UNKNOWN))], ["unconfirmed"])
        self.assertEqual(self.sent, [])
        # A hold the Bank's page states, dated on the meeting day: the vote and the guidance as the page words them.
        quote = "물가오름세가 목표 수준까지 안정적으로 수렴한다는 확신이 들 때까지 계속 대응하겠습니다."
        hold = {"outcome": "hold", "new_rate_pct": "2.75", "previous_rate_pct": "2.75", "guidance_quote": quote,
                "source_url": "https://www.bok.or.kr/x"}
        official = item("한국은행 통화정책방향 결정(동결)", "https://www.bok.or.kr/x", tier="official", decision_source=True)
        outcomes = self.check(meeting_noon + timedelta(minutes=15), researched([official], hold, HOLD_PAGE))
        self.assertEqual([o["status"] for o in outcomes], ["sent"])
        subject, html, _ = self.sent[0]
        self.assertEqual(subject, "[Smith · 사건 안내] 10/22 금통위 동결 2.75% · 월 이자 변화 없음")
        for needle in ("만장일치", "“물가오름세가", "한국은행 원문", "다음 금통위", "11월 26일", "0.25%p당 월 4만 원", "2.0억 원"):
            self.assertIn(needle, html)
        self.assertNotIn("한국은행 통화정책방향 결정(동결)", html)          # The decision's source item is not a finding.
        with closing(ledger.connect_read_only(self.db)) as conn:
            self.assertEqual(ledger.event_note(conn, "bok_decision:2026-10-22")["attempts"], 2)

    def test_the_statistics_decide_when_they_have_the_meeting_day(self):
        later = datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc)
        with closing(ledger.connect(self.db)) as conn:
            ledger.store_evidence(conn, [("ecos", "722Y001/0101000", date(2026, 10, 20), "2.75", None),
                                         ("ecos", "722Y001/0101000", date(2026, 10, 22), "3", None)], retrieved_at=T0)
        outcomes = self.check(later, None)                                                  # No research at all.
        self.assertEqual([o["status"] for o in outcomes], ["sent"])
        subject, html, _ = self.sent[0]
        # 200,000,000 x 0.25% / 12 = 41,667 won a month more, from the next repricing.
        self.assertEqual(subject, "[Smith · 사건 안내] 10/22 금통위 +0.25%p 인상 3% · 월 이자 +4만 원")
        for needle in ("이전 2.75%", "한국은행 ECOS 통계", "다음 재산정부터 약 +4만 원"):
            self.assertIn(needle, html)
        self.assertNotIn("만장일치", html)                              # No page read: no vote, no guidance.
        self.assertNotIn("향후 방향", html)

    def test_the_page_not_the_model_states_the_decision(self):
        meeting = date(2026, 10, 22)
        hold = events.read_decision(HOLD_PAGE, meeting, quote="계속 대응하겠습니다")
        # The first decision sentence decides; the recap of the last hike later on the page does not.
        self.assertEqual((hold["outcome"], hold["rate_after"], hold["rate_before"], hold["dated"], hold["vote_unanimous"]),
                         ("hold", Decimal("2.75"), Decimal("2.75"), True, True))
        self.assertFalse(hold["quote_found"])                          # Too short to prove anything.
        long_quote = "“물가오름세가 목표 수준까지 안정적으로 수렴한다는 확신이 들 때까지 계속 대응하겠습니다”"
        self.assertTrue(events.read_decision(HOLD_PAGE, meeting, quote=long_quote)["quote_found"])
        paraphrase = "물가가 잡힐 때까지 긴축을 이어가겠다고 말했습니다"
        self.assertFalse(events.read_decision(HOLD_PAGE, meeting, quote=paraphrase)["quote_found"])
        hike = events.read_decision(HIKE_PAGE, meeting)
        self.assertEqual((hike["outcome"], hike["rate_before"], hike["rate_after"], hike["vote_unanimous"]),
                         ("hike", Decimal("2.75"), Decimal("3.00"), None))
        self.assertFalse(events.read_decision(HOLD_PAGE, date(2026, 8, 27))["dated"])     # Another meeting's page.
        dissent = "소수의견이 있었습니다. 기준금리를 현재의 2.75%로 유지"
        self.assertIs(events.read_decision(dissent, meeting)["vote_unanimous"], False)
        # The Bank's statement words it differently from the press conference (the live page of 2026-10-22).
        statement = ("통화정책방향(2026.10.22) □ 금융통화위원회는 다음 통화정책방향 결정시까지 한국은행 기준금리를 현재의 3.00% "
                     "수준에서 유지하여 통화정책을 운용하기로 하였다 . □ 금번 기준금리 동결 결정에 대해서는 금융통화위원 7 명 모두 "
                     "찬성하였다 .")
        read = events.read_decision(statement, meeting)
        self.assertEqual((read["outcome"], read["rate_after"], read["dated"], read["vote_unanimous"]),
                         ("hold", Decimal("3.00"), True, True))
        older = "한국은행 기준금리를 현 수준(2.50%)에서 유지하여 … 아무개 위원은 0.25%p 인하하자는 소수의견을 개진하였다"
        read = events.read_decision(older, meeting)
        self.assertEqual((read["outcome"], read["rate_after"], read["vote_unanimous"]), ("hold", Decimal("2.50"), False))
        talk = "기준금리를 현재의 3.00%로 유지 … 흔히 소수의견을 내면서 소통한다는 그런 방식도"   # Not a recorded dissent.
        self.assertIsNone(events.read_decision(talk, meeting)["vote_unanimous"])
        # A questioner's "만장일치가 나왔는데" is not a vote; a page of menu words states no decision.
        self.assertIsNone(events.read_decision("질 문 - 오늘 만장일치가 나왔는데", meeting)["vote_unanimous"])
        self.assertIsNone(events.read_decision("한국은행 보도자료 목록 2026.10.22 인상 유지 동결", meeting)["outcome"])

    def test_confirmed_decision_needs_the_page_to_agree(self):
        hike = {"outcome": "hike", "new_rate_pct": "3.00", "previous_rate_pct": "2.75", "guidance_quote": "",
                "source_url": "https://www.bok.or.kr/x"}
        page = {"url": "https://www.bok.or.kr/x", "official": True, **events.read_decision(HIKE_PAGE, date(2026, 10, 22))}
        accepted = events.confirmed_decision({"items": [], "decision": hike, "decision_check": page}, Decimal("2.75"))
        self.assertEqual((accepted["outcome"], accepted["change"], accepted["vote_unanimous"], accepted["guidance"]),
                         ("hike", Decimal("0.25"), None, ""))
        self.assertEqual(events.confirmed_decision({"decision": hike, "decision_check": page}, None)["rate_before"],
                         Decimal("2.75"))                               # Without statistics the page anchors the step.
        unrelated = {**page, **events.read_decision("한국은행 보도자료 목록 2026.10.22", date(2026, 10, 22))}
        off_grid = {**page, "rate_after": Decimal("2.80")}
        too_far = {**page, "rate_after": Decimal("4.00")}
        for name, found, before in (
                ("model says hold", {"decision": {**hike, "outcome": "hold"}, "decision_check": page}, Decimal("2.75")),
                ("model rate differs", {"decision": {**hike, "new_rate_pct": "3.25"}, "decision_check": page}, Decimal("2.75")),
                ("statistics disagree", {"decision": hike, "decision_check": page}, Decimal("2.50")),
                ("not official", {"decision": hike, "decision_check": {**page, "official": False}}, Decimal("2.75")),
                ("another day", {"decision": hike, "decision_check": {**page, "dated": False}}, Decimal("2.75")),
                ("unrelated page", {"decision": hike, "decision_check": unrelated}, Decimal("2.75")),
                ("off-grid", {"decision": {**hike, "new_rate_pct": "2.80"}, "decision_check": off_grid}, Decimal("2.75")),
                ("too far", {"decision": {**hike, "new_rate_pct": "4.00"}, "decision_check": too_far}, Decimal("2.75")),
                ("no page", {"decision": hike, "decision_check": None}, Decimal("2.75")),
                ("unknown", {"decision": {**hike, "outcome": "unknown"}, "decision_check": page}, Decimal("2.75"))):
            with self.subTest(name=name):
                self.assertIsNone(events.confirmed_decision(found, before))

    def test_notes_are_retried_a_bounded_number_of_times(self):
        meeting_noon = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)
        statuses = [self.check(meeting_noon + timedelta(hours=i), researched([], UNKNOWN))[0]["status"] for i in range(4)]
        self.assertEqual(statuses, ["pending", "pending", "pending", "skipped"])
        self.assertEqual(self.sent, [])

    def test_a_claim_is_atomic_and_fences_a_run_that_lost_it(self):
        # Two overlapping runs (review 2026-10-23: both sent): the second sees the first's claim and stops.
        key, day, at = "bok_decision:2026-10-22", date(2026, 10, 22), datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)

        def claim(conn, now):
            return ledger.claim_event_note(conn, key=key, kind="bok_decision", event_date=day, now=now,
                                           lease=events.BUILDING, max_attempts=events.MAX_ATTEMPTS)
        with closing(ledger.connect(self.db)) as conn:
            first, outcome, _ = claim(conn, at)
            self.assertEqual((outcome, claim(conn, at + timedelta(minutes=1))[:2]), ("claimed", (None, "in-progress")))
            # The first run stalls past the lease; a later run takes the note over, and the stale run can no longer
            # mark it sending (so it never sends).
            second, outcome, _ = claim(conn, at + events.BUILDING + timedelta(minutes=1))
            self.assertIsNotNone(second)
            self.assertFalse(ledger.advance_event_note(conn, key=key, token=first, from_status="pending", status="unknown",
                                                       error_code="sending"))
            self.assertTrue(ledger.advance_event_note(conn, key=key, token=second, from_status="pending", status="unknown",
                                                      error_code="sending"))
            self.assertEqual(ledger.event_note(conn, key)["attempts"], 2)

    def test_a_run_that_dies_after_the_mail_request_never_sends_again(self):
        # The note was marked sending, then the process died before recording the outcome.
        key, at = "bok_decision:2026-10-22", datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)
        with closing(ledger.connect(self.db)) as conn:
            ledger.store_evidence(conn, [("ecos", "722Y001/0101000", date(2026, 10, 20), "2.75", None),
                                         ("ecos", "722Y001/0101000", date(2026, 10, 22), "2.75", None)], retrieved_at=T0)
            token, _, _ = ledger.claim_event_note(conn, key=key, kind="bok_decision", event_date=date(2026, 10, 22), now=at,
                                                  lease=events.BUILDING, max_attempts=events.MAX_ATTEMPTS)
            ledger.advance_event_note(conn, key=key, token=token, from_status="pending", status="unknown", error_code="sending")
        later = self.check(at + timedelta(hours=2))
        self.assertEqual([o["status"] for o in later], ["already-unknown"])
        self.assertEqual(self.sent, [])

    def test_a_move_whose_note_failed_is_sent_at_the_next_check(self):
        # Review 2026-10-23: the next check measures from the failed one, so the move was never detected again.
        calls = []

        def flaky(credentials, *, recipient, subject, html, text):
            calls.append(subject)
            if len(calls) == 1:
                raise gmail.MailError("http-400")
            return "msg-2"

        def run(now, reason):
            return events.check(self.db, CONFIG, now=now, reason=reason, sync=False, sender=flaky,
                                recipient="me@example.com", credentials=("a", "b", "c"))
        self.assertEqual([(o["status"], o["detail"]) for o in run(T1, "slot")], [("failed", "http-400")])
        self.assertEqual([o["status"] for o in run(T1 + timedelta(minutes=15), "manual")], ["sent"])
        self.assertEqual(calls, ["[Smith · 사건 안내] 10/06 보유 종목 -6.4% (-53만 원)"] * 2)   # The same measured move.
        with closing(ledger.connect_read_only(self.db)) as conn:
            note = ledger.event_note(conn, "portfolio_move:2026-10-06:pm")
        self.assertEqual((note["status"], note["attempts"], note["details"]["window"]["from"]),
                         ("sent", 2, (T1 - timedelta(days=1)).isoformat()))

    def test_the_daily_check_runs_once_after_its_time(self):
        day = datetime(2026, 10, 6, tzinfo=KST)
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(events.slot_due(conn, CONFIG, day.replace(hour=16, minute=0)))
            slot = events.slot_due(conn, CONFIG, day.replace(hour=16, minute=20))
            self.assertEqual(slot, day.replace(hour=16, minute=10))
            ledger.record_event_check(conn, checked_at=day.replace(hour=16, minute=20), reason="slot", baseline=T0, events=[])
            self.assertIsNone(events.slot_due(conn, CONFIG, day.replace(hour=23, minute=0)))
            self.assertEqual(events.slot_due(conn, CONFIG, (day + timedelta(days=1)).replace(hour=16, minute=20)),
                             (day + timedelta(days=1)).replace(hour=16, minute=10))
            disabled = {**CONFIG, "events": {**CONFIG["events"], "enabled": False}}
            self.assertIsNone(events.slot_due(conn, disabled, (day + timedelta(days=1)).replace(hour=16, minute=20)))

    def test_the_events_table_is_validated(self):
        config_module._check_events({"enabled": True, "check_time": "16:10", "portfolio_move_pct": 2.5})
        for bad in ({"enabled": "yes"}, {"check_time": "4pm"}, {"portfolio_move_pct": 0}, {"portfolio_move_pct": True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config_module._check_events(bad)
        with self.assertRaises(ValueError):
            config_module._check_keys({"app": {}, "events": {"enabled": True, "threshold": 1}})


def setUpModule():  # noqa: N802 - unittest hook
    fast_ledger.install()


def tearDownModule():  # noqa: N802 - unittest hook
    fast_ledger.uninstall()


if __name__ == "__main__":
    unittest.main()
