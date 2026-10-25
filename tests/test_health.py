"""The health ledger (FR-39): validation, series by method, follow-ups per track, the dates that may leave the health
thread, medicine interpretation notes and idempotent imports. Synthetic values throughout."""
import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from smith import health, people

TODAY = date(2026, 10, 26)
OBS = [
    {"code": "ldl", "value": "118", "unit": "mg/dL", "ref": "0~129", "on": "2021-09-06", "flag": "normal", "method": "direct",
     "source": "2023 결과지 비교 열 p.5"},
    {"code": "ldl", "value": "152", "ref": "0~129", "on": "2026-07-07", "flag": "high", "method": "direct", "source": "2026 결과지 p.6"},
    {"code": "ldl", "value": "146", "ref": "0~129", "on": "2026-07-07", "flag": "high", "method": "calculated", "source": "2026 결과지 p.7"},
    {"code": "psa", "value": "0.62", "on": "2025-06-27", "flag": "normal", "source": "2025 결과지 p.9"},
    {"code": "hs_crp", "value": "<0.5", "on": "2025-06-27", "source": "2025 결과지 p.9"},
    {"code": "hbs_ab", "value": "양성 (785.00)", "on": "2025-06-27", "flag": "normal", "source": "2025 결과지 p.9"},
    {"code": "hav_ab", "value": "음성", "on": "2023-10-16", "flag": "attention", "source": "2023 결과지 p.9"},
]
FINDINGS = [
    {"id": "egd-2023", "area": "위", "summary": "위축성 위염", "on": "2023-10-16", "track": "egd", "follow_up_months": 12,
     "recommendation": "1년 뒤 위내시경", "severity": "follow_up", "source": "2023 결과지 p.12"},
    {"id": "egd-2025", "area": "위", "summary": "위축성 위염, 변화 없음", "on": "2025-06-27", "track": "egd", "follow_up_months": 12,
     "severity": "follow_up", "source": "2025 결과지 p.12"},
    {"id": "eye-2025", "area": "눈", "summary": "우측 안압 높음", "on": "2025-06-27", "track": "eye", "follow_up_months": 1,
     "severity": "see_doctor", "source": "2025 결과지 p.3", "resolved_on": "2025-08-01", "resolved_note": "재측정 정상(고객 진술)"},
    {"id": "echo-2025", "area": "심장", "summary": "삼첨판 역류 경도", "on": "2025-06-27", "track": "echo", "follow_up_months": 30,
     "severity": "follow_up", "source": "2025 결과지 p.10"},
    {"id": "liver-2023", "area": "간", "summary": "경증 지방간", "on": "2023-10-16", "track": "liver-us", "follow_up_months": 12,
     "severity": "follow_up", "source": "2023 결과지 p.11"},
    {"id": "liver-2025", "area": "간", "summary": "정상", "on": "2025-06-27", "track": "liver-us", "severity": "normal",
     "source": "2025 결과지 p.11"},
    {"id": "spine-2024", "area": "척추", "summary": "경추 전만 감소", "on": "2024-03-01", "track": "spine-xray", "follow_up_months": 12,
     "severity": "follow_up", "source": "외부 영상 p.1"},
    {"id": "uric-2025", "area": "대사", "summary": "요산 경도 상승", "on": "2025-06-27", "track": "uric-acid", "follow_up_months": 12,
     "severity": "follow_up", "source": "2025 결과지 p.4"},
    {"id": "carotid-left-2025", "area": "혈관", "summary": "좌총경동맥 변화 없음", "on": "2025-06-27", "track": "carotid-us",
     "severity": "normal", "source": "2025 결과지 p.10"},
    {"id": "carotid-right-2025", "area": "혈관", "summary": "우경동맥 두께 증가, 유의한 변화 없음", "on": "2025-06-27", "track": "carotid-us",
     "follow_up_months": 30, "severity": "follow_up", "source": "2025 결과지 p.10"},
]
REGIMEN = [{"name": "탈모약", "ingredient": "dutasteride", "frequency": "weekly", "since": "2018-06"},
           {"name": "종합 비타민", "frequency": "daily"}]
