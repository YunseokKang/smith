"""Korean public holidays (FR-31 ⑥) from the Korea Astronomy and Space Science Institute's special-day service.

`SpcdeInfoService/getRestDeInfo` (verified 2026-10-25) lists a year's public holidays, substitute holidays included,
with `isHoliday == "Y"`. A year is fetched once and kept in `data/holidays.json` next to the ledger (public data, so
the file is only a cache; it is refreshed after REFRESH_DAYS because substitute holidays are declared during the year).
"""
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smith import datagokr

URL = "http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
REFRESH_DAYS = 30
WEEKEND = (5, 6)   # Saturday, Sunday (date.weekday()).


@dataclass(frozen=True)
class Holiday:
    day: date
    name: str


def path(db: Path) -> Path:
    return Path(db).parent / "holidays.json"


def for_year(key: str, year: int, *, cache: Path, now: datetime, getter: datagokr.Getter | None = None) -> list[Holiday]:
    """The year's holidays from the cache when fresh, else from the service (then cached).

    Raises:
        datagokr.Error: the service did not answer and the cache has nothing for the year.
    """
    stored = _read(cache)
    entry = stored.get(str(year))
    fresh = (isinstance(entry, dict) and isinstance(entry.get("fetched_at"), str)
             and datetime.fromisoformat(entry["fetched_at"]) > now - timedelta(days=REFRESH_DAYS))
    if fresh:
        return _parse(entry.get("days", []))
    try:
        body = datagokr.call(getter or datagokr.default_getter, URL, key, solYear=str(year), numOfRows="100",
                             pageNo="1", _type="json")
    except datagokr.Error:
        if isinstance(entry, dict):   # Stale is better than nothing; the caller shows the data's age if it wants.
            return _parse(entry.get("days", []))
        raise
    days = [{"date": str(i.get("locdate")), "name": str(i.get("dateName", "")).strip()}
            for i in datagokr.items(body) if str(i.get("isHoliday", "")).upper() == "Y"]
    stored[str(year)] = {"fetched_at": now.isoformat(), "days": days}
    _write(cache, stored)
    return _parse(days)


def around(key: str, day: date, *, cache: Path, now: datetime, getter: datagokr.Getter | None = None,
           days_ahead: int = 14) -> tuple[Holiday | None, list[Holiday]]:
    """(today's holiday or None, holidays within `days_ahead` after today), reading next year when the window crosses."""
    years = {day.year, (day + timedelta(days=days_ahead)).year}
    found = [h for y in sorted(years) for h in for_year(key, y, cache=cache, now=now, getter=getter)]
    today = next((h for h in found if h.day == day), None)
    ahead = [h for h in found if day < h.day <= day + timedelta(days=days_ahead)]
    return today, sorted(ahead, key=lambda h: h.day)


def is_rest_day(day: date, holiday: Holiday | None) -> bool:
    return holiday is not None or day.weekday() in WEEKEND


def long_weekends(ahead: list[Holiday], today: date) -> list[tuple[date, date]]:
    """Runs of three or more rest days (weekend plus holidays) starting within the window, as (first, last)."""
    rest = {h.day for h in ahead}
    runs, start = [], None
    for offset in range(1, 22):
        day = today + timedelta(days=offset)
        if day in rest or day.weekday() in WEEKEND:
            start = start or day
        elif start is not None:
            if (day - start).days >= 3:
                runs.append((start, day - timedelta(days=1)))
            start = None
    return runs


def _parse(days: list[Any]) -> list[Holiday]:
    found = []
    for item in days:
        try:
            found.append(Holiday(datetime.strptime(str(item["date"]), "%Y%m%d").date(), str(item["name"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(found, key=lambda h: h.day)


def _read(cache: Path) -> dict[str, Any]:
    try:
        data = json.loads(cache.read_text(encoding="utf-8-sig")) if cache.exists() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(cache: Path, data: dict[str, Any]) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    temp = cache.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, cache)
