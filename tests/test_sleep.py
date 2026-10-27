import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from smith import sleep, weather

TONIGHT = date(2026, 11, 19)
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
SEOUL = ZoneInfo("Asia/Seoul")


def nights(minutes=(440, 430, 450, 435, 445), beds=("23:40", "23:50", "23:30", "23:45", "23:35"), wakes=("06:30",) * 5):
    days = {}
    for back, (m, b, w) in enumerate(zip(minutes, beds, wakes)):
        days[(TONIGHT - timedelta(days=back)).isoformat()] = {"sleep_minutes": m, "bed_at": b, "wake_at": w,
                                                             "latency_minutes": 5, "in_bed_minutes": m + 15}
    return days


def plan(night=None, days=None, **kwargs):
    args = {"night": night, "now_obs": None, "days": nights() if days is None else days, "synced_at": NOW, "tonight": TONIGHT,
            "now": NOW, "first_event": None, "rest_day": False, "airways_sensitive": False, "air_bad": False} | kwargs
    return sleep.plan(**args)


class NightWeatherTests(unittest.TestCase):
    def test_night_spans_tonight_and_tomorrow_morning(self):
        tonight = weather.Forecast(TONIGHT, None, None, None, (), (), None, None, "",
                                   (weather.Hour(21, 9.0, 0, None, 2.0, 50.0), weather.Hour(22, 8.0, 0, None, 2.0, 45.0),
                                    weather.Hour(23, 7.0, 0, None, 3.0, 40.0)))
        morning = weather.Forecast(TONIGHT + timedelta(days=1), None, None, None, (), (), None, None, "",
                                   tuple(weather.Hour(h, 6.0 - h, 0, None, 2.0, 50.0) for h in range(0, 9)))
        report = weather.Report(morning, None, None, (), None, (), {}, "tomorrow", tonight)
        night = weather.night(report)
        self.assertEqual((night.hours, night.t_first, night.tmin, night.tmin_hour, night.rh_min), (9, 8.0, 0.0, 6, 40.0))
        self.assertIsNone(weather.night(weather.Report(morning, None, None, (), None, (), {})))   # A morning report.


class SleepPlanTests(unittest.TestCase):
    def test_bedroom_actions_follow_the_night(self):
        cases = ((weather.Night(9, 10.0, 3.0, 5, 40.0, 35.0, 2.0, ()), ("가습기", "두꺼운 이불", "18~20℃")),
                 (weather.Night(9, 8.0, -2.0, 6, 55.0, 50.0, 10.0, ()), ("가습기", "보일러 예약", "외풍")),   # Cold: heating dries.
                 (weather.Night(9, 28.0, 26.0, 5, 85.0, 80.0, 2.0, ()), ("제습기", "열대야")),
                 (weather.Night(9, 16.0, 13.0, 5, 60.0, 55.0, 2.0, ()), ()))                          # A mild night: nothing.
        for night, expected in cases:
            with self.subTest(night=night):
                lines = " ".join(plan(night).environment)
                self.assertEqual([word for word in expected if word in lines], list(expected))
                if not expected:
                    self.assertEqual(lines, "")
        self.assertIn("기관지", plan(cases[0][0], airways_sensitive=True).environment[0])
        self.assertIn("공기청정기", " ".join(plan(cases[3][0], air_bad=True).environment))

    def test_bedtime_counts_back_from_the_usual_wake_or_an_early_first_event(self):
        self.assertEqual((plan().bedtime, plan().wake, plan().wake_reason), ("23:00", "06:30", "평소 기상"))
        early = plan(first_event=datetime(2026, 11, 20, 7, 0, tzinfo=SEOUL))
        self.assertEqual((early.bedtime, early.wake, early.wake_reason), ("22:00", "05:30", "내일 첫 일정 07:00"))
        self.assertEqual(plan(first_event=datetime(2026, 11, 20, 7, 0, tzinfo=SEOUL), rest_day=True).wake, "06:30")
        short = plan(days=nights(minutes=(350, 360, 340, 355, 345)))
        self.assertEqual(short.bedtime, "22:30")                                    # Thirty minutes earlier after a short week.
        self.assertIn("평균 수면이 5시간 50분입니다. 권장 7~9시간보다 짧으니", " ".join(short.body))
        self.assertIsNone(plan(days=nights(minutes=(400, 390), beds=("23:40", "23:50"), wakes=("06:30", "06:30"))).bedtime)

    def test_patterns_from_the_recent_nights(self):
        irregular = plan(days=nights(beds=("22:20", "00:40", "23:30", "23:45", "23:35")))
        self.assertIn("22:20~00:40로 들쭉날쭉", " ".join(irregular.body))          # Across midnight, compared in one line.
        self.assertEqual(plan().body, ())                                           # A steady week: nothing to say.
        days = nights()
        for back in range(1, 29):
            days.setdefault((TONIGHT - timedelta(days=back)).isoformat(), {})
            days[(TONIGHT - timedelta(days=back)).isoformat()].update({"resting_hr": 60, "hrv_ms": 40.0})
        days[TONIGHT.isoformat()].update({"resting_hr": 64, "hrv_ms": 30.0})
        tired = plan(days=days)
        self.assertIn("덜 회복", " ".join(tired.body))
        self.assertIn("4주 중앙값보다 4회 높음", " ".join(tired.facts))

    def test_missing_or_stale_data_is_said(self):
        self.assertEqual(plan(days={}, synced_at=None).data_note, "수면 자료가 아직 없습니다(Fitbit 연결 뒤 채워집니다).")
        stale = plan(synced_at=NOW - timedelta(days=3))
        self.assertIn("갱신되지 않았습니다", stale.data_note)
        self.assertEqual(plan(night=None).facts[0], "밤 예보를 받지 못했습니다")


if __name__ == "__main__":
    unittest.main()
