"""Official sources for weekend outings (FR-57, docs/daily-design.md §18): what is on, and the weekend's weather.

Every shape here was called with the household's keys on 2026-11-18:

- Korea Tourism Organization TourAPI 4.0, `KorService2` (data.go.kr 15101578, the stored `datagokr` key):
  `searchFestival2` (eventStartDate/eventEndDate, lDongRegnCd), `ldongCode2` (legal-dong district codes; a city such
  as 수원시 has no rows of its own, its 구 do), `detailCommon2` (homepage). Success is resultCode "0000".
- Seoul Open Data Plaza `culturalEventInfo` (OA-15486, the `seoul` key). Plain HTTP on port 8088 (HTTPS refuses);
  the key travels in the path, so no URL ever reaches an exception or a log. The DATE filter does not narrow the
  list (past events come back), so each category is read whole and the dates are checked here.
- KMA mid-term forecast `MidFcstInfoService` (data.go.kr 15059468): `getMidLandFcst` (wf/rnSt by day, AM/PM for
  days 4-7) and `getMidTa` (taMin/taMax), issued 06:00 and 18:00. The short-term forecast (weather.py) already reaches
  four days ahead from the 20:00 run, so the mid-term one only fills days beyond it.

Only public words leave: dates, region codes, category names. Nothing about the household is sent.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from smith import datagokr, weather

TOUR = "https://apis.data.go.kr/B551011/KorService2"
APP = {"MobileOS": "ETC", "MobileApp": "Smith", "_type": "json"}
MID = "https://apis.data.go.kr/1360000/MidFcstInfoService"
MID_LAND_REGION = "11B00000"   # 서울·인천·경기도 (the land forecast region).
MID_TEMP_REGION = "11B10101"   # 서울 (temperatures are by city; the capital area is read from Seoul's).
SEOUL = "http://openapi.seoul.go.kr:8088"
SEOUL_PROVIDER = "seoul"
SEOUL_PAGE = 1000              # The service's largest page.
# Seoul culture categories as the service names them (CODENAME). Performances are many; one page each is enough
# because the list comes newest first.
SEOUL_CATEGORIES = ("전시/미술", "축제-문화/예술", "축제-전통/역사", "축제-자연/경관", "축제-시민화합", "축제-기타", "교육/체험",
                    "클래식", "콘서트", "국악", "뮤지컬/오페라", "연극", "무용", "독주/독창회")
# Legal-dong province codes the outing range may name (verified 2026-11-18 for 11 and 41).
PROVINCES = {"서울": "11", "인천": "28", "경기": "41"}
MAX_TITLE = 120
_TIMEOUT = 20
_URL = re.compile(r"https?://[^\s\"'<>]+")

Getter = datagokr.Getter


class SourceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Event:
    """Something on during a span of days, from either source."""
    source: str                   # "tour" | "seoul"
    id: str
    title: str
    start: date
    end: date
    place: str                    # Venue (Seoul) or street address (TourAPI), public.
    district: str                 # "중구", "고양시 덕양구": what the range check and the reader see.
    category: str                 # Seoul CODENAME or TourAPI lclsSystm3 ("EV010200").
    lat: float | None = None
    lon: float | None = None
    fee: str = ""                 # Seoul: "무료" or the fee text.
    audience: str = ""            # Seoul USE_TRGT ("7세 이상 누구나").
    hours: str = ""               # Seoul PRO_TIME.
    link: str = ""                # A public page about it ("가기 전 확인").


@dataclass(frozen=True)
class DayWeather:
    day: date
    sky: str | None               # "맑음", "흐리고 비" ...
    pop: int | None               # Highest chance of rain in the daytime, %.
    tmin: float | None
    tmax: float | None
    source: str                   # "단기예보 11/18 20시" | "중기예보 11/18 06시"
    air: str | None = None        # Regional PM forecast grade word, the worse of PM10/PM2.5 ("보통").

    @property
    def wet(self) -> bool:
        return (self.pop or 0) >= weather.RAIN_POP or any(w in (self.sky or "") for w in ("비", "눈", "소나기"))


@dataclass
class Weekend:
    days: list[DayWeather] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------------------- TourAPI

def festivals(key: str, first: date, last: date, province: str, *, get: Getter | None = None,
              rows: int = 200) -> list[Event]:
    """Festivals and events in a province overlapping first..last. Raises datagokr.Error."""
    body = datagokr.call(get or datagokr.default_getter, f"{TOUR}/searchFestival2", key,
                         eventStartDate=first.strftime("%Y%m%d"), eventEndDate=last.strftime("%Y%m%d"),
                         lDongRegnCd=province, numOfRows=str(rows), pageNo="1", arrange="C", **APP)
    return [e for e in (_tour_event(i) for i in datagokr.items(body)) if e is not None]


def districts(key: str, province: str, *, get: Getter | None = None) -> list[tuple[str, str]]:
    """(three-digit district code, name) of a province, e.g. ("117", "수원시 영통구"). Raises datagokr.Error."""
    body = datagokr.call(get or datagokr.default_getter, f"{TOUR}/ldongCode2", key, lDongRegnCd=province,
                         numOfRows="300", pageNo="1", **APP)
    return [(str(i.get("code")), str(i.get("name")).strip()) for i in datagokr.items(body)
            if re.fullmatch(r"\d{3}", str(i.get("code", ""))) and str(i.get("name", "")).strip()]


def homepage(key: str, content_id: str, *, get: Getter | None = None) -> str:
    """The event's own page from its TourAPI detail, or "" (the field is sometimes an HTML anchor)."""
    if not content_id.isdigit():
        return ""
    try:
        body = datagokr.call(get or datagokr.default_getter, f"{TOUR}/detailCommon2", key, contentId=content_id, **APP)
    except datagokr.Error:
        return ""
    found = datagokr.items(body)
    match = _URL.search(str(found[0].get("homepage", ""))) if found else None
    return match.group(0).rstrip(").,") if match else ""


