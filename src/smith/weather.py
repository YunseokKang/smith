"""Weather, warnings, air quality and pollen for the daily mail's top block (FR-31 ⓪).

Sources are official and were called on 2026-10-25 to confirm their shapes (docs/daily-design.md §3.1): the KMA
short-term forecast and ultra-short observation (`VilageFcstInfoService_2.0`), the KMA warning status
(`WthrWrnInfoService/getPwnStatus`), AirKorea station values and the regional dust forecast (`ArpltnInforInqireSvc`),
and the KMA pollen risk indices (`HealthWthrIdxServiceV3`). One data.go.kr account key serves them all: the stored
`datagokr` key the real-estate collector already uses.

The clothing-and-items advice is decided here by rules (§3.1), so the same sky gives the same advice every day and a
model only phrases it. The household's airway sensitivity (local setting `airways_sensitive`) moves the air-quality and
pollen cautions one grade earlier and keeps both on the page even when they are fine. Each source fails on its own: a
missing one is reported as missing, never read as fine weather.
"""
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from smith import datagokr

KMA = "http://apis.data.go.kr/1360000"
AIR = "http://apis.data.go.kr/B552584/ArpltnInforInqireSvc"
FORECAST_URL = f"{KMA}/VilageFcstInfoService_2.0/getVilageFcst"
OBSERVATION_URL = f"{KMA}/VilageFcstInfoService_2.0/getUltraSrtNcst"
WARNING_URL = f"{KMA}/WthrWrnInfoService/getPwnStatus"
STATION_URL = f"{AIR}/getMsrstnAcctoRltmMesureDnsty"
DUST_FORECAST_URL = f"{AIR}/getMinuDustFrcstDspth"
POLLEN_URLS = {"참나무": f"{KMA}/HealthWthrIdxServiceV3/getOakPollenRiskIdxV3",
               "소나무": f"{KMA}/HealthWthrIdxServiceV3/getPinePollenRiskIdxV3",
               "잡초류": f"{KMA}/HealthWthrIdxServiceV3/getWeedsPollenRiskndxV3"}   # KMA's own spelling.
FORECAST_ROWS = 1000      # The whole 02:00 run (about 900 rows for three days), so tomorrow's TMN/TMX are included too.
# The short-term forecast runs (KST); each is published about ten minutes after its hour. The evening mail reads the
# newest one that is surely out, so tomorrow's figures are hours fresh rather than the 02:00 run's (daily design §16).
FORECAST_RUNS = (2, 5, 8, 11, 14, 17, 20, 23)
RUN_PUBLISHED_AFTER = timedelta(minutes=15)
GRADE_WORDS = {"좋음": 1, "보통": 2, "나쁨": 3, "매우나쁨": 4}
WAKING_HOURS = range(6, 24)
GRADES = {1: "좋음", 2: "보통", 3: "나쁨", 4: "매우나쁨"}
POLLEN_GRADES = {0: "낮음", 1: "보통", 2: "높음", 3: "매우높음"}
PRECIP = {"1": "비", "2": "비·눈", "3": "눈", "4": "소나기"}
SKY = {"1": "맑음", "3": "구름많음", "4": "흐림"}
SEVERE = ("태풍", "호우", "대설", "강풍", "한파", "폭염")   # A relevant one of these puts a red band on top.
# Rule thresholds (§3.1); the airway-sensitive ones one grade earlier.
RAIN_POP = 50
COLD_MIN = 5.0
SWING = 10.0
HOT_MAX = 30.0
PM25_SENSITIVE = 26.0     # Upper half of '보통' (16-35) already counts for a sensitive airway.
PM10_SENSITIVE = 51.0
WINDY = 9.0               # m/s: the line says 바람 강함 (the same mark as the wind picture).
Getter = datagokr.Getter
_WARNING_LINE = re.compile(r"^o\s*(?P<kind>[가-힣]+?)(?P<level>주의보|경보)\s*:\s*(?P<areas>.+)$")


WeatherError = datagokr.Error


@dataclass(frozen=True)
class Location:
    """Where the household lives, at district precision: a point for the KMA grid, an AirKorea station, a dust-forecast
    region, a pollen area code and the words that make a warning relevant. Nothing finer than a district."""
    lat: float
    lon: float
    air_station: str
    air_region: str
    pollen_area_no: str
    warning_keywords: tuple[str, ...]
    label: str = ""   # Shown in the mail as the basis of the weather ("성남시 분당구"); the district, never an address.

    @property
    def grid(self) -> tuple[int, int]:
        return grid(self.lat, self.lon)


