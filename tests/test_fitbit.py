"""The Fitbit record through the Google Health API (FR-40): roll-ups and lists parsed into one summary per day (the
shapes from the API's discovery document), paging, a refused roll-up summed from the list endpoint, a failed source
leaving the others and its own old values standing (partial), storage merged by day, the week's summary against the
guideline with vigorous minutes counted twice, and the account-link check."""
import json
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

from smith import fitbit

NOW = datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)   # Monday evening (Seoul Tuesday 06:00).


def civil(day: date) -> dict:
    return {"date": {"year": day.year, "month": day.month, "day": day.day}, "time": {"hours": 0, "minutes": 0, "seconds": 0}}


ROLLUPS = {
    "steps": {"rollupDataPoints": [{"civilStartTime": civil(date(2026, 10, 26)), "steps": {"countSum": "8200"}},
                                   {"civilStartTime": civil(date(2026, 10, 27)), "steps": {"countSum": "11000"}}]},
    "active-minutes": {"rollupDataPoints": [
        {"civilStartTime": civil(date(2026, 10, 26)), "activeMinutes": {"activeMinutesRollupByActivityLevel": [
            {"activityLevel": "LIGHT", "activeMinutesSum": "120"}, {"activityLevel": "MODERATE", "activeMinutesSum": "25"},
            {"activityLevel": "VIGOROUS", "activeMinutesSum": "15"}]}},
        {"civilStartTime": civil(date(2026, 10, 27)), "activeMinutes": {"activeMinutesRollupByActivityLevel": [
            {"activityLevel": "VIGOROUS", "activeMinutesSum": "40"}]}}]},
}
EXERCISE_PAGE_1 = {"dataPoints": [
    {"name": "e1", "exercise": {"interval": {"startTime": "2026-10-26T01:00:00Z", "civilStartTime": civil(date(2026, 10, 26))},
                                "exerciseType": "TENNIS", "activeDuration": "2100s"}}], "nextPageToken": "p2"}
EXERCISE_PAGE_2 = {"dataPoints": [
    {"name": "e2", "exercise": {"interval": {"startTime": "2026-10-28T10:00:00Z", "civilStartTime": civil(date(2026, 10, 28))},
                                "exerciseType": "WEIGHTS", "displayName": "Weights", "activeDuration": "1800s"}}]}
LISTS = {
    "sleep": {"dataPoints": [
        {"name": "s1", "sleep": {"interval": {"startTime": "2026-10-25T14:30:00Z", "endTime": "2026-10-25T21:30:00Z",
                                              "civilEndTime": civil(date(2026, 10, 26))}, "type": "STAGES",
                                 "summary": {"minutesAsleep": "395", "minutesAwake": "25"},
                                 "stages": [{"type": "AWAKE", "startTime": "2026-10-25T16:00:00Z", "endTime": "2026-10-25T16:20:00Z"}]}},
        {"name": "s2", "sleep": {"interval": {"startTime": "2026-10-26T15:00:00Z", "endTime": "2026-10-26T22:00:00Z",
                                              "civilEndTime": civil(date(2026, 10, 27))}, "type": "STAGES",
                                 "stages": [{"type": "AWAKE", "startTime": "2026-10-26T17:00:00Z", "endTime": "2026-10-26T17:30:00Z"}]}}]},
    "daily-resting-heart-rate": {"dataPoints": [{"dailyRestingHeartRate": {"date": {"year": 2026, "month": 10, "day": 26}, "beatsPerMinute": "58"}}]},
    "daily-heart-rate-variability": {"dataPoints": [{"dailyHeartRateVariability": {"date": {"year": 2026, "month": 10, "day": 26},
                                                                                   "averageHeartRateVariabilityMilliseconds": 41.5}}]},
    "weight": {"dataPoints": [{"weight": {"sampleTime": {"civilTime": civil(date(2026, 10, 27)), "physicalTime": "2026-10-27T13:10:00Z"},
                                          "weightGrams": 61400}}]},
    "body-fat": {"error": {"code": 500, "message": "boom", "status": "INTERNAL"}},
    "daily-vo2-max": {"dataPoints": [{"dailyVo2Max": {"date": {"year": 2026, "month": 10, "day": 27}, "vo2Max": 41.2, "cardioFitnessLevel": "GOOD"}}]},
    "daily-oxygen-saturation": {"dataPoints": [{"dailyOxygenSaturation": {"date": {"year": 2026, "month": 10, "day": 26},
                                                                           "averagePercentage": 96.4, "lowerBoundPercentage": 93.0}}]},
    "daily-respiratory-rate": {"dataPoints": [{"dailyRespiratoryRate": {"date": {"year": 2026, "month": 10, "day": 26}, "breathsPerMinute": 14.8}}]},
    "daily-sleep-temperature-derivations": {"dataPoints": [{"dailySleepTemperatureDerivations": {
        "date": {"year": 2026, "month": 10, "day": 26}, "nightlyTemperatureCelsius": 33.9, "baselineTemperatureCelsius": 34.1}}]},
    "sedentary-period": {"dataPoints": [
        {"sedentaryPeriod": {"interval": {"startTime": "2026-10-26T00:00:00Z", "endTime": "2026-10-26T04:00:00Z", "civilStartTime": civil(date(2026, 10, 26))}}},
        {"sedentaryPeriod": {"interval": {"startTime": "2026-10-26T05:00:00Z", "endTime": "2026-10-26T10:30:00Z", "civilStartTime": civil(date(2026, 10, 26))}}}]},
}


