import json
import re
import tempfile
import unittest
import unittest.mock
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from smith import config, evernote, keep

NOW = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)   # 03:00 KST on 2 November.
TODAY = date(2026, 11, 2)
KST = timezone(timedelta(hours=9))
G = [f"{i:08x}-0000-0000-0000-000000000000" for i in range(1, 7)]
EXE = Path("aside.exe")


def line(kind: str, **body) -> str:
    return evernote.MARK + json.dumps({"kind": kind, **body}, ensure_ascii=False)


class FakeAside:
    """Answers the list and read scripts from a synthetic account; records each call's arguments."""

    def __init__(self, sources: dict[str, tuple[str, list[str], int]], bodies: dict[str, str], *,
                 unread: set[str] = frozenset(), stop_after: int | None = None, fail: str | None = None, page: int = 100) -> None:
        self.sources, self.bodies, self.unread, self.stop_after, self.fail = sources, bodies, set(unread), stop_after, fail
        self.page = page
        self.calls: list[dict] = []

    def __call__(self, args: list[str], timeout: float) -> tuple[int, str]:
        code = args[2]
        sent = json.loads(re.search(r"const ARGS = (.*);\n", code).group(1))
        self.calls.append(sent)
        if self.fail == "silent":
            return 1, "Aside is not running"
        if self.fail:
            return 0, line("error", code=self.fail)
        if "async function collect" in code:
            name = sent["name"]
            kind, guids, expected = self.sources[name]
            # A source listed in pages of `page` rows: each call reports where it stopped until the last.
            first = int(sent["start"] or 0)
            part = guids[first:first + self.page]
            resume = first + self.page if first + self.page < len(guids) else None
            out = [line("listed", source=name, source_kind=kind, guid=g, title=f"t-{g[:8]}") for g in part]
            summary = line("source", name=name, source_kind=kind, guid=G[5], expected=None if first else expected,
                           found=len(part), resume=resume)
            return 0, "\n".join(out + [summary, "[ok | 9ms]"])
        out = []
        for index, want in enumerate(sent["notes"]):
            if self.stop_after is not None and index == self.stop_after:
                out.append(line("stopped", guid=want["guid"]))
                self.stop_after = None
                break
            if want["guid"] in self.unread:
                out.append(line("unread", guid=want["guid"]))
                if getattr(self, "unread_once", False):
                    self.unread.discard(want["guid"])
                continue
            out.append(line("note", guid=want["guid"], title=want["title"], text=self.bodies[want["guid"]], updated="수정됨 10월 30일"))
        return 0, "\n".join(out)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "evernote"
        self.bodies = {g: f"나는 오늘 {g[:8]} 생각을 적었다. 노후와 여행 계획\n헤더 링크 복사" for g in G}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def sync(self, aside: FakeAside) -> dict:
        return evernote.sync(self.folder, ["Inbox"], ["Books"], now=NOW, today=TODAY, runner=aside, executable=EXE)

    def test_allowed_sources_are_read_and_stored_as_keep_notes(self):
        aside = FakeAside({"Inbox": ("notebook", G[:2], 2), "Books": ("space", G[2:4], 2)}, self.bodies)
        result = self.sync(aside)
        self.assertEqual((result["outcome"], result["notes"], result["read"]), ("synced", 4, 4))
        self.assertEqual([c.get("name") for c in aside.calls[:2]], ["Inbox", "Books"])
        stored = {n["guid"]: n for n in evernote.load_raw(self.folder)}
        self.assertNotIn("헤더 링크 복사", stored[G[0]]["text"])
        self.assertEqual(stored[G[0]]["updated"], "2026-10-30")
        all_notes = evernote.notes(self.folder)
        self.assertEqual({n.id for n in all_notes}, {f"evernote:{g}" for g in G[:4]})
        self.assertEqual({n.labels[0] for n in evernote.notes(self.folder, spaces=False)}, {"Inbox"})
        self.assertTrue(keep.search(all_notes, "노후 여행"))

    def test_unread_note_keeps_its_copy_and_short_listing_deletes_nothing(self):
        self.sync(FakeAside({"Inbox": ("notebook", G[:3], 3), "Books": ("space", G[3:5], 2)}, self.bodies))
        # Next night: G[0] does not open, Inbox lists one note short (G[2] missing), Books really lost G[4].
        changed = {**self.bodies, G[1]: "바뀐 내용입니다. 이번 주 생각을 다시 정리했다."}
        result = self.sync(FakeAside({"Inbox": ("notebook", G[:2], 3), "Books": ("space", G[3:4], 1)}, changed, unread={G[0]}))
        stored = {n["guid"]: n for n in evernote.load_raw(self.folder)}
        self.assertEqual(result["outcome"], "partial")
        self.assertIn(G[0], stored)                       # Unread: the earlier copy stands.
        self.assertIn("바뀐 내용", stored[G[1]]["text"])  # Read again: replaced.
        self.assertIn(G[2], stored)                       # Inbox listed short (2 of 3): nothing of it is dropped.
        self.assertNotIn(G[4], stored)                    # Books listed completely: the missing note is gone.

    def test_aside_failure_leaves_the_store_untouched(self):
        self.sync(FakeAside({"Inbox": ("notebook", G[:1], 1), "Books": ("space", [], 0)}, self.bodies))
        for fail, code in (("silent", "aside-failed"), ("not-signed-in", "not-signed-in")):
            with self.subTest(fail=fail):
                result = self.sync(FakeAside({}, {}, fail=fail))
                self.assertEqual((result["outcome"], result["error_code"]), ("error", code))
                self.assertEqual(len(evernote.load_raw(self.folder)), 1)
                self.assertEqual(evernote.sync_state(self.folder)["last_outcome"], "error")
                self.assertNotIn("running_since", evernote.sync_state(self.folder))

    def test_reads_that_only_kept_old_copies_are_partial_and_the_read_cap_deletes_nothing(self):
        self.sync(FakeAside({"Inbox": ("notebook", G[:3], 3), "Books": ("space", [], 0)}, self.bodies))
        result = self.sync(FakeAside({"Inbox": ("notebook", G[:3], 3), "Books": ("space", [], 0)}, self.bodies, unread=set(G)))
        self.assertEqual((result["outcome"], result["read"], result["unread"], result["kept"]), ("partial", 0, 3, 3))
        with unittest.mock.patch.object(evernote, "MAX_NOTES", 2):
            result = self.sync(FakeAside({"Inbox": ("notebook", G[:3], 3), "Books": ("space", [], 0)}, self.bodies))
        self.assertEqual((result["outcome"], result["read"], result["notes"]), ("partial", 2, 3))

    def test_a_long_list_is_read_over_calls_that_resume_where_the_last_stopped(self):
        aside = FakeAside({"Inbox": ("notebook", G[:5], 5), "Books": ("space", [], 0)}, self.bodies, page=2)
        result = self.sync(aside)
        self.assertEqual((result["outcome"], result["sources"]["Inbox"]["found"]), ("synced", 5))
        resumed = [c for c in aside.calls if c.get("name") == "Inbox"]
        self.assertEqual([(c["start"], c["guid"], c["have"], c["expected"]) for c in resumed],
                         [(0, None, 0, None), (2, G[5], 2, 5), (4, G[5], 4, 5)])

    def test_a_second_sync_is_refused_while_one_holds_the_lock(self):
        with evernote.claimed(self.folder) as mine:
            self.assertTrue(mine)
            self.assertTrue(evernote.running(self.folder))
            result = self.sync(FakeAside({}, {}))
            self.assertEqual((result["outcome"], result["error_code"]), ("busy", "already-running"))
        self.assertFalse(evernote.running(self.folder))

    def test_secrets_never_reach_a_model(self):
        bodies = {G[0]: "여행 예약 password:\nsynthetic-login-secret-xyz\n예약 상세",
                  G[1]: "여행 자금 계좌 번호: 12345678901234 로 모으기",
                  G[2]: "여행 자금은 9876 5432 1098 7654 카드로. 2026-11-01 출발, 바다를 보러 간다."}
        self.sync(FakeAside({"Inbox": ("notebook", G[:3], 3), "Books": ("space", [], 0)}, bodies))
        given = evernote.notes(self.folder)
        self.assertEqual([n.id for n in given], [f"evernote:{G[2]}"])        # Marked notes are held back whole.
        self.assertIn("[숫자 가림]", given[0].text)                            # An unmarked long number is masked,
        self.assertIn("2026-11-01", given[0].text)                            # a date is not.
        payload = json.dumps(keep.for_model(keep.search(given, "여행 자금", limit=5)), ensure_ascii=False)
        self.assertNotIn("9876", payload)
        for title, text, expected in (("여행 계획", "바다", False), ("은행 비밀번호", "x", True), ("메모", "비밀 번호는 x", True),
                                      ("메모", "Account number 1", True), ("ISA 계좌 개설 생각", "언젠가", False),
                                      ("예약", "Travel API key: synthetic-x", True), ("LLM", "token 수가 늘면 비용도 는다", False)):
            with self.subTest(text=text):
                self.assertEqual(evernote.sensitive(title, text), expected)
        for text in ("Travel payment: 1234 - 5678 - 9012 for it", "key sk_live_synthetic0123456789abcd here"):
            with self.subTest(text=text):
                self.assertNotRegex(evernote.masked(text), r"5678|0123456789")
        self.assertEqual(evernote.masked("2026-11-01 출발, 3박"), "2026-11-01 출발, 3박")

    def test_a_missed_note_is_retried_once_in_reverse_order(self):
        wanted = [evernote.Listed(g, "Inbox", "notebook", f"t-{g[:8]}") for g in G[:3]]
        aside = FakeAside({}, self.bodies, unread={G[1]})
        aside.unread_once = True
        got = evernote.read_notes(wanted, runner=aside, executable=EXE)
        self.assertEqual(set(got), set(G[:3]))
        self.assertEqual([n["guid"] for n in aside.calls[-1]["notes"]], [G[1]])

    def test_a_stopped_batch_continues_where_it_stopped(self):
        wanted = [evernote.Listed(g, "Inbox", "notebook", f"t-{g[:8]}") for g in G]
        aside = FakeAside({}, self.bodies, stop_after=2)
        got = evernote.read_notes(wanted, runner=aside, executable=EXE)
        self.assertEqual(set(got), set(G))
        self.assertEqual([n["guid"] for n in aside.calls[1]["notes"]][0], G[2])