@dataclass(frozen=True)
class Forecast:
    day: date
    tmin: float | None
    tmax: float | None
    pop_max: int | None            # Highest hourly precipitation probability over the waking hours.
    rain_hours: tuple[int, ...]    # Hours with any precipitation type.
    precip: tuple[str, ...]        # "비", "눈", "비·눈", "소나기"
    sky: str | None                # Around midday.
    wind_max: float | None         # m/s
    issued: str                    # "YYYYMMDD HHMM" of the forecast run.
    hourly: tuple["Hour", ...] = ()   # Every forecast hour of the day, for the commute windows (daily design §16.2).


@dataclass(frozen=True)
class Hour:
    hour: int
    temp: float | None     # TMP, ℃
    pop: int | None        # POP, %
    precip: str | None     # PTY as a word ("비", "눈", ...), None when dry
    wind: float | None     # WSD, m/s
    humidity: float | None = None   # REH, %


@dataclass(frozen=True)
class Window:
    """The forecast over a span of hours (inclusive), e.g. the morning commute 07~09시."""
    label: str
    start: int
    end: int
    tmin: float | None
    tmax: float | None
    pop_max: int | None
    precip: tuple[str, ...]
    rain_from: int | None   # First hour with precipitation forecast or POP >= RAIN_POP.
    wind_max: float | None
    rain_known: bool = True  # False when an hour of the span has no POP: a dry line would then be a guess.


@dataclass(frozen=True)
class Observation:
    at: str
    temp: float | None
    humidity: float | None
    precip: str | None   # Current precipitation type or None.
    wind: float | None


@dataclass(frozen=True)
class Warning:
    kind: str            # "강풍", "호우", ...
    level: str           # "주의보" | "경보" | "예비특보"
    areas: str
    relevant: bool


@dataclass(frozen=True)
class AirQuality:
    station: str
    at: str
    pm10: float | None
    pm25: float | None
    pm10_grade: int | None
    pm25_grade: int | None
    forecast_grade: str | None   # Today's regional PM2.5 forecast ("보통"), if published.
    # True for the evening mail's outlook: the grades are the regional forecast for tomorrow, not a measurement.
    outlook: bool = False


@dataclass(frozen=True)
class Pollen:
    kind: str
    today: int | None
    tomorrow: int | None
    in_season: bool


@dataclass(frozen=True)
class Report:
    today: Forecast | None
    tomorrow: Forecast | None
    now: Observation | None
    warnings: tuple[Warning, ...]
    air: AirQuality | None
    pollen: tuple[Pollen, ...]
    failures: dict[str, str] = field(default_factory=dict)   # source -> error code
    # "today" for the morning mail; "tomorrow" when read the evening before (`today` then holds tomorrow's forecast,
    # `tomorrow` the day after, `air` the regional outlook and the pollen its tomorrow index), so the advice rules and
    # the traffic lights apply unchanged to the day the mail is about.
    horizon: str = "today"
    # The evening mail only: what is left of tonight in the same run (hours after it), for the night outlook.
    tonight: Forecast | None = None


@dataclass(frozen=True)
class Advice:
    headline: str                 # The one thing for the subject line.
    items: tuple[str, ...]        # What to take.
    cautions: tuple[str, ...]     # How to move.
    severe: bool                  # A relevant severe warning: red band.
    air_line: str                 # Always present: the air quality and pollen sentence inputs.


def grid(lat: float, lon: float) -> tuple[int, int]:
    """KMA's Lambert conformal grid (the constants of the API guide); 종로구청 → (60, 127), 분당구청 → (62, 123)."""
    re_, slat1, slat2 = 6371.00877 / 5.0, math.radians(30.0), math.radians(60.0)
    olon, olat, xo, yo = math.radians(126.0), math.radians(38.0), 43, 136
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi / 4 + slat2 / 2) / math.tan(math.pi / 4 + slat1 / 2))
    sf = math.tan(math.pi / 4 + slat1 / 2) ** sn * math.cos(slat1) / sn
    ro = re_ * sf / math.tan(math.pi / 4 + olat / 2) ** sn
    ra = re_ * sf / math.tan(math.pi / 4 + math.radians(lat) / 2) ** sn
    theta = math.radians(lon) - olon
    theta = (theta + math.pi) % (2 * math.pi) - math.pi
    theta *= sn
    return int(ra * math.sin(theta) + xo + 0.5), int(ro - ra * math.cos(theta) + yo + 0.5)