ROUTINE = [{"activity": "테니스", "per_week": 2, "minutes": "30~40"}, {"activity": "산책", "per_week": "1~2", "minutes": [30, 40]}]
POSTURE = [{"code": "forward_head_deg", "value": "30", "on": "2023-05-23", "device": "exbody 리포트 2023", "source": "리포트 p.1"},
           {"code": "forward_head_deg", "value": "7", "on": "2025-06-30", "device": "exbody 화면 2025", "source": "사진"}]
RAW = {"observations": OBS, "findings": FINDINGS, "regimen": REGIMEN, "routine": ROUTINE, "posture": POSTURE}


class LedgerTests(unittest.TestCase):
    def test_parse_series_by_method_and_printed_text(self):
        ledger = health.parse(RAW)
        direct = health.series(ledger, "ldl", "direct")
        self.assertEqual([o.value for o in direct], [Decimal("118"), Decimal("152")])
        self.assertEqual(len(health.series(ledger, "ldl")), 3)
        self.assertEqual(health.latest(ledger, "ldl").method, "calculated")   # Same day; both kept, never joined.
        crp = health.latest(ledger, "hs_crp")
        self.assertEqual((crp.value, crp.text, crp.flag, crp.unit), (Decimal("0.5"), "<0.5", "unknown", "mg/L"))
        antibody = health.latest(ledger, "hbs_ab")                      # Qualitative result with the titre in brackets.
        self.assertEqual((antibody.value, antibody.text), (Decimal("785.00"), "양성 (785.00)"))
        self.assertEqual((health.latest(ledger, "hav_ab").value, health.latest(ledger, "hav_ab").text), (None, "음성"))
        with self.assertRaises(health.HealthError):                       # Only qualitative codes may lack a number.
            health.parse({"observations": [{"code": "ldl", "value": "높음", "on": "2026-01-01", "source": "s"}]})
        grade = health.parse({"posture": [{"code": "overall_grade", "value": "6단계", "on": "2023-05-23", "device": "d", "source": "s"}]})
        self.assertEqual((grade.posture[0].value, grade.posture[0].text), (None, "6단계"))
        flat = health.parse({"genetics": [{"trait": "요산", "area": "건강관리", "grade": "주의", "source": "s"}], "types": ["A형"],
                             "on": "2024-12-13", "area_summary": {"건강관리": "주의 54점"}})
        self.assertEqual((flat.genetics[0].grade, flat.genetics_types, flat.genetics_on), ("주의", ("A형",), date(2024, 12, 13)))
        self.assertEqual(ledger.routine[1].per_week_max, 2)
        self.assertEqual((ledger.routine[0].minutes_min, ledger.routine[0].minutes_max), (30, 40))
        notes = health.check(ledger)
        self.assertTrue(any("ldl" in n and "방식" in n for n in notes))
        self.assertTrue(any("자세 측정 장비" in n for n in notes))
        self.assertTrue(any("spine-xray" in n for n in notes))

    def test_rules_reject_unknown_codes_identifiers_and_bad_values(self):
        bad = [
            ({"observations": [{"code": "ldl_x", "value": "1", "on": "2026-01-01", "source": "s"}]}, "unknown code"),
            ({"observations": [{"code": "ldl", "value": "high", "on": "2026-01-01", "source": "s"}]}, "number"),
            ({"observations": [{"code": "ldl", "value": "1", "on": "2026-01-01"}]}, "source"),
            ({"observations": [{"code": "ldl", "value": "1", "on": "2026-01-01", "source": "s", "patient": "x"}]}, "unknown key"),
            ({"findings": [{"id": "Bad Id", "area": "a", "summary": "s", "on": "2026-01-01", "source": "s"}]}, "id"),
            ({"findings": [FINDINGS[0], FINDINGS[0]]}, "duplicate"),
            ({"findings": [{**FINDINGS[0], "severity": "fatal"}]}, "severity"),
            ({"posture": [{"code": "forward_head_deg", "value": "1", "on": "2026-01-01", "source": "s"}]}, "device"),
            ({"routine": [{"activity": "x", "per_week": "2~1", "minutes": 30}]}, "per_week"),
            ({"genetics": {"genetics": [{"trait": "t", "area": "a", "source": "s"}]}}, "genetics.on"),
        ]
        for raw, word in bad:
            with self.subTest(word=word):
                with self.assertRaises(health.HealthError) as caught:
                    health.parse(raw)
                self.assertIn(word, str(caught.exception))

    def test_follow_ups_take_the_latest_finding_per_track_and_skip_resolved(self):
        ledger = health.parse(RAW)
        due = {f.track: f for f in health.follow_ups(ledger, TODAY)}
        self.assertEqual(due["egd"].due, date(2026, 6, 27))              # From the 2025 finding, not 2023.
        self.assertEqual(due["egd"].days_left, -121)                     # Overdue, said as such.
        self.assertEqual(due["echo"].due, date(2027, 12, 27))            # 30 months after 2025-06-27.
        self.assertNotIn("eye", due)                                     # Resolved by the client.
        self.assertNotIn("liver-us", due)                                # The latest finding on the track is normal.
        self.assertIn("spine-xray", due)                                 # Unknown track still tracked in the letter...
        self.assertEqual(due["uric-acid"].label, "요산")                  # A lab track: in the letter, overdue here.
        # Two readings on one day: the one asking for a follow-up speaks for the day, whatever the file order.
        self.assertEqual((due["carotid-us"].finding.id, due["carotid-us"].due), ("carotid-right-2025", date(2027, 12, 27)))
        labels = {d.id: d.label for d in health.as_dates(ledger, TODAY)}
        self.assertEqual(labels, {"health-echo": "심장초음파 추적", "health-carotid-us": "경동맥 초음파 추적",
                                  "health-checkup": "건강검진(지난 검진 1년 뒤)"})
        # ...but never exported as a date (unknown or lab track), and the overdue one is not the morning mail's business.
        self.assertNotIn("health-spine-xray", labels)
        self.assertNotIn("health-uric-acid", labels)
        self.assertNotIn("health-egd", labels)
        book = health.merge_dates(people.parse({}), ledger, TODAY)
        soon = [o for o in people.occurrences(book, TODAY, 500) if o.kind == "expiry"]
        self.assertEqual({o.label for o in soon}, {"심장초음파 추적", "경동맥 초음파 추적", "건강검진(지난 검진 1년 뒤)"})
        self.assertEqual(soon[0].remind_days, (30, 7))
        self.assertEqual(health.add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(health.add_months(date(2025, 6, 27), 30), date(2027, 12, 27))

    def test_medicine_notes_cover_only_values_on_file_and_flag_other_frequency(self):
        ledger = health.parse(RAW)
        notes = health.interpretation_notes(ledger)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["codes"], ["psa"])
        self.assertIn("2배", notes[0]["text"])
        self.assertIn("주 1회", notes[0]["text"])          # Weekly use vs the label's daily studies.
        self.assertIn("라벨", notes[0]["source"])
        daily = health.parse({**RAW, "regimen": [{**REGIMEN[0], "frequency": "daily"}]})
        self.assertNotIn("빈도", health.interpretation_notes(daily)[0]["text"])
        without_psa = health.parse({**RAW, "observations": OBS[:3]})
        self.assertEqual(health.interpretation_notes(without_psa), [])