class TextTests(unittest.TestCase):
    def test_clean_drops_editor_controls_and_hides_secret_lines(self):
        text = "여행 계획\n헤더 링크 복사\n\n\n\n은행 비밀번호 1234\nwifi PW: abcd\n불러오는 중…\n끝"
        self.assertEqual(evernote.clean(text), "여행 계획\n\n[가림: 민감 정보 줄]\n[가림: 민감 정보 줄]\n끝")

    def test_untitled_notes_skip_the_title_check(self):
        for listed, expected in ((None, None), ("제목 없음", ""), ("Untitled", ""), ("  여행   계획 ", "여행 계획")):
            with self.subTest(listed=listed):
                self.assertEqual(evernote.expected_title(listed), expected)

    def test_updated_line_dates(self):
        for label, expected in (("수정됨 2024년 12월 28일", date(2024, 12, 28)), ("수정됨 10월 30일", date(2026, 10, 30)),
                                ("수정됨 12월 30일", date(2025, 12, 30)), ("25. 12. 30.", date(2025, 12, 30)),
                                ("수정됨 5시간 전", TODAY), ("수정됨 어제", TODAY - timedelta(days=1)), ("", None), ("수정됨 2월 30일", None)):
            with self.subTest(label=label):
                self.assertEqual(evernote.parse_updated(label, TODAY), expected)