def fetch(key: str, location: Location, *, now: datetime, getter: Getter | None = None, horizon: str = "today") -> Report:
    """Everything the block needs, each source on its own; `failures` names the ones that did not answer.

    `now` is household-local. With `horizon="tomorrow"` (the evening mail) the report is about the next day: the
    newest published forecast run, tomorrow's regional dust outlook and pollen index, no current observation."""
    get = getter or datagokr.default_getter
    today = now.date()
    failures: dict[str, str] = {}

    def attempt(name: str, call: Callable[[], Any]) -> Any:
        try:
            return call()
        except WeatherError as error:
            failures[name] = error.code
            return None

    pollen = tuple(p for p in (attempt(f"pollen-{kind}", lambda k=kind: pollen_index(key, k, location, today, get))
                               for kind in POLLEN_URLS) if p is not None)
    if horizon == "tomorrow":
        target = today + timedelta(days=1)
        run = latest_run(now)
        rows = attempt("forecast", lambda: forecast_rows(key, location, run.date(), get, base_time=run.strftime("%H%M")))
        forecasts = {} if rows is None else {d: summarize(rows, d) for d in (target, target + timedelta(days=1))}
        # The observation is tonight's, for the night outlook (the humidity now in the district), not tomorrow's.
        return Report(forecasts.get(target), forecasts.get(target + timedelta(days=1)),
                      attempt("observation", lambda: observation(key, location, now, get)),
                      tuple(attempt("warnings", lambda: warnings(key, location, get)) or ()),
                      attempt("air", lambda: dust_outlook(key, location, today, target, get)),
                      tuple(Pollen(p.kind, p.tomorrow, None, p.in_season) for p in pollen), failures, "tomorrow",
                      None if rows is None else summarize(rows, today))
    rows = attempt("forecast", lambda: forecast_rows(key, location, today, get))
    forecasts = {} if rows is None else {d: summarize(rows, d) for d in (today, today + timedelta(days=1))}
    return Report(forecasts.get(today), forecasts.get(today + timedelta(days=1)),
                  attempt("observation", lambda: observation(key, location, now, get)),
                  tuple(attempt("warnings", lambda: warnings(key, location, get)) or ()),
                  attempt("air", lambda: air_quality(key, location, today, get)), pollen, failures)


def forecast_rows(key: str, location: Location, day: date, get: Getter, *, base_time: str = "0200") -> list[dict[str, Any]]:
    """One forecast run of `day` (by default 02:00, which carries today's TMN and TMX); rows are {fcstDate, fcstTime,
    category, fcstValue}."""
    nx, ny = location.grid
    body = datagokr.call(get, FORECAST_URL, key, pageNo="1", numOfRows=str(FORECAST_ROWS), dataType="JSON",
                 base_date=day.strftime("%Y%m%d"), base_time=base_time, nx=str(nx), ny=str(ny))
    return datagokr.items(body)


def latest_run(now: datetime) -> datetime:
    """The newest forecast run surely published at household-local `now` (yesterday's 23:00 run before 02:15)."""
    for hour in reversed(FORECAST_RUNS):
        run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if run + RUN_PUBLISHED_AFTER <= now:
            return run
    return (now - timedelta(days=1)).replace(hour=FORECAST_RUNS[-1], minute=0, second=0, microsecond=0)


