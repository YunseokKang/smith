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


ROUTINES = {"dates": [
    {"id": "transfer-a", "label": "가족 용돈 이체", "kind": "routine", "day": 21, "start": "2024-08-21", "until": "2027-01-21"},
    {"id": "month-end", "label": "말일 납부", "kind": "routine", "day": "last"},
    {"id": "later", "label": "새 적립", "kind": "routine", "day": 5, "start": "2027-02-01"},
    {"id": "card", "label": "카드 출금", "kind": "routine", "day": 25},
]}
CHRISTMAS = date(2026, 12, 25)   # A Friday holiday in the synthetic holiday table.


class RoutineTests(unittest.TestCase):
    def setUp(self):
        self.book = people.with_holidays(people.parse(ROUTINES), [CHRISTMAS], [2026, 2027])

    def test_monthly_days_move_to_the_next_business_day_inside_start_and_until(self):
        rows = people.occurrences(self.book, date(2026, 11, 1), 120)
        on = {o.key: (o.on, o.note) for o in rows if o.kind == "routine"}
        self.assertEqual(on["routine:transfer-a:2026-11-21"], (date(2026, 11, 23), "11/21(토)이 주말이라 다음 평일"))
        self.assertEqual(on["routine:transfer-a:2026-12-21"], (date(2026, 12, 21), None))
        self.assertNotIn("routine:transfer-a:2027-02-21", on)                     # None after until.
        self.assertEqual(on["routine:card:2026-12-25"], (date(2026, 12, 28), "12/25(금)이 공휴일이라 다음 평일"))
        self.assertEqual(on["routine:month-end:2026-11-30"], (date(2026, 11, 30), None))       # "last" = 말일.
        self.assertEqual(on["routine:month-end:2027-01-31"][0], date(2027, 2, 1))              # Sunday 31st.
        self.assertEqual([o.on for o in rows if o.label == "새 적립"], [date(2027, 2, 5)])         # Not before start.
        lapse = [o for o in rows if o.kind == "routine_end"]
        self.assertEqual([(o.on, o.label) for o in lapse], [(date(2027, 1, 21), "가족 용돈 이체 만료")])
        self.assertEqual(people.upcoming(self.book, date(2026, 11, 15)), [])    # The quiet strip leaves routines out.
        # A Saturday's transfer that moved to Monday shows in a window starting on that Monday.
        self.assertIn("routine:transfer-a:2026-11-21", [o.key for o in people.occurrences(self.book, date(2026, 11, 23), 0)])
        # Without holiday data the weekend rule still holds, and the note says the holidays were not known.
        bare = {o.key: o for o in people.occurrences(people.parse(ROUTINES), date(2026, 12, 1), 31)}
        self.assertEqual(bare["routine:card:2026-12-25"].on, CHRISTMAS)
        self.assertEqual(bare["routine:card:2026-12-25"].note, "공휴일 자료 없음: 주말만 반영")
        # Holidays must be known as far ahead as the longest routine reminder asks, plus the room a day can move.
        self.assertEqual(people.routine_reach(self.book), 30 + people.SHIFT_MARGIN + 1)
        longer = people.parse({"dates": [{"id": "r", "label": "x", "kind": "routine", "day": 1, "remind_days": [60]}]})
        self.assertEqual(people.routine_reach(longer), 60 + people.SHIFT_MARGIN + 1)

    def test_reminders_a_week_before_the_day_before_the_day_and_before_the_lapse(self):
        cases = {date(2026, 11, 16): {("가족 용돈 이체", 7)}, date(2026, 11, 22): {("가족 용돈 이체", 1)},
                 date(2026, 11, 23): {("가족 용돈 이체", 0)}, date(2026, 11, 21): set(),
                 date(2026, 12, 22): {("가족 용돈 이체 만료", 30)},
                 date(2027, 1, 14): {("가족 용돈 이체", 7), ("가족 용돈 이체 만료", 7)},
                 date(2027, 1, 20): {("가족 용돈 이체", 1), ("가족 용돈 이체 만료", 1)},
                 date(2027, 2, 14): set()}                                      # Lapsed: no more reminders.
        for today, expected in cases.items():
            with self.subTest(today=today):
                found = {(r.occurrence.label, r.days_left) for r in people.reminders(self.book, today)
                         if people.is_routine(r.occurrence) and r.occurrence.label.startswith("가족")}
                self.assertEqual(found, expected)

    def test_routine_validation(self):
        cases = (({"id": "r", "label": "x", "kind": "routine"}, "day is required"),
                 ({"id": "r", "label": "x", "kind": "routine", "day": 32}, "from 1 to 31"),
                 ({"id": "r", "label": "x", "kind": "routine", "day": 1, "month": 2}, "not month"),
                 ({"id": "r", "label": "x", "kind": "routine", "day": 1, "until": "2027-13-01"}, "until must be"),
                 ({"id": "r", "label": "x", "kind": "routine", "day": 1, "start": "2027-01-02", "until": "2027-01-01"},
                  "before start"),
                 ({"id": "r", "label": "x", "kind": "expiry", "date": "2027-01-01", "until": "2027-02-01"},
                  "belong to a routine"))
        for raw, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(people.PeopleError) as caught:
                    people.parse({"dates": [raw]})
                self.assertIn(message, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
