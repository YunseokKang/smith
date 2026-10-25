import unittest
import urllib.parse
from datetime import date, datetime
from zoneinfo import ZoneInfo

from smith import agenda

TZ = "Asia/Seoul"
TODAY = date(2026, 10, 26)


def fake_getter(responses):
    """Serves canned (status, payload) by URL prefix and records every URL asked."""
    seen = []

    def getter(url, headers):
        seen.append((url, headers))
        for prefix, answer in responses.items():
            if url.startswith(prefix):
                return answer
        return 404, {}
    return getter, seen


EVENTS = {"items": [
    {"id": "e2", "status": "confirmed", "summary": "팀 회의", "start": {"dateTime": "2026-10-26T10:00:00+09:00"},
     "end": {"dateTime": "2026-10-26T11:00:00+09:00"},
     "description": "<p>참고 https://docs.example.com/x 연락 010-1234-5678 회의 ID: 123 456 7890 비밀번호: abcd</p>",
     "location": "경기 성남시 분당구 판교역로 166 3층"},
    {"id": "e1", "status": "confirmed", "summary": "휴가", "start": {"date": "2026-10-26"}, "end": {"date": "2026-10-28"}},
    {"id": "e3", "status": "confirmed", "summary": "치과", "start": {"dateTime": "2026-10-26T02:10:00Z"},
     "end": {"dateTime": "2026-10-26T02:40:00Z"}, "location": "분당 서울치과", "eventType": "fromGmail"},
    {"id": "e4", "status": "cancelled", "summary": "취소된 회의", "start": {"dateTime": "2026-10-26T15:00:00+09:00"},
     "end": {"dateTime": "2026-10-26T16:00:00+09:00"}},
    {"id": "e5", "status": "confirmed", "summary": "집중 시간", "start": {"dateTime": "2026-10-26T10:30:00+09:00"},
     "end": {"dateTime": "2026-10-26T12:00:00+09:00"}, "transparency": "transparent"},
]}
LISTS = {"items": [{"id": "list-a", "title": "내 할 일"}, {"id": "list-b", "title": "집"}]}
TASKS_A = {"items": [
    {"id": "t1", "title": "보고서 제출", "due": "2026-10-20T00:00:00.000Z", "status": "needsAction", "notes": "링크 https://x.y/z"},
    {"id": "t2", "title": "오늘 할 것", "due": "2026-10-26T00:00:00.000Z", "status": "needsAction"},
    {"id": "t3", "title": "기한 없음", "status": "needsAction"},
    {"id": "t4", "title": "끝난 것", "due": "2026-10-21T00:00:00.000Z", "status": "completed"},
    {"id": "t5", "title": "미래", "due": "2026-10-30T00:00:00.000Z", "status": "needsAction"},
]}
TASKS_B = {"items": [{"id": "t6", "title": "세금 납부", "due": "2026-10-24T00:00:00.000Z", "status": "needsAction"}]}


class EventTests(unittest.TestCase):
    def test_events_are_minimal_sanitized_and_ordered(self):
        getter, seen = fake_getter({agenda.CALENDAR_URL: (200, EVENTS)})
        found = agenda.day_events("tok", TODAY, tz=TZ, getter=getter)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(seen[0][0]).query))
        self.assertEqual((query["singleEvents"], query["orderBy"], query["fields"]), ("true", "startTime", agenda.EVENT_FIELDS))
        self.assertNotIn("attendees", query["fields"])
        self.assertEqual(query["timeMin"], "2026-10-26T00:00:00+09:00")
        self.assertEqual([e.title for e in found], ["휴가", "팀 회의", "집중 시간", "치과"])   # All-day first, then by time.
        meeting = found[1]
        self.assertEqual(meeting.description, "참고 연락 [식별정보 제거] [접속 정보 제거] [접속 정보 제거]")
        self.assertIsNone(meeting.location)                                   # A street address never travels.
        dentist = found[3]
        self.assertEqual((dentist.start, dentist.location, dentist.event_type),
                         (datetime(2026, 10, 26, 11, 10, tzinfo=ZoneInfo(TZ)), "분당 서울치과", "fromGmail"))
        self.assertEqual((found[0].all_day, found[0].end), (True, date(2026, 10, 28)))
        self.assertFalse(found[2].busy)

    def test_overlaps_gaps_and_payload(self):
        getter, _ = fake_getter({agenda.CALENDAR_URL: (200, EVENTS)})
        found = agenda.day_events("tok", TODAY, tz=TZ, getter=getter)
        self.assertEqual([(a.title, b.title, gap) for a, b, gap in agenda.tight_gaps(found)], [("팀 회의", "치과", 10)])
        self.assertEqual(agenda.overlaps(found), [])                          # The free block does not count.
        payload = agenda.for_model(found, [], TODAY)
        self.assertEqual(payload["events"][0], {"title": "휴가", "all_day": True, "from_gmail": False, "until": "2026-10-27"})
        self.assertEqual(payload["events"][3]["start"], "11:10")
        self.assertEqual(payload["tight_gaps"], [["팀 회의", "치과", 10]])
        self.assertNotIn("id", payload["events"][0])