def poster(url, body, headers):
    data_type = url.split("/dataTypes/")[1].split("/")[0]
    assert headers["Authorization"] == "Bearer tok"
    payload = json.loads(body)
    assert payload["windowSizeDays"] == 1 and payload["range"]["start"]["date"]["year"] == 2026
    return 200, ROLLUPS[data_type]


def getter(url, headers):
    data_type = url.split("/dataTypes/")[1].split("/")[0]
    query = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
    assert ">=" in query["filter"] and "AND" in query["filter"], query
    if data_type == "exercise":
        return 200, EXERCISE_PAGE_2 if query.get("pageToken") == "p2" else EXERCISE_PAGE_1
    if data_type == "body-fat":
        return 500, LISTS[data_type]
    return 200, LISTS.get(data_type, {"dataPoints": []})


class FetchTests(unittest.TestCase):
    def test_days_are_parsed_per_source_pages_followed_and_a_failed_source_noted(self):
        notes, failed = [], []
        days = fitbit.fetch_days("tok", date(2026, 10, 26), date(2026, 11, 1), getter=getter, poster=poster, notes=notes, failed=failed)
        mon, tue, wed = days["2026-10-26"], days["2026-10-27"], days["2026-10-28"]
        self.assertEqual((mon["steps"], tue["steps"]), (8200, 11000))
        self.assertEqual(mon["active_minutes_by_level"], {"LIGHT": 120, "MODERATE": 25, "VIGOROUS": 15})
        self.assertEqual((mon["exercise_minutes"], mon["strength"], mon["exercise_kinds"]), (35, False, ["TENNIS"]))
        self.assertEqual((wed["exercise_minutes"], wed["strength"]), (30, True))          # From the second page.
        self.assertEqual(mon["sleep_minutes"], 395)                  # From the summary, booked on the morning it ended.
        self.assertEqual(tue["sleep_minutes"], 390)                  # 7h minus a 30-minute awake stage.
        self.assertEqual((mon["resting_hr"], mon["hrv_ms"]), (58, 41.5))
        self.assertEqual(tue["weight_kg"], 61.4)
        self.assertEqual((tue["vo2_max"], tue["cardio_fitness"]), (41.2, "GOOD"))
        self.assertEqual((mon["spo2_avg"], mon["spo2_low"], mon["breaths_per_minute"], mon["skin_temp_delta_c"]), (96.4, 93.0, 14.8, -0.2))
        self.assertEqual(mon["sedentary_minutes"], 240 + 330)
        self.assertEqual(days["2026-10-29"]["exercise_minutes"], 0)  # A successful source leaves its no-data value...
        self.assertNotIn("body_fat_pct", days["2026-10-29"])          # ...a failed one leaves nothing.
        self.assertEqual(failed, ["body-fat"])
        self.assertEqual([n for n in notes if n.startswith("body-fat")], ["body-fat: http-500 — INTERNAL boom"])

    def test_a_refused_rollup_is_summed_from_the_list_endpoint(self):
        minutes = {"dataPoints": [
            {"steps": {"interval": {"civilStartTime": civil(date(2026, 10, 26))}, "count": "500"}},
            {"steps": {"interval": {"civilStartTime": civil(date(2026, 10, 26))}, "count": "700"}}]}

        def refusing(url, body, headers):
            return 400, {"error": {"status": "INVALID_ARGUMENT", "message": "nope"}}

        def lister(url, headers):
            data_type = url.split("/dataTypes/")[1].split("/")[0]
            return (200, minutes) if data_type == "steps" else (200, {"dataPoints": []})
        notes = []
        days = fitbit.fetch_days("tok", date(2026, 10, 26), date(2026, 10, 27), getter=lister, poster=refusing, notes=notes)
        self.assertEqual(days["2026-10-26"]["steps"], 1200)
        self.assertTrue(any("steps: rollup rejected (400: INVALID_ARGUMENT nope" in n for n in notes), notes)
        self.assertIn("steps: summed from the list endpoint", notes)

    def test_auth_account_link_and_other_failures(self):
        with self.assertRaises(fitbit.FitbitError) as caught:
            fitbit.fetch_days("tok", date(2026, 10, 26), date(2026, 10, 27), getter=lambda u, h: (401, {}),
                              poster=lambda u, b, h: (401, {}))
        self.assertEqual(caught.exception.code, "login-required")
        not_linked = (400, {"error": {"status": "FAILED_PRECONDITION", "message": "Account not linked",
                                      "details": [{"reason": "ACCOUNT_NOT_LINKED"}]}})
        with self.assertRaises(fitbit.FitbitError) as caught:
            fitbit.fetch_days("tok", date(2026, 10, 26), date(2026, 10, 27), getter=lambda u, h: not_linked,
                              poster=lambda u, b, h: not_linked)
        self.assertEqual(caught.exception.code, "account-not-linked")
        self.assertEqual(fitbit.check_linked("tok", getter=lambda u, h: not_linked)[0], False)
        self.assertEqual(fitbit.check_linked("tok", getter=lambda u, h: (200, {"healthUserId": "x"}))[0], True)
        self.assertIsNone(fitbit.check_linked("tok", getter=lambda u, h: (503, {}))[0])
        notes = []
        days = fitbit.fetch_days("tok", date(2026, 10, 26), date(2026, 10, 27), getter=lambda u, h: (429, {}),
                                 poster=lambda u, b, h: (429, {}), notes=notes)
        self.assertEqual(days, {"2026-10-26": {}, "2026-10-27": {}})
        self.assertIn("steps: rate-limited", notes)