def summarize(rows: list[dict[str, Any]], day: date) -> Forecast | None:
    """One day's figures out of the forecast rows; None when the run has nothing for that day."""
    stamp = day.strftime("%Y%m%d")
    mine = [r for r in rows if str(r.get("fcstDate")) == stamp]
    if not mine:
        return None
    by: dict[str, dict[int, str]] = {}
    for row in mine:
        hour = _hour(row.get("fcstTime"))
        if hour is not None:
            by.setdefault(str(row.get("category")), {})[hour] = str(row.get("fcstValue"))
    temps = [_num(v) for v in by.get("TMP", {}).values()]
    temps = [t for t in temps if t is not None]
    tmin = _first_num(by.get("TMN", {})) if by.get("TMN") else (min(temps) if temps else None)
    tmax = _first_num(by.get("TMX", {})) if by.get("TMX") else (max(temps) if temps else None)
    pops = [_num(v) for h, v in by.get("POP", {}).items() if h in WAKING_HOURS]
    pops = [p for p in pops if p is not None]
    rain_hours = tuple(sorted(h for h, v in by.get("PTY", {}).items() if v not in ("0", "") and h in WAKING_HOURS))
    precip = tuple(dict.fromkeys(PRECIP[v] for h, v in sorted(by.get("PTY", {}).items()) if v in PRECIP and h in WAKING_HOURS))
    sky_values = by.get("SKY", {})
    sky = SKY.get(sky_values.get(12) or sky_values.get(15) or sky_values.get(9) or "")
    winds = [w for w in (_num(v) for v in by.get("WSD", {}).values()) if w is not None]
    issued = f"{mine[0].get('baseDate', '')} {mine[0].get('baseTime', '')}".strip()
    hours = sorted(set().union(*(set(values) for values in by.values()))) if by else []
    hourly = tuple(Hour(h, _num(by.get("TMP", {}).get(h)), _int(by.get("POP", {}).get(h)),
                        PRECIP.get(by.get("PTY", {}).get(h, "0")), _num(by.get("WSD", {}).get(h)),
                        _num(by.get("REH", {}).get(h))) for h in hours)
    return Forecast(day, tmin, tmax, int(max(pops)) if pops else None, rain_hours, precip, sky,
                    max(winds) if winds else None, issued, hourly)


def window(forecast: Forecast | None, label: str, start: int, end: int) -> Window | None:
    """The hours `start`..`end` (inclusive) of a forecast; None when the forecast has no hour in that span."""
    if forecast is None:
        return None
    hours = [h for h in forecast.hourly if start <= h.hour <= end]
    if not hours:
        return None
    temps = [h.temp for h in hours if h.temp is not None]
    pops = [h.pop for h in hours if h.pop is not None]
    winds = [h.wind for h in hours if h.wind is not None]
    wet = [h.hour for h in hours if h.precip or (h.pop or 0) >= RAIN_POP]
    return Window(label, start, end, min(temps) if temps else None, max(temps) if temps else None, max(pops) if pops else None,
                  tuple(dict.fromkeys(h.precip for h in hours if h.precip)), wet[0] if wet else None,
                  max(winds) if winds else None, all(h.pop is not None for h in hours))


@dataclass(frozen=True)
class Night:
    """Tonight from `start` o'clock to `end` o'clock tomorrow morning, from the forecast hours."""
    hours: int              # How many forecast hours the span had.
    t_first: float | None   # The temperature at the first hour (bedtime).
    tmin: float | None
    tmin_hour: int | None
    rh_mean: float | None
    rh_min: float | None
    wind_max: float | None
    precip: tuple[str, ...]


def night(report: Report | None, *, start: int = 22, end: int = 6) -> Night | None:
    """The night outlook of an evening report: tonight's hours from `start` and tomorrow's up to `end`; None when the
    report is not the evening's or has no hour of the span."""
    if report is None or report.horizon != "tomorrow":
        return None
    hours = [h for h in (report.tonight.hourly if report.tonight else ()) if h.hour >= start]
    hours += [h for h in (report.today.hourly if report.today else ()) if h.hour <= end]
    if not hours:
        return None
    temps = [(h.temp, h.hour) for h in hours if h.temp is not None]
    rh = [h.humidity for h in hours if h.humidity is not None]
    winds = [h.wind for h in hours if h.wind is not None]
    low = min(temps) if temps else None
    return Night(len(hours), temps[0][0] if temps else None, low[0] if low else None, low[1] if low else None,
                 round(sum(rh) / len(rh), 1) if rh else None, min(rh) if rh else None, max(winds) if winds else None,
                 tuple(dict.fromkeys(h.precip for h in hours if h.precip)))