class TaskTests(unittest.TestCase):
    def test_open_tasks_due_by_today_across_lists(self):
        getter, seen = fake_getter({f"{agenda.TASKS_URL}/users/@me/lists": (200, LISTS),
                                    f"{agenda.TASKS_URL}/lists/list-a/tasks": (200, TASKS_A),
                                    f"{agenda.TASKS_URL}/lists/list-b/tasks": (200, TASKS_B)})
        found = agenda.tasks("tok", TODAY, getter=getter)
        self.assertEqual([t.title for t in found], ["오늘 할 것", "세금 납부", "보고서 제출"])
        self.assertEqual([t.title for t in agenda.overdue(found, TODAY)], ["세금 납부", "보고서 제출"])
        self.assertEqual([t.title for t in agenda.due_today(found, TODAY)], ["오늘 할 것"])
        self.assertEqual(found[2].notes, "링크")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(seen[1][0]).query))
        self.assertEqual((query["showCompleted"], query["dueMax"]), ("false", "2026-10-27T00:00:00Z"))
        payload = agenda.for_model([], found, TODAY)
        self.assertEqual(payload["tasks_overdue"][0], {"title": "세금 납부", "due": "2026-10-24", "days_overdue": 2, "notes": ""})


class ErrorTests(unittest.TestCase):
    def test_errors_name_the_cause(self):
        cases = [((403, {"error": {"errors": [{"reason": "accessNotConfigured"}]}}), "api-not-enabled"),
                 ((403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}), "no-scope"),
                 ((401, {}), "login-required"), ((500, {}), "http-500")]
        for answer, code in cases:
            with self.subTest(code=code):
                getter, _ = fake_getter({agenda.CALENDAR_URL: answer})
                with self.assertRaises(agenda.AgendaError) as caught:
                    agenda.day_events("tok", TODAY, tz=TZ, getter=getter)
                self.assertEqual(caught.exception.code, code)

        def broken(url, headers):
            raise OSError("down")
        with self.assertRaises(agenda.AgendaError) as caught:
            agenda.tasks("tok", TODAY, getter=broken)
        self.assertEqual(caught.exception.code, "network-error")
        self.assertEqual(agenda.missing_scopes({agenda.CALENDAR_SCOPE}), [agenda.TASKS_SCOPE])

    def test_clean_and_place(self):
        self.assertEqual(agenda.clean("A &nbsp; <b>B</b> www.x.com/q C mail me@x.com", 50), "A B C mail [식별정보 제거]")
        self.assertEqual(agenda.clean("x" * 30, 10), "xxxxxxxxx…")
        self.assertEqual(agenda.clean("Zoom 비번: A_b#12 접속코드 abc-defg-hij 끝", 100), "Zoom [접속 정보 제거] [접속 정보 제거] 끝")
        self.assertEqual(agenda.clean("meeting code 123 4567 890, password=Qw/er.ty", 100), "[접속 정보 제거], [접속 정보 제거]")
        self.assertEqual(agenda.place("스타벅스 판교점"), "스타벅스 판교점")
        self.assertIsNone(agenda.place("회의실 3B"))
        self.assertIsNone(agenda.place(""))


if __name__ == "__main__":
    unittest.main()