class ImportTests(unittest.TestCase):
    def test_import_is_idempotent_and_a_conflict_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "health"
            first = health.import_batch(folder, RAW)
            self.assertEqual(first.added["observations"], 7)
            # The same value printed again in a later report's comparison column is the same record, whatever the page.
            copy = health.import_batch(folder, {"observations": [{**OBS[0], "source": "2025 결과지 비교 열 p.5", "flag": "unknown"}]})
            self.assertEqual((copy.added["observations"], copy.skipped["observations"]), (0, 1))
            resolved = health.resolve(folder, "egd-2025", date(2026, 9, 1), "재검 정상(고객 진술)")
            self.assertEqual(resolved.resolved_on, date(2026, 9, 1))
            self.assertNotIn("egd", {f.track for f in health.follow_ups(health.load(folder), TODAY)})
            with self.assertRaises(health.HealthError):
                health.resolve(folder, "no-such", date(2026, 9, 1), "x")
            self.assertEqual(health.load(folder).findings[0].on, date(2023, 10, 16))   # Sorted by date on load.
            again = health.import_batch(folder, {"observations": OBS[:2], "regimen": [{**REGIMEN[1], "note": "센트룸"}]})
            self.assertEqual((again.added["observations"], again.skipped["observations"]), (0, 2))
            self.assertEqual(again.added["regimen"], 1)                           # The client's statement replaces.
            self.assertEqual(health.load(folder).regimen[1].note, "센트룸")
            before = (folder / "observations.json").read_text(encoding="utf-8")
            with self.assertRaises(health.HealthError) as caught:
                health.import_batch(folder, {"observations": [{**OBS[1], "value": "151"}],
                                             "findings": [{**FINDINGS[0], "summary": "다른 소견"}]})
            self.assertIn("ldl/2026-07-07/direct", str(caught.exception))
            self.assertIn("findings egd-2023", str(caught.exception))
            self.assertEqual((folder / "observations.json").read_text(encoding="utf-8"), before)
            with self.assertRaises(health.HealthError):
                health.import_batch(folder, {"notes": []})
            with self.assertRaises(health.HealthError):                           # Validated before any write.
                health.import_batch(folder, {"observations": [{"code": "nope", "value": "1", "on": "2026-01-01", "source": "s"}]})
            self.assertEqual(health.load(folder).observations, health.load(folder).observations)
            self.assertEqual(health.load(Path(directory) / "missing"), health.Ledger())

    def test_import_writes_all_sections_or_none_and_respects_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            health.import_batch(folder, {"observations": OBS[:2], "findings": FINDINGS[:1]})
            before = {name: (folder / f"{name}.json").read_text(encoding="utf-8") for name in ("observations", "findings")}
            real_replace = health.os.replace

            def failing_replace(src, dst):   # The second section's replace fails: the first must not stand alone.
                if str(dst).endswith("findings.json"):
                    raise OSError("disk full")
                return real_replace(src, dst)
            temps_written = []
            real_write_text = Path.write_text

            def spy_write_text(self, *args, **kwargs):
                if self.name.endswith(".json.tmp"):
                    temps_written.append(self.name)
                return real_write_text(self, *args, **kwargs)
            health.os.replace = failing_replace
            Path.write_text = spy_write_text
            try:
                with self.assertRaises(OSError):
                    health.import_batch(folder, {"observations": OBS[2:4], "findings": FINDINGS[1:2]})
            finally:
                health.os.replace = real_replace
                Path.write_text = real_write_text
            self.assertEqual(sorted(temps_written), ["findings.json.tmp", "observations.json.tmp"])   # Both staged first...
            self.assertEqual((folder / "findings.json").read_text(encoding="utf-8"), before["findings"])
            lock = folder / ".write.lock"
            lock.write_text("", encoding="utf-8")
            with self.assertRaises(health.HealthError) as caught:
                health.import_batch(folder, {"observations": OBS[:1]})
            self.assertIn("lock", str(caught.exception))
            with self.assertRaises(health.HealthError):
                health.resolve(folder, "egd-2023", date(2026, 9, 1), "x")
            lock.unlink()
            self.assertEqual(health.import_batch(folder, {"observations": OBS[:1]}).skipped["observations"], 1)
            self.assertFalse(lock.exists())

    def test_single_report_sections_replace_only_when_identical(self):
        micro = {"microbiome": {"on": "2025-07-25", "lab": "Lab", "scores": [{"code": "overall", "label": "종합", "value": "57",
                                                                                "korean_mean": 50, "grade": "보통"}]}}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            health.import_batch(folder, micro)
            self.assertEqual(health.import_batch(folder, micro).skipped["microbiome"], 1)
            other = json.loads(json.dumps(micro))
            other["microbiome"]["scores"][0]["value"] = "60"
            with self.assertRaises(health.HealthError):
                health.import_batch(folder, other)
            self.assertEqual(health.load(folder).microbiome.scores[0].korean_mean, "50")


if __name__ == "__main__":
    unittest.main()
