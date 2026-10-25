import tempfile
import unittest
from datetime import date
from pathlib import Path

from smith import people

BOOK = {
    "people": [
        {"id": "father", "label": "아버지", "relation": "father",
         "birth": {"calendar": "lunar", "year": 1960, "month": 9, "day": 4}},
        {"id": "wife", "label": "와이프", "relation": "spouse",
         "birth": {"calendar": "solar", "year": 1989, "month": 2, "day": 22}},
        {"id": "nephew", "label": "조카", "relation": "nephew",
         "birth": {"calendar": "solar", "year": 2025, "month": 11, "day": 6}},
        {"id": "friend", "label": "친구", "relation": "friend", "birth": {"calendar": "solar", "month": 2, "day": 29}},
    ],
    "dates": [
        {"id": "wedding", "label": "결혼기념일", "kind": "anniversary", "month": 8, "day": 28, "year": 2021},
        {"id": "passport", "label": "여권 만료", "kind": "expiry", "date": "2026-11-24", "remind_days": [60, 30]},
    ],
    "occasions": [{"id": "white-day", "disabled": True}],
}


class LunarTests(unittest.TestCase):
    def test_known_public_dates_cross_check_the_tables(self):
        # 설날 and 추석 are published every year; they anchor the conversion against an independent fact.
        self.assertEqual(people.lunar_to_solar(2026, 1, 1), (date(2026, 2, 17), None))
        self.assertEqual(people.lunar_to_solar(2026, 8, 15), (date(2026, 9, 25), None))
        self.assertEqual(people.lunar_to_solar(2025, 1, 1), (date(2025, 1, 29), None))

    def test_intercalary_year_short_month_and_range(self):
        on, note = people.lunar_to_solar(2025, 6, 17)      # 2025 has a leap 6th month: the plain one counts.
        self.assertEqual((on, note), (date(2025, 7, 11), "윤달 있는 해, 평달 기준"))
        on, note = people.lunar_to_solar(2026, 2, 30)      # A 29-day month: the 30th falls back to the 29th.
        self.assertEqual((on, note), (date(2026, 4, 16), "작은달이라 29일 기준"))
        self.assertIsNone(people.lunar_to_solar(2051, 1, 1))
        self.assertEqual(people.solar_day(2027, 2, 29), (date(2027, 2, 28), "평년이라 2월 28일 기준"))


class BookTests(unittest.TestCase):
    def setUp(self):
        self.book = people.parse(BOOK)

    def test_occurrences_carry_age_milestone_and_notes(self):
        rows = {o.key: o for o in people.occurrences(self.book, date(2026, 10, 1), 60)}
        father = rows["birthday:father:2026"]
        self.assertEqual((father.on, father.label, father.years, father.milestone, father.note, father.remind_days),
                         (date(2026, 10, 14), "아버지 생신", 66, None, "음력 9월 4일", (7, 0)))
        nephew = rows["birthday:nephew:2026"]
        self.assertEqual((nephew.on, nephew.milestone, nephew.remind_days), (date(2026, 11, 6), "돌", (30, 7, 0)))
        self.assertEqual(rows["expiry:passport"].remind_days, (60, 30))
        self.assertNotIn("occasion:white-day:2026", rows)                    # Disabled in the file.

    def test_korean_age_milestones_and_anniversaries(self):
        rows = {o.key: o for o in people.occurrences(self.book, date(2026, 1, 1), 365 * 6)}
        self.assertEqual(rows["birthday:father:2029"].milestone, "칠순")       # Korean age 70 in 2029 (born 1960).
        self.assertIsNone(rows["birthday:father:2028"].milestone)
        self.assertEqual((rows["anniversary:wedding:2026"].years, rows["anniversary:wedding:2026"].milestone), (5, None))
        self.assertEqual((rows["anniversary:wedding:2031"].years, rows["anniversary:wedding:2031"].milestone,
                          rows["anniversary:wedding:2031"].remind_days), (10, "10주년", (30, 7, 0)))
        self.assertIsNone(rows["birthday:friend:2027"].years)                 # No birth year: no age, no milestone.
        self.assertEqual(rows["birthday:friend:2027"].on, date(2027, 2, 28))
        self.assertEqual(rows["occasion:seollal:2026"].on, date(2026, 2, 17))
        self.assertEqual(rows["occasion:chuseok:2026"].on, date(2026, 9, 25))

    def test_reminders_fall_on_the_agreed_days_and_the_payload_hides_birth_dates(self):
        cases = {date(2026, 10, 7): {"조카 생일": 30, "아버지 생신": 7}, date(2026, 10, 30): {"조카 생일": 7},
                 date(2026, 11, 6): {"조카 생일": 0}, date(2026, 10, 25): {"여권 만료": 30},
                 date(2026, 10, 14): {"아버지 생신": 0}, date(2026, 9, 14): {}}
        for today, expected in cases.items():
            with self.subTest(today=today):
                found = people.reminders(self.book, today)
                self.assertEqual({r.occurrence.label: r.days_left for r in found}, expected)
        payload = people.for_model(people.reminders(self.book, date(2026, 10, 30)))
        self.assertEqual(payload, [{"label": "조카 생일", "kind": "birthday", "date": "2026-11-06", "days_left": 7,
                                    "milestone": "돌", "years": 1}])
        today = people.for_model(people.reminders(self.book, date(2026, 11, 6)))
        self.assertNotIn("years", today[0])                                   # Age only in the gift window.

    def test_validation_names_the_broken_entry(self):
        cases = (
            ({"people": [{"id": "Father", "label": "아버지"}]}, "id must be lowercase"),
            ({"people": [{"id": "a", "label": "x"}, {"id": "a", "label": "y"}]}, "id used twice: a"),
            ({"people": [{"id": "a", "label": "아버지", "name": "홍길동"}]}, "unknown key(s) name"),
            ({"people": [{"id": "a", "label": "아버지", "birth": {"calendar": "solar", "month": 4, "day": 31}}]},
             "day is out of range"),
            ({"dates": [{"id": "d", "label": "x", "kind": "birthday", "month": 1, "day": 1}]}, "kind must be one of"),
            ({"dates": [{"id": "d", "label": "x", "kind": "expiry", "month": 1, "day": 1}]}, "takes one date"),
            ({"dates": [{"id": "d", "label": "x", "kind": "occasion", "month": 1, "day": 1, "person_id": "nobody"}]},
             "not in people"),
            ({"occasions": [{"id": "d", "label": "x", "kind": "expiry", "date": "2027-01-01"}]}, "kind is occasion"),
            ([], "one JSON object"),
        )
        for raw, message in cases:
            with self.subTest(message=message):
                with self.assertRaises((people.PeopleError, ValueError)) as caught:
                    people.parse(raw)
                self.assertIn(message, str(caught.exception))

    def test_save_is_atomic_and_rejects_a_bad_document(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "people.json"
            self.assertEqual(people.load(file), people.parse({}))              # No file: the defaults only.
            people.save(file, BOOK)
            self.assertEqual(len(people.load(file).people), 4)
            with self.assertRaises(people.PeopleError):
                people.save(file, {"people": [{"id": "x", "label": ""}]})
            self.assertEqual(len(people.load(file).people), 4)                 # The old file survived the bad save.
            self.assertFalse(file.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