class ScheduleAndConfigTests(unittest.TestCase):
    def test_sync_is_due_once_a_night_after_its_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            at = time(3, 0)
            local = NOW.astimezone(KST)
            self.assertTrue(evernote.sync_due(folder, now=NOW, local_now=local, at=at))
            self.assertFalse(evernote.sync_due(folder, now=NOW, local_now=local.replace(hour=2), at=at))
            with evernote.claimed(folder):
                self.assertFalse(evernote.sync_due(folder, now=NOW, local_now=local, at=at))  # Another run is reading.
            evernote._write_state(folder, {"last_checked": NOW.isoformat(), "last_outcome": "partial"})
            self.assertFalse(evernote.sync_due(folder, now=NOW + timedelta(hours=10), local_now=local + timedelta(hours=10), at=at))
            self.assertTrue(evernote.sync_due(folder, now=NOW + timedelta(days=1), local_now=local + timedelta(days=1), at=at))
            evernote._write_state(folder, {"last_checked": NOW.isoformat(), "last_outcome": "error"})
            self.assertFalse(evernote.sync_due(folder, now=NOW + timedelta(minutes=30), local_now=local + timedelta(minutes=30), at=at))
            self.assertTrue(evernote.sync_due(folder, now=NOW + timedelta(hours=2), local_now=local + timedelta(hours=2), at=at))

    def test_nightly_sync_stays_out_of_the_mail_window_across_midnight(self):
        from smith import daily_command

        settings = {"app": {"timezone": "Asia/Seoul"}, "evernote": {"enabled": True, "time": "23:00", "notebooks": ["Inbox"]}}
        late = datetime(2026, 11, 1, 23, 15, tzinfo=KST)             # Next mail at 01:00 tomorrow: 105 minutes away.
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(evernote, "sync") as fake:
            fake.return_value = {"outcome": "synced", "notes": 0, "read": 0, "kept": 0, "unread": 0, "sources": {}, "error_code": None}
            db = Path(tmp) / "smith.db"
            today_slot = datetime(2026, 11, 1, 1, 0, tzinfo=KST)
            self.assertEqual(daily_command._sync_evernote(db, settings, late.astimezone(timezone.utc), today_slot), [])
            self.assertFalse(fake.called)
            daily_command._sync_evernote(db, settings, late.replace(hour=23, minute=0) - timedelta(hours=1, minutes=-30),
                                         datetime(2026, 11, 1, 6, 0, tzinfo=KST))
            self.assertFalse(fake.called)                             # 22:30 is before the 23:00 sync time.
            daily_command._sync_evernote(db, settings, late.astimezone(timezone.utc), datetime(2026, 11, 1, 6, 0, tzinfo=KST))
            self.assertTrue(fake.called)                              # 06:00 mail: 23:15 is clear of it.

    def test_config_section(self):
        config._check_evernote({"enabled": True, "time": "03:00", "notebooks": ["Inbox"], "spaces": ["독서", "여행"]})
        for bad in ({"enabled": True}, {"time": "3am"}, {"spaces": "독서"}, {"spaces": ["독서", "독서"]}, {"notebooks": [""]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config._check_evernote(bad)

    def test_load_all_adds_evernote_and_thoughts_leave_spaces_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "smith.db"
            item = {"text": "나는 이번 달 목표를 다시 생각했다. 매일 조금씩.", "title": "t", "updated": "2026-10-30"}
            evernote._write_notes(evernote.directory(db), [{**item, "guid": G[0], "source": "Inbox", "source_kind": "notebook"},
                                                           {**item, "guid": G[1], "source": "Books", "source_kind": "space"}], NOW)
            self.assertEqual(len(keep.load_all(db)), 2)
            self.assertEqual([n.id for n in keep.load_all(db, thoughts=True)], [f"evernote:{G[0]}"])


if __name__ == "__main__":
    unittest.main()