def _tour_event(item: dict[str, Any]) -> Event | None:
    start, end = _ymd(item.get("eventstartdate")), _ymd(item.get("eventenddate"))
    title = _text(item.get("title"), MAX_TITLE)
    content_id = str(item.get("contentid", ""))
    if not (start and end and title and content_id.isdigit()) or end < start:
        return None
    return Event("tour", content_id, title, start, end, _text(item.get("addr1"), 120),
                 _province_district(str(item.get("addr1", ""))), _text(item.get("lclsSystm3"), 12),
                 _coord(item.get("mapy"), 33, 39), _coord(item.get("mapx"), 124, 132))


def _province_district(address: str) -> str:
    """"고양시 덕양구" / "중구" out of a street address ("경기도 고양시 덕양구 …", "서울특별시 중구 …")."""
    words = address.split()
    if len(words) >= 3 and words[1].endswith("시") and words[2].endswith("구"):
        return f"{words[1]} {words[2]}"
    return words[1] if len(words) >= 2 and words[1][-1:] in ("구", "시", "군") else ""


# ------------------------------------------------------------------------------------------------------------ Seoul

def seoul_events(key: str, category: str, *, opener: Callable[..., Any] | None = None) -> list[Event]:
    """One Seoul culture category (the newest SEOUL_PAGE). Raises SourceError(network-error | http-N | bad-body |
    seoul-<code>); "INFO-200" (nothing in the category) is an empty list."""
    if not re.fullmatch(r"[0-9A-Za-z]{8,64}", key or ""):
        raise SourceError("seoul-bad-key")
    url = f"{SEOUL}/{key}/json/culturalEventInfo/1/{SEOUL_PAGE}/{urllib.parse.quote(category, safe='')}"
    try:
        with (opener or urllib.request.urlopen)(url, timeout=_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise SourceError(f"http-{error.code}") from None
    except (OSError, ValueError):
        raise SourceError("network-error") from None
    if not isinstance(body, dict):
        raise SourceError("seoul-bad-body")
    # A normal answer wraps RESULT in the service name; "nothing found" and key errors put it at the top.
    block = body.get("culturalEventInfo")
    result = (block if isinstance(block, dict) else body).get("RESULT")
    code = str(result.get("CODE", "")) if isinstance(result, dict) else ""
    if code == "INFO-200":
        return []
    if not isinstance(block, dict) or code != "INFO-000":
        raise SourceError(f"seoul-{code or 'bad-body'}")
    rows = block.get("row")
    return [e for e in (_seoul_event(r) for r in rows if isinstance(r, dict)) if e is not None] if isinstance(rows, list) else []


def _seoul_event(row: dict[str, Any]) -> Event | None:
    start, end = _iso_day(row.get("STRTDATE")), _iso_day(row.get("END_DATE"))
    title = _text(row.get("TITLE"), MAX_TITLE)
    link = str(row.get("HMPG_ADDR", "")).strip()
    code = re.search(r"cultcode=(\d+)", link)
    if not (start and end and title) or end < start:
        return None
    fee = "무료" if str(row.get("IS_FREE", "")).strip() == "무료" else _text(row.get("USE_FEE"), 80)
    return Event("seoul", code.group(1) if code else title, title, start, end, _text(row.get("PLACE"), 60),
                 _text(row.get("GUNAME"), 10), _text(row.get("CODENAME"), 20),
                 _coord(row.get("LAT"), 33, 39), _coord(row.get("LOT"), 124, 132), fee,
                 _text(row.get("USE_TRGT"), 40), _text(row.get("PRO_TIME"), 80),
                 link if link.startswith("https://culture.seoul.go.kr/") else "")


# ---------------------------------------------------------------------------------------------------------- weather

def weekend_weather(key: str, location: weather.Location, days: list[date], *, now: datetime,
                    get: Getter | None = None) -> Weekend:
    """Each day's sky, rain chance and temperatures: the short-term forecast where it reaches, the mid-term one
    beyond it, and the regional dust forecast where it is published. Failures are named, never read as fine."""
    get = get or datagokr.default_getter
    out = Weekend()
    run = weather.latest_run(now)
    try:
        rows = weather.forecast_rows(key, location, run.date(), get, base_time=run.strftime("%H%M"))
    except datagokr.Error as error:
        rows, out.failures["forecast"] = [], error.code
    mid: tuple[dict[str, Any], dict[str, Any], date, str] | None = None
    for day in days:
        short = weather.summarize(rows, day) if rows else None
        if short is not None and short.pop_max is not None:
            out.days.append(DayWeather(day, _short_sky(short), short.pop_max, short.tmin, short.tmax,
                                       f"단기예보 {run.month}/{run.day} {run.hour}시"))
            continue
        if mid is None and "mid" not in out.failures:
            try:
                mid = _mid(key, now, get)
            except datagokr.Error as error:
                out.failures["mid"] = error.code
        found = _mid_day(mid, day) if mid else None
        out.days.append(found or DayWeather(day, None, None, None, None, "예보 없음"))
    for i, item in enumerate(out.days):
        try:
            air = weather.dust_outlook(key, location, now.date(), item.day, get)
        except datagokr.Error as error:
            if error.code == "no-data":
                continue                   # Not yet published for that day: shown as "미세먼지 예보 전".
            out.failures["air"] = error.code   # Down or refused: named, not shown as "not yet" (Codex review 2026-11-18).
            break
        grades = [g for g in (air.pm10_grade, air.pm25_grade) if g]
        if grades:
            out.days[i] = DayWeather(item.day, item.sky, item.pop, item.tmin, item.tmax, item.source,
                                     weather.GRADES.get(max(grades)))
    return out


def mid_run(now: datetime) -> datetime:
    """The newest mid-term issue surely out (06:00 and 18:00, read 30 minutes later)."""
    for hour in (18, 6):
        issue = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if issue + timedelta(minutes=30) <= now:
            return issue
    return (now - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)


def _mid(key: str, now: datetime, get: Getter) -> tuple[dict[str, Any], dict[str, Any], date, str]:
    issue = mid_run(now)
    stamp = issue.strftime("%Y%m%d%H%M")
    land = datagokr.items(datagokr.call(get, f"{MID}/getMidLandFcst", key, regId=MID_LAND_REGION, tmFc=stamp,
                                        dataType="JSON", numOfRows="10", pageNo="1"))
    temp = datagokr.items(datagokr.call(get, f"{MID}/getMidTa", key, regId=MID_TEMP_REGION, tmFc=stamp,
                                        dataType="JSON", numOfRows="10", pageNo="1"))
    if not land:
        raise datagokr.Error("no-data")
    return land[0], (temp[0] if temp else {}), issue.date(), f"중기예보 {issue.month}/{issue.day} {issue.hour}시"


def _mid_day(mid: tuple[dict[str, Any], dict[str, Any], date, str], day: date) -> DayWeather | None:
    land, temp, issued, label = mid
    n = (day - issued).days
    if not 4 <= n <= 10:
        return None
    if n <= 7:
        skies = [str(land.get(f"wf{n}{h}", "")).strip() for h in ("Am", "Pm")]
        pops = [_int(land.get(f"rnSt{n}{h}")) for h in ("Am", "Pm")]
        sky = skies[0] if skies[0] == skies[1] else " → ".join(s for s in skies if s)
    else:
        sky, pops = str(land.get(f"wf{n}", "")).strip(), [_int(land.get(f"rnSt{n}"))]
    known = [p for p in pops if p is not None]
    return DayWeather(day, sky or None, max(known) if known else None, _num(temp.get(f"taMin{n}")),
                      _num(temp.get(f"taMax{n}")), label)


def _short_sky(f: weather.Forecast) -> str | None:
    if f.precip:
        return f"{f.sky or '흐림'}, {'·'.join(f.precip)}"
    return f.sky


# ----------------------------------------------------------------------------------------------------------- shared

def _text(raw: Any, limit: int) -> str:
    return " ".join(str(raw or "").split())[:limit]


def _ymd(raw: Any) -> date | None:
    text = str(raw or "")
    try:
        return datetime.strptime(text, "%Y%m%d").date() if re.fullmatch(r"\d{8}", text) else None
    except ValueError:
        return None


def _iso_day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw or "")[:10])
    except ValueError:
        return None


def _coord(raw: Any, low: float, high: float) -> float | None:
    value = _num(raw)
    return value if value is not None and low <= value <= high else None


def _num(raw: Any) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def _int(raw: Any) -> int | None:
    value = _num(raw)
    return int(value) if value is not None and 0 <= value <= 100 else None
