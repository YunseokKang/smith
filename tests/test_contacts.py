import http.client
import json
import os
import tempfile
import time
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from smith import agenda, config, contacts, daily, daily_html, daily_qa, daily_writer, people

NOW = datetime(2026, 11, 19, 21, 0, tzinfo=timezone.utc)
DAY = date(2026, 11, 20)
SEOUL = ZoneInfo("Asia/Seoul")
# Synthetic People API connections (shapes from people.connections.list with the Smith field mask).
JUNHO = {"resourceName": "people/c1001", "names": [{"displayName": "김준호"}], "nicknames": [{"value": "준호형"}],
         "birthdays": [{"date": {"month": 11, "day": 27}}],
         "biographies": [{"value": "아이 둘 민준·서연\n커피 안 마심, 010-1234-5678"}],
         "organizations": [{"name": "한빛상사", "title": "팀장"}],
         "memberships": [{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}}]}
AUNT = {"resourceName": "people/c1002", "names": [{"displayName": "이모"}], "biographies": [{"value": "음력 10월 1일 생신"}],
        "birthdays": [{"date": {"year": 1961, "month": 11, "day": 9}}],
        "events": [{"date": {"year": 1990, "month": 12, "day": 1}, "formattedType": "결혼기념일"}],
        "memberships": [{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/starred"}}]}
PLAIN = {"resourceName": "people/c1003", "names": [{"displayName": "김준"}], "birthdays": [{"date": {"month": 11, "day": 27}}]}
NAMELESS = {"resourceName": "people/c1004", "names": []}


def person(n, name, note="메모", month=11, day=27):
    return {"resourceName": f"people/c2{n:03d}", "names": [{"displayName": name}], "biographies": [{"value": note}],
            "birthdays": [{"date": {"month": month, "day": day}}]}


def getter_for(pages, calls=None):
    def get(url, headers):
        if calls is not None:
            calls.append(url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        return 200, pages[query.get("pageToken", "")]
    return get


def book_of(*persons):
    return [c for c in (contacts.parse(p) for p in persons) if c is not None]


def lunch(title):
    return agenda.Event("e1", title, datetime(2026, 11, 20, 12, 0, tzinfo=SEOUL), datetime(2026, 11, 20, 13, 0, tzinfo=SEOUL),
                        False, None, "", "default", True)


def assembled(found, events=(), policy="noted"):
    book = contacts.merge(people.parse({}), contacts.for_reminders(found, policy))
    inputs = daily.Inputs(None, list(events), [], book, None, [], {}, address_book=found)
    return daily.assemble(inputs, day=DAY, tz="Asia/Seoul", now=NOW, airways_sensitive=False)


class ParseAndFetchTests(unittest.TestCase):
    def test_parse_keeps_the_remembered_fields_only(self):
        junho, aunt = book_of(JUNHO, AUNT)
        self.assertEqual((junho.id, junho.name, junho.nicknames, junho.birthday, junho.organization, junho.starred),
                         ("c1001", "김준호", ("준호형",), (None, 11, 27), "한빛상사 · 팀장", False))
        self.assertIn("아이 둘 민준·서연 커피 안 마심", junho.note)
        self.assertNotIn("1234-5678", junho.note)                                   # Identifiers masked in the note.
        self.assertEqual((aunt.lunar_birthday, aunt.birthday, aunt.events, aunt.starred),
                         ((10, 1), (1961, 11, 9), (("결혼기념일", 1990, 12, 1),), True))
        self.assertIsNone(contacts.parse(NAMELESS))

    def test_fetch_reads_every_page_and_marks_a_cut_list_partial(self):
        calls = []
        found, complete = contacts.fetch("t", getter=getter_for({"": {"connections": [JUNHO, NAMELESS], "nextPageToken": "p2"},
                                                                 "p2": {"connections": [AUNT]}}, calls))
        self.assertEqual(([c.name for c in found], complete, len(calls)), (["김준호", "이모"], True, 2))
        fields = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(calls[0]).query))["personFields"].split(",")
        for absent in ("phoneNumbers", "emailAddresses", "addresses", "photos"):
            self.assertNotIn(absent, fields)
        endless = lambda url, headers: (200, {"connections": [], "nextPageToken": "more"})   # noqa: E731
        self.assertEqual(contacts.fetch("t", getter=endless), ([], False))           # Page limit: partial, not complete.

    def test_error_codes(self):
        def failing(status, body=None, raises=None):
            def get(url, headers):
                if raises is not None:
                    raise raises
                return status, body or {}
            return get
        legacy = {"error": {"errors": [{"reason": "accessNotConfigured"}]}}
        rpc = {"error": {"status": "PERMISSION_DENIED", "details": [{"reason": "SERVICE_DISABLED"}]}}
        scope = {"error": {"details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}
        for getter, code in ((failing(401), "login-required"), (failing(403, legacy), "api-not-enabled"),
                             (failing(403, rpc), "api-not-enabled"), (failing(403, scope), "no-scope"),
                             (failing(500), "http-500"), (failing(0, raises=OSError("down")), "network-error"),
                             (failing(0, raises=http.client.IncompleteRead(b"{")), "network-error"),
                             (failing(0, raises=ValueError("not json")), "bad-response")):
            with self.subTest(code=code), self.assertRaises(contacts.ContactsError) as caught:
                contacts.fetch("t", getter=getter)
            self.assertEqual(caught.exception.code, code)


class StorageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.file = contacts.path(Path(folder.name) / "smith.db")

    def test_sync_failure_keeps_the_copy_and_says_so(self):
        self.assertTrue(contacts.sync_due(self.file, NOW))
        result = contacts.sync(self.file, "t", now=NOW, getter=getter_for({"": {"connections": [JUNHO, AUNT]}}))
        self.assertEqual((result["outcome"], result["count"]), ("synced", 2))
        self.assertEqual(contacts.load(self.file), book_of(JUNHO, AUNT))
        self.assertIsNone(contacts.health(contacts.read(self.file), NOW))
        later = NOW + timedelta(days=1)
        failed = contacts.sync(self.file, "t", now=later, getter=lambda url, headers: (401, {}))
        self.assertEqual(failed["error_code"], "login-required")
        stored = contacts.read(self.file)
        self.assertEqual((len(stored.contacts), stored.synced_at), (2, NOW))      # Yesterday's copy stays ...
        self.assertEqual(contacts.health(stored, later), "sync-login-required")    # ... and the mail says it is behind.
        self.assertFalse(contacts.sync_due(self.file, later + timedelta(hours=1)))   # Next try tomorrow, not every tick.
        self.assertEqual(contacts.health(stored, NOW + timedelta(days=4)), "sync-login-required")
        contacts.sync(self.file, "t", now=later, getter=getter_for({"": {"connections": [JUNHO]}}))
        self.assertIsNone(contacts.health(contacts.read(self.file), later))
        self.assertEqual(contacts.health(contacts.read(self.file), later + timedelta(days=4)), "stale")
        self.assertEqual([p.name for p in self.file.parent.iterdir() if p.suffix in (".tmp", ".lock")], [])

    def test_broken_or_partial_copies_are_reported_not_raised(self):
        for content, state in (('{"contacts": 1}', "invalid-file"), ("not json", "invalid-file"),
                               ('{"contacts": [1, {"id": "x"}]}', "invalid-file"),
                               ('{"synced_at": "2026-11-19T21:00:00+00:00", "complete": false, "contacts": []}', "partial")):
            with self.subTest(content=content):
                self.file.write_text(content, encoding="utf-8")
                self.assertEqual(contacts.health(contacts.read(self.file), NOW), state)
                self.assertEqual(contacts.load(self.file), [])

    def test_a_live_lock_skips_and_a_crashed_one_is_taken_over(self):
        lock = self.file.with_name(self.file.name + ".lock")
        lock.write_text("", encoding="utf-8")
        pages = getter_for({"": {"connections": [JUNHO]}})
        self.assertEqual(contacts.sync(self.file, "t", now=NOW, getter=pages)["outcome"], "busy")
        old = time.time() - contacts.LOCK_STALE_SECONDS - 5
        os.utime(lock, (old, old))
        self.assertEqual(contacts.sync(self.file, "t", now=NOW, getter=pages)["outcome"], "synced")
        self.assertFalse(lock.exists())


class MatchingTests(unittest.TestCase):
    def test_names_match_as_words_only(self):
        found = book_of(JUNHO, AUNT, PLAIN)
        for text, names in (("이모티콘 디자인 검토", []), ("이모네 김장", ["이모"]), ("김준호님과 점심", ["김준호"]),
                            ("준호형이랑 저녁", ["김준호"]), ("김준 결혼식", ["김준"]), ("김준호 팀장 점심", ["김준호"]),
                            ("이모이벤트 기획", []), ("김준호팀장 미팅", ["김준호"])):
            with self.subTest(text=text):
                self.assertEqual([c.name for c in contacts.mentioned(found, text)], names)

    def test_a_shared_name_picks_nobody(self):
        found = book_of(person(1, "김민수", "대학 동기"), person(2, "김민수", "거래처"), JUNHO)
        hits, ambiguous = contacts.matches(found, "김민수 아이 이름이 뭐였지?")
        self.assertEqual((hits, ambiguous), ([], ["김민수"]))
        self.assertEqual(contacts.about_events(found, ["김민수 점심"]), [[]])

    def test_search_needs_a_name_or_a_who_question(self):
        found = book_of(JUNHO, AUNT, PLAIN)
        self.assertEqual(contacts.search(found, "아이디어 추천해줘"), [])                   # A note's chance overlap.
        self.assertEqual(contacts.search(found, "아이 이름이 뭐였지?"), [])
        self.assertEqual([c.name for c in contacts.search(found, "김준호 아이 이름이 뭐였지?")], ["김준호"])
        self.assertEqual([c.name for c in contacts.search(found, "한빛상사 다니는 사람 누구였지?")], ["김준호"])
        row = contacts.for_model(contacts.search(found, "이모 결혼기념일"))[0]
        self.assertEqual((row["name"], row["birthday"], row["events"]), ("이모", "11-09 · 음력 10/1", ["결혼기념일 12-01"]))


class UseTests(unittest.TestCase):
    def test_reminder_policy_and_merged_birthday(self):
        found = book_of(JUNHO, AUNT, PLAIN)
        self.assertEqual([c.name for c in contacts.for_reminders(found, "starred")], ["이모"])
        self.assertEqual([c.name for c in contacts.for_reminders(found, "noted")], ["김준호", "이모"])
        self.assertEqual(len(contacts.for_reminders(found, "all")), 3)
        book = contacts.merge(people.parse({}), contacts.for_reminders(found, "noted"))
        due = {(r.occurrence.label, r.days_left) for r in people.reminders(book, DAY)}
        self.assertIn(("김준호 생일", 7), due)                                          # Solar 11/27, D-7.
        self.assertNotIn("김준 생일", {label for label, _ in due})                    # No note, not starred: quiet.
        lunar = [o for o in people.occurrences(book, DAY) if o.person_id == contacts.person_id(found[1]) and o.kind == "birthday"]
        self.assertEqual([o.note.split(" · ")[0] for o in lunar], ["음력 10월 1일"])  # The note's lunar date wins.

    def test_daily_mail_names_the_person_under_the_event(self):
        data = assembled(book_of(JUNHO, AUNT), [lunch("김준호 팀장 점심")])
        _, html, text = daily_html.render(data)
        self.assertIn("이 분은: <b>김준호</b> · 한빛상사 · 팀장 · 아이 둘 민준·서연", html)
        self.assertIn("  이 분은: 김준호 · 한빛상사", text)
        payload = daily.payload(data)
        self.assertEqual(payload["agenda"]["events"][0]["about"][0]["person"], "김준호")
        self.assertNotIn("contacts", payload)                                        # The address book never goes whole.

    def test_one_model_call_sees_at_most_three_people(self):
        found = book_of(*(person(n, f"사람{chr(0xAC00 + n)}", f"취향 {n}") for n in range(5)))
        data = assembled(found, [lunch(f"{found[4].name} 미팅")])
        self.assertGreater(len({r.occurrence.person_id for r in data["reminders"]}), 3)   # The mail itself shows them all.
        _, html, _ = daily_html.render(data)
        self.assertTrue(all(c.name in html for c in found))
        payload = daily.payload(data)
        named = {row.get("person") for key in ("reminders", "upcoming") for row in payload[key]} \
            | {a["person"] for e in payload["agenda"]["events"] for a in e.get("about", [])}
        self.assertLessEqual(len(named - {None}), contacts.MODEL_PEOPLE)
        notes = daily_writer.notes_by_reminder(data, [])
        self.assertLessEqual(len(notes), contacts.MODEL_PEOPLE)
        self.assertTrue(all(row["source"] == "주소록" for rows in notes.values() for row in rows))

    def test_writer_gets_the_contact_note_for_its_reminder(self):
        data = assembled(book_of(JUNHO))
        notes = daily_writer.notes_by_reminder(data, [])
        key = next(r.occurrence.key for r in data["reminders"] if r.occurrence.label == "김준호 생일")
        self.assertEqual(notes[key][0]["source"], "주소록")
        self.assertIn("커피 안 마심", notes[key][0]["note"])


class ReplyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "smith.db"
        contacts.save(contacts.path(self.db), book_of(JUNHO, AUNT, PLAIN), now=NOW)

    def ask(self, question, output, ctx=None):
        seen = []

        def call(text, system_prompt, schema, use_case, timeout, web):
            seen.append((use_case, web, json.loads(text)))
            return output, "0"
        result = daily_qa.answer(self.db, ctx or {"today": {}, "window": {}}, question, now=NOW, received_at=NOW, history=[],
                                 secrets=[], tz="Asia/Seoul", executable=Path("claude"), caller=call)
        return result, seen

    def test_named_contact_answers_and_memory_stays_clean(self):
        output = {"answer": {"text": "민준이와 서연이입니다.", "refs": []}, "follow_up": [],
                  "remember": [{"topic": "other", "fact": "김준호 팀장 아이는 민준·서연"},
                               {"topic": "family", "fact": "준호 아이는 민준과 서연이다"},
                               {"topic": "other", "fact": "아이는 둘이다"},
                               {"topic": "family", "fact": "와이프는 꽃을 좋아한다"}],
                  "research": {"needed": "no", "subject": "", "kind": "none", "queries": [], "anchors": []}}
        ctx = {"today": {"reminders": [{"label": "이모 생일", "person": "이모"}, {"label": "아버지 생신"}]}, "window": {}}
        result, seen = self.ask("김준호 아이 이름이 뭐였지?", output, ctx)
        sent = seen[0][2]
        self.assertEqual([c["name"] for c in sent["client_contacts"]], ["김준호"])
        self.assertEqual([r["label"] for r in sent["today"]["reminders"]], ["아버지 생신"])   # Other contacts cut.
        self.assertEqual(result["remember"], [("family", "와이프는 꽃을 좋아한다")])

    def test_an_address_book_answer_never_goes_to_the_web(self):
        output = {"answer": {"text": "찾아보겠습니다.", "refs": []}, "follow_up": [], "remember": [],
                  "research": {"needed": "yes", "subject": "김준호", "kind": "person",
                               "queries": ["김준호 한빛상사 팀장"], "anchors": ["한빛상사", "팀장"]}}
        result, seen = self.ask("김준호 팀장 자세히 조사해줘", output)
        self.assertEqual([(u, w) for u, w, _ in seen], [("daily-answer", False), ("daily-answer-refused", False)])
        self.assertIn("주소록에 이 분의 메모가 있어", result["memos"][0])
        _, seen = self.ask("오늘 만나는 분 자세히 조사해줘", output)               # The model named the contact itself.
        self.assertNotIn(True, [w for _, w, _ in seen])

    def test_a_shared_name_is_not_guessed(self):
        contacts.save(contacts.path(self.db), book_of(person(1, "김민수", "대학 동기"), person(2, "김민수", "거래처")), now=NOW)
        output = {"answer": {"text": "모르겠습니다.", "refs": []}, "follow_up": [], "remember": [],
                  "research": {"needed": "no", "subject": "", "kind": "none", "queries": [], "anchors": []}}
        result, seen = self.ask("김민수 아이 이름이 뭐였지?", output)
        self.assertEqual(seen[0][2]["client_contacts"], [])
        self.assertIn("둘 이상", result["memos"][0])


class ConfigTests(unittest.TestCase):
    def test_config_policy_is_validated(self):
        config._check_daily({"contact_reminders": "all"})
        with self.assertRaises(ValueError):
            config._check_daily({"contact_reminders": "everyone"})


if __name__ == "__main__":
    unittest.main()
