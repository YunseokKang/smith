import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from smith import weather

LOC = weather.Location(37.3825, 127.1189, "정자동", "경기남부", "4113500000", ("경기", "성남"))
NOW = datetime(2026, 10, 26, 6, 0, tzinfo=ZoneInfo("Asia/Seoul"))
TODAY = date(2026, 10, 26)


def kma(items):
    return 200, {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                              "body": {"dataType": "JSON", "items": {"item": items}, "totalCount": len(items)}}}


def air(items):
    return 200, {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL_CODE"}, "body": {"items": items}}}


def rows(day, **hourly):
    """Forecast rows for one day: TMN/TMX plus per-hour values given as {category: {hour: value}}."""
    out = [{"baseDate": "20261026", "baseTime": "0200", "fcstDate": day, "fcstTime": "0600", "category": "TMN", "fcstValue": "4.0"},
           {"baseDate": "20261026", "baseTime": "0200", "fcstDate": day, "fcstTime": "1500", "category": "TMX", "fcstValue": "16.0"}]
    for category, values in hourly.items():
        out += [{"fcstDate": day, "fcstTime": f"{h:02d}00", "category": category, "fcstValue": v} for h, v in values.items()]
    return out


FORECAST = rows("20261026", POP={6: "10", 9: "30", 15: "60", 21: "20", 3: "90"}, PTY={15: "1", 16: "1", 3: "3"},
                SKY={12: "4"}, WSD={9: "3.2", 15: "7.5"}, TMP={6: "5", 15: "15"}) + \
    [{"fcstDate": "20261027", "fcstTime": f"{h:02d}00", "category": "TMP", "fcstValue": str(v)} for h, v in ((6, 2), (15, 12))]
OBSERVATION = [{"category": "T1H", "obsrValue": "4.8"}, {"category": "REH", "obsrValue": "90"},
               {"category": "PTY", "obsrValue": "0"}, {"category": "WSD", "obsrValue": "1.1"}]
WARNINGS = [{"t6": "o 강풍주의보 : 경기도(성남, 수원), 강원도산지\r\no 풍랑주의보 : 동해중부안쪽먼바다\r\n",
             "t7": "(1) 한파 예비특보\r\no 10월 27일 아침 : 경기도(성남)\r\n(2) 태풍 예비특보\r\no 10월 27일 : 제주도",
             "tmFc": 202610260500}]
STATION = [{"dataTime": "2026-10-26 05:00", "pm10Value": "55", "pm25Value": "30", "pm10Grade1h": "2", "pm25Grade1h": "2"}]
DUST = [{"informData": "2026-10-26", "informCode": "PM25", "dataTime": "2026-10-26 05시 발표",
         "informGrade": "서울 : 보통,경기북부 : 보통,경기남부 : 나쁨,제주 : 좋음"}]
POLLEN_OAK = {"response": {"header": {"resultCode": "99", "resultMsg": "해당지수자료 제공기간이 아닙니다! [자료제공기간 3월 ~ 6월]"}}}
POLLEN_WEEDS = kma([{"code": "D08", "areaNo": "4113500000", "today": "2", "tomorrow": "1"}])


def getter_for(overrides=None):
    answers = {weather.FORECAST_URL: kma(FORECAST), weather.OBSERVATION_URL: kma(OBSERVATION),
               weather.WARNING_URL: kma(WARNINGS), weather.STATION_URL: air(STATION), weather.DUST_FORECAST_URL: air(DUST),
               weather.POLLEN_URLS["참나무"]: (200, POLLEN_OAK), weather.POLLEN_URLS["소나무"]: (200, POLLEN_OAK),
               weather.POLLEN_URLS["잡초류"]: POLLEN_WEEDS, **(overrides or {})}
    seen = []

    def get(url):
        seen.append(url)
        base = url.split("?")[0]
        return answers[base]
    return get, seen


class WeatherTests(unittest.TestCase):
    def test_grid_matches_the_kma_guide(self):
        self.assertEqual(weather.grid(37.5735, 126.9790), (60, 127))   # 종로구청
        self.assertEqual(LOC.grid, (62, 123))                          # 분당구청

    def test_report_reads_every_source_without_leaking_the_key(self):
        get, seen = getter_for()
        report = weather.fetch("SECRET-KEY", LOC, now=NOW, getter=get)
        self.assertEqual(report.failures, {})
        today = report.today
        self.assertEqual((today.tmin, today.tmax, today.pop_max, today.rain_hours, today.precip, today.sky, today.wind_max),
                         (4.0, 16.0, 60, (15, 16), ("비",), "흐림", 7.5))   # 03:00 rows are before waking hours.
        self.assertEqual((report.tomorrow.tmin, report.tomorrow.tmax), (2.0, 12.0))   # No TMN/TMX: hourly min/max.
        self.assertEqual((report.now.temp, report.now.precip, report.now.at), (4.8, None, "2026-10-26 05:00"))
        self.assertEqual([(w.kind, w.level, w.relevant) for w in report.warnings],
                         [("강풍", "주의보", True), ("풍랑", "주의보", False), ("한파", "예비특보", True), ("태풍", "예비특보", False)])
        self.assertEqual((report.air.pm25, report.air.pm25_grade, report.air.forecast_grade), (30.0, 2, "나쁨"))
        self.assertEqual([(p.kind, p.in_season, p.today) for p in report.pollen],
                         [("참나무", False, None), ("소나무", False, None), ("잡초류", True, 2)])
        self.assertTrue(all("serviceKey=SECRET-KEY" in url for url in seen))
        self.assertIn("base_time=0200", next(u for u in seen if u.startswith(weather.FORECAST_URL)))
        self.assertIn("base_time=0500", next(u for u in seen if u.startswith(weather.OBSERVATION_URL)))
        payload = weather.for_model(report, weather.advise(report, airways_sensitive=True))
        self.assertNotIn("SECRET-KEY", str(payload))
        self.assertNotIn("37.38", str(payload))
        self.assertEqual(payload["warnings"], [{"kind": "강풍", "level": "주의보", "relevant": True},
                                               {"kind": "한파", "level": "예비특보", "relevant": True}])

    def test_rules_follow_the_table_and_the_airway_setting(self):
        get, _ = getter_for()
        report = weather.fetch("k", LOC, now=NOW, getter=get)
        general = weather.advise(report, airways_sensitive=False)
        self.assertEqual(general.items, ("우산", "외투(아침·저녁 쌀쌀)", "마스크"))     # Rain 60%, min 4℃; pollen 높음.
        self.assertEqual(general.cautions, ("강풍주의보: 외출 자제 또는 주의", "꽃가루 잡초류: 창문 닫기, 외출 뒤 세안"))
        self.assertEqual((general.headline, general.severe), ("강풍주의보", True))
        sensitive = weather.advise(report, airways_sensitive=True)
        self.assertIn("마스크(기관지 기준)", sensitive.items)                          # PM2.5 30: upper '보통'.
        self.assertEqual(sensitive.air_line, "초미세먼지 30㎍/㎥ 보통 · 미세먼지 55㎍/㎥ 보통 · 오늘 예보 나쁨 · 꽃가루 잡초류 높음 · 기관지 기준")
        calm_warnings = kma([{"t6": "o 없음\r\n", "t7": ""}])
        get, _ = getter_for({weather.WARNING_URL: calm_warnings})
        advice = weather.advise(weather.fetch("k", LOC, now=NOW, getter=get), airways_sensitive=False)
        self.assertEqual((advice.headline, advice.severe), ("비 60%", False))

    def test_each_source_fails_on_its_own(self):
        portal = (200, {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": "30", "errMsg": "SERVICE KEY IS NOT REGISTERED"}}})
        get, _ = getter_for({weather.STATION_URL: portal, weather.FORECAST_URL: (500, {})})
        report = weather.fetch("k", LOC, now=NOW, getter=get)
        self.assertEqual(report.failures, {"forecast": "http-500", "air": "portal-30"})
        self.assertIsNone(report.today)
        advice = weather.advise(report, airways_sensitive=True)
        self.assertEqual(advice.headline, "강풍주의보")
        self.assertTrue(advice.air_line.startswith("대기질: 오늘 받지 못함"))

        def down(url):
            raise OSError("offline")
        report = weather.fetch("k", LOC, now=NOW, getter=down)
        self.assertEqual(set(report.failures.values()), {"network-error"})
        self.assertEqual(weather.advise(report, airways_sensitive=False).headline, "날씨 자료 없음")


if __name__ == "__main__":
    unittest.main()