def window_line(w: Window) -> str:
    """'출근길 07~09시 · 3~5℃ · 비 60% (08시부터) · 바람 강함', from the figures only."""
    parts = [f"{w.label} {w.start:02d}~{w.end:02d}시"]
    if w.tmin is not None and w.tmax is not None:
        parts.append(f"{w.tmin:g}℃" if w.tmin == w.tmax else f"{w.tmin:g}~{w.tmax:g}℃")
    else:
        parts.append("기온 자료 없음")
    if w.rain_from is None and not w.rain_known:
        parts.append("강수 자료 없음")
    elif w.rain_from is not None:
        kind = "·".join(w.precip) if w.precip else "비"
        parts.append(f"{kind} {w.pop_max}% ({w.rain_from:02d}시부터)" if w.pop_max else f"{kind} ({w.rain_from:02d}시부터)")
    else:
        parts.append(f"비 {w.pop_max}%" if w.pop_max and w.pop_max >= 30 else "비 없음")
    if w.wind_max is not None and w.wind_max >= WINDY:
        parts.append("바람 강함")
    return " · ".join(parts)


def observation(key: str, location: Location, now: datetime, get: Getter) -> Observation:
    """The latest hourly observation: KMA publishes HH:00 values about 40 minutes past the hour."""
    base = (now - timedelta(minutes=40)).replace(minute=0, second=0, microsecond=0)
    nx, ny = location.grid
    body = datagokr.call(get, OBSERVATION_URL, key, pageNo="1", numOfRows="10", dataType="JSON",
                 base_date=base.strftime("%Y%m%d"), base_time=base.strftime("%H%M"), nx=str(nx), ny=str(ny))
    values = {str(r.get("category")): str(r.get("obsrValue")) for r in datagokr.items(body)}
    return Observation(base.strftime("%Y-%m-%d %H:%M"), _num(values.get("T1H")), _num(values.get("REH")),
                       PRECIP.get(values.get("PTY", "0")) if values.get("PTY") not in (None, "0") else None,
                       _num(values.get("WSD")))


def warnings(key: str, location: Location, get: Getter) -> list[Warning]:
    """Current warnings and preliminary notices nationwide, each marked relevant when its areas name the district."""
    body = datagokr.call(get, WARNING_URL, key, pageNo="1", numOfRows="10", dataType="JSON", stnId="108")
    items = datagokr.items(body)
    if not items:
        return []
    found = []
    for line in str(items[0].get("t6", "")).splitlines():
        match = _WARNING_LINE.match(line.strip())
        if match:
            areas = match.group("areas").strip()
            found.append(Warning(match.group("kind"), match.group("level"), areas, _relevant(areas, location)))
    kind = ""
    for line in str(items[0].get("t7", "")).splitlines():
        header = re.match(r"^\(\d+\)\s*([가-힣]+)\s*예비특보", line.strip())
        if header:
            kind = header.group(1)
        elif line.strip().startswith("o") and kind and ":" in line:
            areas = line.split(":", 1)[1].strip()
            found.append(Warning(kind, "예비특보", areas, _relevant(areas, location)))
    return found


def air_quality(key: str, location: Location, today: date, get: Getter) -> AirQuality:
    """The station's latest hourly values plus today's regional PM2.5 forecast grade when one is published."""
    body = datagokr.call(get, STATION_URL, key, returnType="json", stationName=location.air_station, dataTerm="DAILY",
                 numOfRows="1", pageNo="1", ver="1.3")
    items = datagokr.items(body)
    if not items:
        raise WeatherError("no-data")
    row = items[0]
    try:
        forecast_grade = region_grade(key, location, today, today, "PM25", get)[0]
    except WeatherError:
        forecast_grade = None   # The forecast is a bonus; the station values stand on their own.
    return AirQuality(location.air_station, str(row.get("dataTime", "")), _num(row.get("pm10Value")),
                      _num(row.get("pm25Value")), _grade(row.get("pm10Grade1h") or row.get("pm10Grade")),
                      _grade(row.get("pm25Grade1h") or row.get("pm25Grade")), forecast_grade)


def region_grade(key: str, location: Location, searched: date, target: date, code: str, get: Getter) -> tuple[str | None, str]:
    """(grade word, issue time) of the household's region in the newest dust forecast issued on `searched` for
    `target`; (None, "") when no issue covers that day or the region is not named in it."""
    rows = datagokr.items(datagokr.call(get, DUST_FORECAST_URL, key, returnType="json", searchDate=searched.isoformat(),
                                        InformCode=code, numOfRows="20", pageNo="1"))
    mine = sorted((r for r in rows if str(r.get("informData")) == target.isoformat()
                   and str(r.get("informCode", code)) == code), key=lambda r: str(r.get("dataTime")))
    if not mine:
        return None, ""
    match = re.search(re.escape(location.air_region) + r"\s*:\s*([가-힣]+)", str(mine[-1].get("informGrade", "")))
    return (match.group(1) if match else None), str(mine[-1].get("dataTime", ""))


