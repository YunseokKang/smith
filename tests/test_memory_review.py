"""The monthly memory check (FR-52): a retired or corrected line moves to the archive and no call sees it again, the
mail is due once a month from the first Sunday's slot, shows never-shown facts first and then the ones shown longest
ago, leaves the counsel topic out, and a reply's edits are checked against the numbered snapshot, applied once and
reported in code's own sentences. Synthetic memory and mailbox; nothing is sent."""
import base64
import json
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from smith import config as config_module, gmail, ledger, memory, memory_qa, memory_review, qa

try:
    import fast_ledger
except ImportError:   # Run as `python -m unittest tests.test_memory_review`.
    from tests import fast_ledger

SEOUL = ZoneInfo("Asia/Seoul")
ME = "me@example.com"
CONFIG = {"app": {"timezone": "Asia/Seoul"}, "mail": {"recipient": ME}, "memory_review": {"enabled": True, "size": 3}}
CREDS = ("a", "b", "c")
DECEMBER = date(2026, 12, 6)    # The first Sunday of December 2026.
JANUARY = date(2027, 1, 3)


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
        self.folder = memory.directory(self.db)
        for topic, text, day in (("property", "솔방울은 푸른마을아파트를 말한다.", date(2026, 10, 5)),
                                 ("plan", "2027년 봄에 월세로 옮길 계획이다.", date(2026, 10, 20)),
                                 ("preference", "결론부터 짧게 듣는 것을 좋아한다.", date(2026, 11, 1)),
                                 ("family", "어머니는 등산을 좋아하신다.", date(2026, 11, 10)),
                                 ("other", "주말에는 메일을 늦게 확인한다.", date(2026, 9, 1)),
                                 ("counsel", "상담에서만 말한 고민이 있다.", date(2026, 11, 12))):
            memory.add(self.folder, topic, text, on=day, source="메일")
        self.sent = []

    def sender(self, credentials, *, recipient, subject, html, text, **kwargs):
        self.sent.append({"recipient": recipient, "subject": subject, "html": html, "text": text, **kwargs})
        return f"memoryletter{len(self.sent):02d}"

    def send_month(self, day: date) -> dict:
        return memory_review.publish(self.db, CONFIG, now=at(day, 10, 5), day=day, trigger="scheduled", recipient=ME,
                                     credentials=CREDS, sender=self.sender)

    def snapshot(self, day: date) -> dict:
        with closing(ledger.connect_read_only(self.db)) as conn:
            return ledger.daily_snapshot(conn, memory_review.run_key(day))