class StorageTests(unittest.TestCase):
    def test_sync_merges_days_partial_keeps_old_values_and_the_week_is_summarised(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.assertTrue(fitbit.sync_due(folder, NOW))
            result = fitbit.sync(folder, "tok", now=NOW, getter=getter, poster=poster)
            self.assertEqual((result["outcome"], result["failed"]), ("partial", ["body-fat"]))   # body-fat failed.
            self.assertFalse(fitbit.sync_due(folder, NOW))
            week = fitbit.weekly_summary(folder, date(2026, 10, 26), date(2026, 11, 1))
            self.assertEqual((week["source"], week["days_with_data"]), ("fitbit", 3))
            self.assertEqual(week["aerobic_minutes"], (25 + 2 * 15) + (2 * 40) + 30)   # Vigorous counts double; Wed exercise only.
            self.assertEqual((week["strength_days"], week["meets_aerobic"], week["meets_strength"]), (1, True, False))
            self.assertEqual((week["steps_avg"], week["sleep_avg_minutes"], week["resting_hr_avg"]), (9600, 392, 58.0))
            self.assertEqual(week["weight_latest_kg"], 61.4)
            self.assertTrue(week["stale"])                                  # A partial sync is reported as behind.
            lights = {s["key"]: s for s in week["signals"]}
            self.assertEqual((lights["active"]["level"], lights["strength"]["level"], lights["steps"]["level"]), ("good", "warning", "good"))
            self.assertEqual((lights["sleep"]["level"], lights["sleep"]["mine"]), ("warning", "평균 6시간 32분"))   # 392 min: under 7 h.
            self.assertEqual((lights["resting_hr"]["level"], lights["hrv"]["level"]), ("good", "none"))      # No 4-week baseline for HRV.
            self.assertEqual((lights["spo2"]["level"], lights["breaths"]["level"], lights["cardio"]["level"]), ("good", "good", "good"))
            self.assertEqual((lights["sedentary"]["level"], lights["sedentary"]["mine"]), ("warning", "하루 9시간 30분"))
            self.assertTrue(all(s["guide"] and s["source"] for s in week["signals"]))
            # A later sync in which only steps succeed must not wipe the sleep and heart-rate fields of the day.
            steps_only = fitbit.sync(folder, "tok", now=NOW, poster=poster,
                                     getter=lambda u, h: (500, {"error": {"status": "INTERNAL", "message": "down"}}))
            self.assertEqual(steps_only["outcome"], "partial")
            day = fitbit.load_days(folder)["2026-10-26"]
            self.assertEqual((day["steps"], day["sleep_minutes"], day["resting_hr"], day["exercise_minutes"]), (8200, 395, 58, 35))
            failed = fitbit.sync(folder, "tok", now=NOW, getter=lambda u, h: (401, {}), poster=lambda u, b, h: (401, {}))
            self.assertEqual((failed["outcome"], failed["error_code"]), ("error", "login-required"))
            self.assertEqual(fitbit.load_days(folder)["2026-10-26"]["steps"], 8200)   # The stored days survive a failure.
            self.assertIsNone(fitbit.weekly_summary(folder, date(2026, 9, 1), date(2026, 9, 7)))
            self.assertTrue(any("stored" in line for line in fitbit.describe(folder)))


if __name__ == "__main__":
    unittest.main()