def dust_outlook(key: str, location: Location, today: date, target: date, get: Getter) -> AirQuality:
    """Tomorrow's regional PM10 and PM2.5 forecast grades as an outlook (no measured values). Raises WeatherError
    ("no-data") when neither grade is published for the region yet."""
    pm10, _ = region_grade(key, location, today, target, "PM10", get)
    pm25, issued = region_grade(key, location, today, target, "PM25", get)
    if pm10 is None and pm25 is None:
        raise WeatherError("no-data")
    return AirQuality(location.air_region, issued, None, None, GRADE_WORDS.get(pm10 or ""), GRADE_WORDS.get(pm25 or ""),
                      pm25, outlook=True)


def pollen_index(key: str, kind: str, location: Location, today: date, get: Getter) -> Pollen:
    """Today's and tomorrow's risk index (0 낮음 … 3 매우높음); out of season the service answers code 99."""
    try:
        body = datagokr.call(get, POLLEN_URLS[kind], key, pageNo="1", numOfRows="10", dataType="JSON",
                     areaNo=location.pollen_area_no, time=today.strftime("%Y%m%d") + "06")
    except WeatherError as error:
        if error.code == "out-of-season":
            return Pollen(kind, None, None, False)
        raise
    items = datagokr.items(body)
    if not items:
        return Pollen(kind, None, None, False)
    return Pollen(kind, _index(items[0].get("today")), _index(items[0].get("tomorrow")), True)


def advise(report: Report, *, airways_sensitive: bool) -> Advice:
    """The rules of §3.1 applied to the report; the model later phrases these, never adds to them."""
    items: list[str] = []
    cautions: list[str] = []
    today = report.today
    severe = [w for w in report.warnings if w.relevant and w.kind in SEVERE and w.level != "예비특보"]
    for warning in severe:
        cautions.append(f"{warning.kind}{warning.level}: 외출 자제 또는 주의")
    if today is not None:
        if (today.pop_max or 0) >= RAIN_POP or any(p in ("비", "소나기", "비·눈") for p in today.precip):
            items.append("우산")
        if any(p in ("눈", "비·눈") for p in today.precip):
            cautions.append("눈: 대중교통 권장, 운전 주의")
        if today.tmin is not None and (today.tmin <= COLD_MIN or
                                       (today.tmax is not None and today.tmax - today.tmin >= SWING)):
            items.append("외투(아침·저녁 쌀쌀)")
        if today.tmax is not None and today.tmax >= HOT_MAX:
            items.append("물·양산")
            cautions.append("한낮 외출 주의")
    air = report.air
    if air is not None:
        bad = max(air.pm10_grade or 0, air.pm25_grade or 0) >= 3
        sensitive = airways_sensitive and ((air.pm25 or 0) >= PM25_SENSITIVE or (air.pm10 or 0) >= PM10_SENSITIVE)
        if bad:
            items.append("마스크")
            cautions.append("미세먼지 나쁨: 환기 자제")
        elif sensitive:
            items.append("마스크(기관지 기준)")
    high = [p for p in report.pollen if p.in_season and (p.today or 0) >= (1 if airways_sensitive else 2)]
    if high:
        items.append("마스크" if "마스크" not in items and "마스크(기관지 기준)" not in items else "")
        cautions.append(f"꽃가루 {'·'.join(p.kind for p in high)}: 창문 닫기, 외출 뒤 세안")
    items = [i for i in dict.fromkeys(items) if i]
    return Advice(_headline(report, severe, items), tuple(items), tuple(dict.fromkeys(cautions)), bool(severe),
                  _air_line(report, airways_sensitive))


@dataclass(frozen=True)
class Signal:
    """One traffic light of the mail's top row (user request 2026-10-25): a level the design maps to a color and a
    shape, always with its word, so the meaning never rides on color alone."""
    name: str      # "날씨", "미세먼지", "꽃가루", "특보"
    level: str     # good | warning | serious | critical | none
    label: str     # "좋음", "보통 · 기관지 주의", "계절 밖"
    detail: str    # "12~25℃ · 비 30%"