class ReviseTests(Fixture):
    def fact(self, text: str) -> memory.Fact:
        return next(f for f in memory.load(self.folder) if f.text == text)

    def test_a_retired_line_moves_to_the_archive_and_no_call_sees_it_again(self):
        fact = self.fact("솔방울은 푸른마을아파트를 말한다.")
        self.assertEqual(memory.revise(self.folder, fact, replacement=None, on=DECEMBER, source="기억 점검"), "retired")
        self.assertNotIn(fact.text, [f.text for f in memory.load(self.folder, private=True)])
        archived = (self.folder / memory.ARCHIVE).read_text(encoding="utf-8")
        self.assertIn("2026-12-06 · 지움 · 원래 줄: 2026-10-05 · 메일 · 솔방울은 푸른마을아파트를 말한다.", archived)
        self.assertEqual(memory.revise(self.folder, fact, replacement=None, on=DECEMBER, source="기억 점검"), "missing")

    def test_a_correction_takes_the_lines_place_and_keeps_the_old_one(self):
        fact = self.fact("2027년 봄에 월세로 옮길 계획이다.")
        self.assertEqual(memory.revise(self.folder, fact, replacement="2027년 봄에 1203호 전세로 옮길 계획이다.", on=DECEMBER,
                                       source="기억 점검"), "replaced")
        plans = [f for f in memory.load(self.folder) if f.topic == "plan"]
        self.assertEqual([(f.text, f.recorded_on, f.source) for f in plans],
                         [("2027년 봄에 (동·호 생략) 전세로 옮길 계획이다.", DECEMBER, "기억 점검")])
        self.assertIn("고침 → 2027년 봄에 (동·호 생략) 전세로", (self.folder / memory.ARCHIVE).read_text(encoding="utf-8"))
        self.assertEqual(memory.revise(self.folder, plans[0], replacement="2027년 봄에 (동·호 생략) 전세로 옮길 계획이다",
                                       on=DECEMBER, source="기억 점검"), "unchanged")

    def test_a_held_lock_refuses_the_write_and_a_file_left_by_a_crash_does_not(self):
        wait, memory.LOCK_WAIT_SECONDS = memory.LOCK_WAIT_SECONDS, 0   # No real waiting in a unit test.
        self.addCleanup(setattr, memory, "LOCK_WAIT_SECONDS", wait)
        with memory._locked(self.folder):            # Another writer holds the OS lock on its own handle.
            with self.assertRaises(memory.MemoryBusy):
                memory.add(self.folder, "other", "새 사실", on=DECEMBER, source="대화")
        (self.folder / ".write.lock").write_bytes(b"x")   # A crashed run leaves the file, never the lock.
        self.assertIsNotNone(memory.add(self.folder, "other", "새 사실", on=DECEMBER, source="대화"))

    def test_a_decimal_point_makes_a_different_fact(self):
        memory.add(self.folder, "plan", "주택 예산은 1.5억 원이다.", on=DECEMBER, source="대화")
        fact = self.fact("주택 예산은 1.5억 원이다.")
        self.assertEqual(memory.revise(self.folder, fact, replacement="주택 예산은 15억 원이다.", on=DECEMBER,
                                       source="기억 점검"), "replaced")
        self.assertIsNotNone(memory.add(self.folder, "plan", "주택 예산은 1.5억 원이다.", on=DECEMBER, source="대화"))