def signals(report: Report, advice: Advice, *, airways_sensitive: bool) -> list[Signal]:
    """The lights: weather (rules fired → warning, snow or heavy rain → serious, a severe warning → critical), air
    quality on the official four grades, pollen on its four grades (none out of season), and a warning light only
    when a relevant notice exists."""
    today = report.today
    found = []
    if today is None:
        found.append(Signal("날씨", "none", "자료 없음", ""))
    else:
        temp = "" if today.tmin is None or today.tmax is None else f"{today.tmin:g}~{today.tmax:g}℃"
        rain = f"비 {today.pop_max}%" if today.pop_max else "강수 없음"
        if advice.severe:
            level, label = "critical", advice.headline
        elif any(p in ("눈", "비·눈") for p in today.precip) or (today.pop_max or 0) >= 70 or \
                (today.tmax is not None and today.tmax >= 33) or (today.tmin is not None and today.tmin <= -10):
            level, label = "serious", "눈" if any(p in ("눈", "비·눈") for p in today.precip) else (
                "큰 비" if (today.pop_max or 0) >= 70 else ("폭염" if (today.tmax or 0) >= 33 else "강추위"))
        elif advice.items and any(i for i in advice.items if not i.startswith("마스크")):
            level, label = "warning", "·".join(i.split("(")[0] for i in advice.items if not i.startswith("마스크"))
        else:
            level, label = "good", today.sky or "좋음"
        found.append(Signal("날씨", level, label, " · ".join(p for p in (temp, rain) if p)))
    air = report.air
    if air is None:
        found.append(Signal("미세먼지", "none", "자료 없음", ""))
    else:
        grade = max(air.pm25_grade or 0, air.pm10_grade or 0)
        level = {1: "good", 2: "warning", 3: "serious", 4: "critical"}.get(grade, "none")
        label = GRADES.get(grade, "자료 없음")
        if airways_sensitive and grade == 2 and ((air.pm25 or 0) >= PM25_SENSITIVE or (air.pm10 or 0) >= PM10_SENSITIVE):
            label += " · 기관지 주의"
        detail = "권역 예보" if air.outlook else " · ".join(p for p in (f"초미세 {air.pm25:g}" if air.pm25 is not None else "",
                                                                    f"미세 {air.pm10:g}" if air.pm10 is not None else "") if p)
        found.append(Signal("미세먼지", level, label, detail))
    season = [p for p in report.pollen if p.in_season]
    if season:
        top = max((p.today or 0) for p in season)
        level = {0: "good", 1: "warning", 2: "serious", 3: "critical"}[top]
        label = POLLEN_GRADES[top] + (" · 기관지 주의" if airways_sensitive and top == 1 else "")
        found.append(Signal("꽃가루", level, label, "·".join(p.kind for p in season)))
    else:
        found.append(Signal("꽃가루", "none", "계절 밖", "3~6월 · 8~10월"))
    relevant = [w for w in report.warnings if w.relevant]
    if relevant:
        severe = [w for w in relevant if w.level != "예비특보"]
        found.append(Signal("특보", "critical" if severe else "warning", f"{relevant[0].kind}{relevant[0].level}",
                            f"관련 {len(relevant)}건"))
    return found