class MonthlyMailTests(Fixture):
    def test_due_from_the_first_sunday_slot_until_the_month_is_sent(self):
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(memory_review.due(conn, CONFIG, at(DECEMBER, 9, 55)))
            self.assertEqual(memory_review.due(conn, CONFIG, at(DECEMBER, 10, 5)), DECEMBER)
            self.assertEqual(memory_review.due(conn, CONFIG, at(date(2026, 12, 28), 8, 0)), DECEMBER)   # Missed: later.
        lines = memory_review.run_due(self.db, CONFIG, now=at(date(2026, 12, 7), 8, 0), recipient=ME, credentials=CREDS,
                                      sender=self.sender)
        self.assertEqual(lines, ["memory memory:2026-12: sent"])
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(memory_review.due(conn, CONFIG, at(date(2026, 12, 20), 10, 0)))
        self.assertEqual(memory_review.run_due(self.db, CONFIG, now=at(date(2026, 12, 20), 10, 0), recipient=ME,
                                               credentials=CREDS, sender=self.sender), [])
        self.assertEqual(len(self.sent), 1)

    def test_newest_unseen_facts_lead_then_the_longest_unseen_and_counsel_stays_out(self):
        self.send_month(DECEMBER)
        first = self.snapshot(DECEMBER)
        shown = [i["text"] for i in first["facts"] if i["shown"]]
        self.assertEqual(sorted(shown), sorted(["어머니는 등산을 좋아하신다.", "결론부터 짧게 듣는 것을 좋아한다.",
                                                "2027년 봄에 월세로 옮길 계획이다."]))
        self.assertEqual([i["n"] for i in first["facts"]], [1, 2, 3, 4, 5])     # Shown first, then the rest.
        self.assertNotIn("상담에서만", json.dumps(first, ensure_ascii=False) + self.sent[0]["html"])
        self.assertIn("3개 확인", self.sent[0]["subject"])
        self.assertIn("기억하고 있는 것", self.sent[0]["html"])
        self.assertIn("(전체 5개)", self.sent[0]["text"])
        # January: a fact remembered since leads as new, then the two never shown.
        memory.add(self.folder, "plan", "1월 셋째 주에 가족 여행을 간다.", on=date(2026, 12, 20), source="메일")
        self.send_month(JANUARY)
        second = self.snapshot(JANUARY)
        new = [i["text"] for i in second["facts"] if i["new"]]
        self.assertEqual(new, ["1월 셋째 주에 가족 여행을 간다."])
        self.assertEqual(sorted(i["text"] for i in second["facts"] if i["shown"] and not i["new"]),
                         sorted(["솔방울은 푸른마을아파트를 말한다.", "주말에는 메일을 늦게 확인한다."]))
        self.assertIn("지난 점검 뒤 새로 기억한 것", self.sent[1]["html"])
        # February: everything has been shown once, so December's facts come back first.
        february = date(2027, 2, 7)
        self.send_month(february)
        third = {i["text"] for i in self.snapshot(february)["facts"] if i["shown"]}
        self.assertEqual(third, set(shown))

    def test_a_late_manual_send_dates_the_review_by_the_day_it_went_out(self):
        memory_review.publish(self.db, CONFIG, now=at(date(2026, 12, 20), 15, 0), day=DECEMBER, trigger="manual",
                              recipient=ME, credentials=CREDS, sender=self.sender)
        with closing(ledger.connect_read_only(self.db)) as conn:
            shown, last = memory_review.shown_history(conn, now=at(JANUARY, 10, 5))
        self.assertEqual(last, date(2026, 12, 20))          # Facts kept on Dec 7-20 were in that mail, so not "new" later.
        self.assertEqual(set(shown.values()), {date(2026, 12, 20)})

    def test_an_empty_memory_skips_the_month_without_mail(self):
        for path in self.folder.glob("*.md"):
            path.unlink()
        result = self.send_month(DECEMBER)
        self.assertEqual((result["status"], result["error_code"]), ("skipped", "no-memory"))
        self.assertEqual(self.sent, [])


class SettingTests(unittest.TestCase):
    def test_the_size_setting_is_validated(self):
        for size in (2, 31, "12", True):
            with self.subTest(size=size), self.assertRaises(ValueError):
                config_module._check_memory_review({"size": size})
        config_module._check_memory_review({"enabled": True, "weekday": "saturday", "time": "09:30", "size": 20})



def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def mail(message_id: str, thread_id: str, text: str, received: datetime) -> dict:
    headers = [{"name": "From", "value": f"<{ME}>"}, {"name": "Message-ID", "value": f"<{message_id}@mail>"}]
    return {"id": message_id, "threadId": thread_id, "labelIds": ["SENT"], "internalDate": str(int(received.timestamp() * 1000)),
            "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": b64(text)}}}


REPLY = "1번 지워 줘. 2번은 이제 전세로 바뀌었어. 새로 기억해 줘: 둘째 주 토요일엔 가족 모임이 있다. 혈압약도 먹기 시작했어."