def for_model(report: Report, advice: Advice) -> dict[str, Any]:
    """Numbers and rule outcomes only: no coordinates, no station address, no key."""
    def forecast(f: Forecast | None) -> dict[str, Any] | None:
        return None if f is None else {"date": f.day.isoformat(), "tmin": f.tmin, "tmax": f.tmax, "pop_max": f.pop_max,
                                       "rain_hours": list(f.rain_hours), "precip": list(f.precip), "sky": f.sky,
                                       "wind_max_ms": f.wind_max}
    days = ({"tomorrow": forecast(report.today), "day_after": forecast(report.tomorrow)} if report.horizon == "tomorrow"
            else {"today": forecast(report.today), "tomorrow": forecast(report.tomorrow)})
    return {**days, "horizon": report.horizon,
            "now": None if report.now is None else {"temp": report.now.temp, "humidity": report.now.humidity,
                                                    "precip": report.now.precip, "at": report.now.at},
            "warnings": [{"kind": w.kind, "level": w.level, "relevant": w.relevant} for w in report.warnings if w.relevant],
            "air": None if report.air is None else {"pm10": report.air.pm10, "pm25": report.air.pm25,
                                                    "pm10_grade": GRADES.get(report.air.pm10_grade or 0),
                                                    "pm25_grade": GRADES.get(report.air.pm25_grade or 0),
                                                    "forecast_today": None if report.air.outlook else report.air.forecast_grade,
                                                    "outlook": report.air.outlook, "at": report.air.at},
            "pollen": [{"kind": p.kind, ("tomorrow" if report.horizon == "tomorrow" else "today"):
                        POLLEN_GRADES.get(p.today) if p.today is not None else None,
                        **({} if report.horizon == "tomorrow" else
                           {"tomorrow": POLLEN_GRADES.get(p.tomorrow) if p.tomorrow is not None else None}),
                        "in_season": p.in_season} for p in report.pollen],
            "advice": {"headline": advice.headline, "items": list(advice.items), "cautions": list(advice.cautions),
                       "severe": advice.severe, "air_line": advice.air_line},
            "failures": dict(report.failures)}


def _headline(report: Report, severe: list[Warning], items: list[str]) -> str:
    if severe:
        return f"{severe[0].kind}{severe[0].level}"
    today = report.today
    if today is not None and any(p in ("눈", "비·눈") for p in today.precip):
        return "눈"
    if "우산" in items:
        return f"비 {today.pop_max}%" if today and today.pop_max else "우산"
    if report.air is not None and max(report.air.pm10_grade or 0, report.air.pm25_grade or 0) >= 3:
        return "미세먼지 나쁨"
    if today is not None and today.tmin is not None and today.tmax is not None:
        return f"{today.tmin:g}~{today.tmax:g}℃" + (" 외투" if "외투(아침·저녁 쌀쌀)" in items else "")
    return "날씨 자료 없음" if "forecast" in report.failures else (today.sky if today and today.sky else "날씨")


def _air_line(report: Report, airways_sensitive: bool) -> str:
    air = report.air
    if air is None:
        parts = ["대기질: " + ("내일 예보를 받지 못함" if report.horizon == "tomorrow" else "오늘 받지 못함")]
    elif air.outlook:
        parts = [f"내일 {air.station} 예보: 초미세먼지 {GRADES.get(air.pm25_grade or 0, '미발표')}, "
                 f"미세먼지 {GRADES.get(air.pm10_grade or 0, '미발표')}"]
    else:
        parts = [f"초미세먼지 {air.pm25:g}㎍/㎥ {GRADES.get(air.pm25_grade or 0, '')}" if air.pm25 is not None else "초미세먼지 값 없음",
                 f"미세먼지 {air.pm10:g}㎍/㎥ {GRADES.get(air.pm10_grade or 0, '')}" if air.pm10 is not None else "미세먼지 값 없음"]
        if air.forecast_grade:
            parts.append(f"오늘 예보 {air.forecast_grade}")
    season = [p for p in report.pollen if p.in_season]
    if season:
        parts.append(("내일 " if report.horizon == "tomorrow" else "") + "꽃가루 "
                     + ", ".join(f"{p.kind} {POLLEN_GRADES.get(p.today or 0)}" for p in season))
    elif report.pollen:
        parts.append("꽃가루: 제공 기간 아님")
    if airways_sensitive:
        parts.append("기관지 기준")
    return " · ".join(p.strip() for p in parts)


def _relevant(areas: str, location: Location) -> bool:
    return any(word in areas for word in location.warning_keywords)


def _hour(raw: Any) -> int | None:
    text = str(raw or "")
    return int(text[:2]) if len(text) == 4 and text.isdigit() else None


def _num(raw: Any) -> float | None:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _first_num(values: dict[int, str]) -> float | None:
    for value in values.values():
        number = _num(value)
        if number is not None:
            return number
    return None


def _int(raw: Any) -> int | None:
    number = _num(raw)
    return None if number is None else int(number)


def _grade(raw: Any) -> int | None:
    number = _num(raw)
    return int(number) if number is not None and 1 <= number <= 4 else None


def _index(raw: Any) -> int | None:
    number = _num(raw)
    return None if number is None else max(0, min(3, int(number)))