class ReplyTests(Fixture):
    def setUp(self):
        super().setUp()
        self.send_month(DECEMBER)
        self.facts = {i["n"]: i["text"] for i in self.snapshot(DECEMBER)["facts"]}
        received = at(DECEMBER, 12, 0)
        self.threads = {"threadmemory": [mail("memoryletter01", "threadmemory", "", received),
                                         mail("memoryreply01", "threadmemory", REPLY, received)]}
        no = {"about_health": "no"}
        self.output = {"edits": [{"n": "1", "action": "delete", "text": "", **no},
                                 {"n": "2", "action": "replace", "text": f"{self.facts[2][:-1]}, 이제 전세로 바뀌었다.", **no},
                                 {"n": "99", "action": "delete", "text": "", **no}],
                       "additions": [{"topic": "plan", "fact": "둘째 주 토요일에는 가족 모임이 있다.", "whose": "client", **no},
                                     {"topic": "other", "fact": "혈압약을 먹기 시작했다.", "whose": "client",
                                      "about_health": "yes"}],
                       "unclear": ""}
        self.replies = []

    def reader(self, url, headers):
        if "/threads/" in url:
            thread_id = url.split("/threads/")[1].split("?")[0]
            return 200, {"id": thread_id, "messages": self.threads[thread_id]}
        wanted = url.split("/messages/")[1].split("?")[0]
        found = next(m for messages in self.threads.values() for m in messages if m["id"] == wanted)
        return 200, found if "format=full" in url else {"id": wanted, "threadId": found["threadId"]}

    def reply_sender(self, credentials, **kwargs):
        self.replies.append(kwargs)
        return f"answer{len(self.replies):04d}"

    def answering(self, args, **kwargs):
        body = {"subtype": "success", "is_error": False, "structured_output": self.output, "total_cost_usd": 0.01}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    def poll(self):
        now = at(DECEMBER, 13, 0)
        answerer = memory_qa.answerer(self.db, now=now, tz="Asia/Seoul", secrets=[], executable=Path("claude.exe"),
                                      runner=self.answering)
        return qa.poll(self.db, recipient=ME, credentials=CREDS, now=now, secrets=[], tz="Asia/Seoul",
                       build=lambda report: {}, executable=Path("claude.exe"), runner=self.answering,
                       sender=self.reply_sender, reader=self.reader,
                       poster=lambda url, body, headers: (200, {"access_token": "tok",
                                                                "scope": f"{gmail.SCOPE} {gmail.READ_SCOPE}"}),
                       memory_check=answerer)

    def test_a_reply_retires_corrects_and_adds_then_says_exactly_what_changed(self):
        results = self.poll()
        self.assertEqual([r["status"] for r in results], ["answered"])
        reply = self.replies[0]
        self.assertEqual((reply["recipient"], reply["thread_id"]), (ME, "threadmemory"))
        text = reply["text"]
        self.assertIn(f"1번을 지웠습니다: {self.facts[1]}", text)
        self.assertIn("2번을 고쳤습니다:", text)
        self.assertIn("새로 기억합니다: 둘째 주 토요일에는 가족 모임이 있다.", text)
        self.assertIn("손대지 않았습니다", text)          # The unknown number, asked back.
        self.assertIn("공용 기억 대신 건강 기록에 적었습니다: 혈압약을 먹기 시작했다.", text)
        remembered = [f.text for f in memory.load(self.folder)]
        self.assertNotIn(self.facts[1], remembered)
        self.assertNotIn(self.facts[2], remembered)
        self.assertIn("둘째 주 토요일에는 가족 모임이 있다.", remembered)
        self.assertFalse(any("혈압" in t for t in remembered))           # Health statements never enter shared memory.
        notes = (self.db.parent / "health" / "client-notes.md").read_text(encoding="utf-8")
        self.assertIn("혈압약을 먹기 시작했다.", notes)
        with closing(ledger.connect_read_only(self.db)) as conn:
            payload = json.loads(conn.execute("SELECT payload FROM advice_runs WHERE use_case = 'memory-answer'").fetchone()[0])
        self.assertEqual([f["n"] for f in payload["facts"]], [1, 2, 3, 4, 5])
        self.assertNotIn("상담에서만", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(self.poll(), [])                                 # Never answered twice.

    def question(self, text=REPLY):
        return SimpleNamespace(message_id="memoryreply01", report_id=memory_review.run_key(DECEMBER), text=text,
                               received_at=at(DECEMBER, 12))

    def answer_with(self, *outputs):
        """The answerer run once per output (a retry per extra output); returns (results, model calls)."""
        calls, results = [], []
        for output in outputs:
            def runner(args, output=output, **kwargs):
                calls.append(output)
                body = {"subtype": "success", "is_error": False, "structured_output": output, "total_cost_usd": 0.01}
                return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
            answerer = memory_qa.answerer(self.db, now=at(DECEMBER, 13), tz="Asia/Seoul", secrets=[],
                                          executable=Path("claude.exe"), runner=runner)
            results.append(answerer(self.question(), [])[2])
        return results, calls

    def test_a_retried_reply_is_answered_from_its_stored_plan_and_never_read_again(self):
        # Codex review 2026-11-15: a second reading phrased the same addition differently and it was added twice.
        reworded = {**self.output, "additions": [{"topic": "plan", "fact": "가족 모임은 둘째 주 토요일이다.",
                                                  "whose": "client", "about_health": "no"}]}
        (first, again), calls = self.answer_with(self.output, reworded)
        self.assertEqual(len(calls), 1)
        self.assertEqual([o["kind"] for o in first["outcomes"]], ["retired", "replaced", "added", "health-noted"])
        self.assertEqual(again["text"], first["text"])
        remembered = [f.text for f in memory.load(self.folder)]
        self.assertEqual(sum("가족 모임" in t for t in remembered), 1)
        # A run that applied everything and died before recording any outcome (Codex review 2026-11-15): the resumed
        # run finds each result in place, writes nothing again and reports what was done, without the model.
        with closing(ledger.connect(self.db)) as conn:
            saved = ledger.daily_snapshot(conn, memory_qa.PLAN_KEY.format("memoryreply01"))
            ledger.record_daily_snapshot(conn, run_key=memory_qa.PLAN_KEY.format("memoryreply01"), day=DECEMBER,
                                         created_at=at(DECEMBER, 13), snapshot={"plan": saved["plan"], "outcomes": []})
        (third,), calls = self.answer_with(reworded)
        self.assertEqual(calls, [])
        self.assertEqual(third["outcomes"], first["outcomes"])
        notes = (self.db.parent / "health" / "client-notes.md").read_text(encoding="utf-8")
        self.assertEqual(notes.count("혈압약을 먹기 시작했다."), 1)

    def test_health_statements_never_enter_the_shared_memory(self):
        # Codex review 2026-11-15: these passed a narrow medical word list.
        for text, health in (("당뇨병이 있다.", True), ("아내는 수유 중이다.", True), ("체중은 82kg이다.", True),
                             ("두통약을 자주 먹는다.", True), ("천식이 있다.", True), ("편두통이 있다.", True),
                             # Codex review round 4: a bare 암 and 뇌졸중, and a condition named only by its verb.
                             ("저는 암이 있다.", True), ("저는 뇌졸중을 앓았다.", True), ("작년에 희귀질환에 걸렸다.", True),
                             ("전세 계약은 2027년 3월에 끝난다.", False), ("식당 예약은 내가 맡는다.", False),
                             ("건강보험료는 회사가 절반을 낸다.", False), ("이사에 석 달이 걸렸다.", False),
                             ("여권을 재발급했다.", False), ("암호화폐는 사지 않는다.", False)):
            with self.subTest(text=text):
                self.assertIs(memory_qa.health_statement(text), health)
        # The model may call any of these the client's own: code still reads who the text is about.
        output = {"edits": [], "unclear": "", "additions": [
            {"topic": "profile", "fact": "당뇨병이 있다.", "whose": "client", "about_health": "no"},
            {"topic": "family", "fact": "아내는 수유 중이다.", "whose": "other", "about_health": "yes"},
            {"topic": "family", "fact": "엄마는 당뇨병이 있다.", "whose": "client", "about_health": "yes"},
            {"topic": "family", "fact": "제 어머니는 당뇨병이 있다.", "whose": "client", "about_health": "yes"},
            {"topic": "property", "fact": "전세 계약은 2027년 3월에 끝난다.", "whose": "client", "about_health": "no"}]}
        edits, additions, _ = memory_qa.plan(self.snapshot(DECEMBER), output, said="전세 계약은 2027년 3월에 끝나.")
        planned = {"edits": edits, "additions": additions, "unclear": "", "problems": []}
        kinds = [o["kind"] for o in memory_qa.apply(self.db, planned, on=DECEMBER)]
        self.assertEqual(kinds, ["health-noted", "health-other", "health-other", "health-other", "added"])
        remembered = " ".join(f.text for f in memory.load(self.folder, private=True))
        self.assertNotIn("당뇨", remembered)
        self.assertNotIn("수유", remembered)
        notes = (self.db.parent / "health" / "client-notes.md").read_text(encoding="utf-8")
        self.assertIn("- 2026-12-06 (메일 답장): 당뇨병이 있다.", notes)
        self.assertNotIn("수유", notes)
        self.assertNotIn("어머니", notes)
        self.assertNotIn("엄마", notes)

    def test_a_figure_the_client_did_not_write_and_edits_that_undo_each_other_are_refused(self):
        # Codex review 2026-11-15: "1.5억" read as "15억" would have become the client's word; "rewrite 1 as 2, delete 2"
        # emptied both lines while the answer said both were done.
        stored = self.snapshot(DECEMBER)
        no = {"about_health": "no"}
        output = {"unclear": "", "additions": [
            {"topic": "plan", "fact": "주택 예산은 15억 원이다.", "whose": "client", **no},
            {"topic": "plan", "fact": "주택 예산은 1.5억 달러다.", "whose": "client", **no},
            {"topic": "plan", "fact": "주택 예산은 1.5억 원이다.", "whose": "client", **no}],
            "edits": [{"n": "1", "action": "replace", "text": self.facts[2], **no},
                      {"n": "2", "action": "delete", "text": "", **no},
                      {"n": "3", "action": "replace", "text": "결론은 3줄로 듣고 싶다.", **no}]}
        edits, additions, problems = memory_qa.plan(stored, output, said="예산은 1.5억이야. 1번은 2번이랑 같으니 2번은 지워.")
        self.assertEqual([a["fact"] for a in additions], ["주택 예산은 1.5억 원이다."])
        self.assertEqual([(e["n"], e["new"]) for e in edits], [(2, None)])
        self.assertEqual(len(problems), 4)
        for text, sources, ok in (("예산은 1.5억이다.", ("1.5억",), True), ("예산은 1,500만 원이다.", ("1500만원",), True),
                                  ("예산은 15억이다.", ("1.5억",), False), ("예산은 1.5억 엔이다.", ("1.5억 원",), False),
                                  # Codex review round 4: a currency code or sign, a flipped sign, a changed period.
                                  ("예산은 1.5억 USD다.", ("1.5억 원",), False), ("예산은 $1.5억이다.", ("1.5억",), False),
                                  ("수익률은 +3%다.", ("수익률 -3%",), False), ("수익률은 3%다.", ("수익률 -3%",), False),
                                  ("만기는 3개월 뒤다.", ("만기 3년",), False), ("만기는 3년 뒤다.", ("만기 3년",), True),
                                  ("수익률은 -3%다.", ("수익률 −3%",), True), ("2020-05에 샀다.", ("2020-05 매수",), True),
                                  ("세금 3억 원을 냈다.", ("3억 세금",), True), ("1.5억 주택을 샀다.", ("1.5억 원",), True),
                                  ("예산은 120만 달러다.", ("120만 USD",), True), ("예산은 1.5억 원이다.", ("1.5억 엔",), False)):
            with self.subTest(text=text):
                self.assertIs(memory_qa.grounded(text, *sources), ok)

    def test_a_confirmation_alone_changes_nothing(self):
        (result,), _ = self.answer_with({"edits": [], "additions": [], "unclear": ""})
        self.assertEqual(result["text"], "모두 그대로 두겠습니다. 확인해 주셔서 감사합니다.")
        self.assertFalse((self.folder / memory.ARCHIVE).exists())

if __name__ == "__main__":
    unittest.main()
