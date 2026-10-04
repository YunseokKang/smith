"""Macro evidence: Korean and US policy and market rates from official statistics APIs.

Sources (verified against the live APIs on 2026-10-04):
- Bank of Korea ECOS `StatisticSearch` (stat/item codes from `StatisticItemList`).
- FRED `series/observations` (https://fred.stlouisfed.org/docs/api/fred/series_observations.html).

API keys travel in request URLs, so URLs and raw error messages are never printed or logged.
"""
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from smith.net import open_url
from smith.records import canonical_decimal

_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class SeriesSpec:
    provider: str
    series_id: str  # ECOS: "<stat code>/<item code>"; FRED: series id.
    label: str
    unit: str
    source_url: str  # An https URL, optionally followed by a space and a readable series code.
    max_age_days: int  # Older latest observations are reported as stale.

    @property
    def link(self) -> str:
        """The URL part of `source_url`, without the readable series code."""
        return self.source_url.split(" ", 1)[0]


SERIES = (
    SeriesSpec("ecos", "722Y001/0101000", "Bank of Korea base rate", "%",
               "https://ecos.bok.or.kr/ (722Y001 0101000)", 10),
    SeriesSpec("ecos", "817Y002/010200000", "Korea treasury 3Y", "%", "https://ecos.bok.or.kr/ (817Y002 010200000)", 10),
    SeriesSpec("ecos", "817Y002/010210000", "Korea treasury 10Y", "%", "https://ecos.bok.or.kr/ (817Y002 010210000)", 10),
    SeriesSpec("ecos", "817Y002/010502000", "Korea CD 91D", "%", "https://ecos.bok.or.kr/ (817Y002 010502000)", 10),
    SeriesSpec("ecos", "731Y001/0000001", "KRW per USD (BOK reference rate)", "KRW",
               "https://ecos.bok.or.kr/ (731Y001 0000001)", 10),
    SeriesSpec("fred", "DFEDTARU", "Fed funds target upper", "%", "https://fred.stlouisfed.org/series/DFEDTARU", 10),
    SeriesSpec("fred", "DFF", "Fed funds effective", "%", "https://fred.stlouisfed.org/series/DFF", 10),
    SeriesSpec("fred", "DGS2", "US treasury 2Y", "%", "https://fred.stlouisfed.org/series/DGS2", 10),
    SeriesSpec("fred", "DGS10", "US treasury 10Y", "%", "https://fred.stlouisfed.org/series/DGS10", 10),
    SeriesSpec("fred", "MORTGAGE30US", "US 30Y fixed mortgage", "%",
               "https://fred.stlouisfed.org/series/MORTGAGE30US", 14),
)

# url -> (status, body bytes)
Getter = Callable[[str], tuple[int, bytes]]
Point = tuple[str, str, date, str, date | None]  # provider, series_id, observed_on, value, published_on


class EvidenceError(Exception):
    """A provider request failed. Carries only a short code, never the URL (it contains the key)."""

    def __init__(self, provider: str, code: str) -> None:
        super().__init__(f"{provider}: {code}")
        self.provider, self.code = provider, code


def urllib_get(url: str) -> tuple[int, bytes]:
    try:
        with open_url(url, timeout=_TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read()
    except (OSError, http.client.HTTPException):
        raise EvidenceError("http", "network-error") from None


def fetch_provider(provider: str, api_key: str, *, start: date, end: date,
                   get: Getter = urllib_get) -> list[Point]:
    """Fetch every configured series of one provider; any failure fails the whole provider.

    Raises:
        EvidenceError: a request failed or a response was malformed.
    """
    fetch = _fetch_ecos if provider == "ecos" else _fetch_fred
    points: list[Point] = []
    for spec in (s for s in SERIES if s.provider == provider):
        try:
            series = fetch(spec, api_key, start, end, get)
        except EvidenceError as error:
            raise EvidenceError(provider, error.code) from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise EvidenceError(provider, "invalid-response") from None
        # Every configured series is required. An empty one (a renamed code, a provider change)
        # must not leave old values looking freshly confirmed.
        if not series:
            raise EvidenceError(provider, f"missing-series:{spec.series_id}")
        points.extend(series)
    return points


def _fetch_ecos(spec: SeriesSpec, key: str, start: date, end: date, get: Getter) -> list[Point]:
    stat, item = spec.series_id.split("/")
    status, payload = _json(get(f"https://ecos.bok.or.kr/api/StatisticSearch/{urllib.parse.quote(key)}/json/kr/"
                                f"1/1000/{stat}/D/{start:%Y%m%d}/{end:%Y%m%d}/{item}"))
    if "RESULT" in payload:  # ECOS reports errors and empty results in-band.
        code = payload["RESULT"]["CODE"]
        if code == "INFO-200":  # No data in the requested range; the caller treats it as missing.
            return []
        raise EvidenceError("ecos", str(code))
    if status != 200:
        raise EvidenceError("ecos", f"http-{status}")
    return [("ecos", spec.series_id, datetime.strptime(row["TIME"], "%Y%m%d").date(),
             canonical_decimal(row["DATA_VALUE"]), None)
            for row in payload["StatisticSearch"]["row"] if row.get("DATA_VALUE") not in (None, "")]


def _fetch_fred(spec: SeriesSpec, key: str, start: date, end: date, get: Getter) -> list[Point]:
    query = urllib.parse.urlencode({"series_id": spec.series_id, "api_key": key, "file_type": "json",
                                    "observation_start": start.isoformat(), "observation_end": end.isoformat()})
    status, payload = _json(get(f"https://api.stlouisfed.org/fred/series/observations?{query}"))
    if status != 200:
        raise EvidenceError("fred", str(payload.get("error_code") or f"http-{status}"))
    # "." marks a missing observation (for example a market holiday); it is skipped, not zero.
    # `realtime_start` is the start of FRED's real-time period, not a publication date, so no
    # publication date is recorded.
    return [("fred", spec.series_id, date.fromisoformat(row["date"]), canonical_decimal(row["value"]), None)
            for row in payload["observations"] if row["value"] != "."]


def _json(response: tuple[int, bytes]) -> tuple[int, dict[str, Any]]:
    status, body = response
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("not an object")
    return status, payload


def describe(series: dict[tuple[str, str], dict[date, tuple[str, date | None]]], *,
             as_of: date) -> list[dict[str, Any]]:
    """Latest value per configured series on or before `as_of`, with changes in units (percentage
    points for rates) against about 3 and 12 months earlier, and a stale flag."""
    rows = []
    for spec in SERIES:
        points = {day: value for day, value in series.get((spec.provider, spec.series_id), {}).items()
                  if day <= as_of}
        latest_day = max(points, default=None)
        row: dict[str, Any] = {"spec": spec, "observed_on": latest_day, "value": None, "published_on": None,
                               "change_3m": None, "change_12m": None, "stale": True}
        if latest_day is not None:
            value = Decimal(points[latest_day][0])
            row.update(value=value, published_on=points[latest_day][1],
                       stale=(as_of - latest_day).days > spec.max_age_days,
                       change_3m=_change(points, value, latest_day - timedelta(days=91)),
                       change_12m=_change(points, value, latest_day - timedelta(days=365)))
        rows.append(row)
    return rows


def _change(points: dict[date, tuple[str, date | None]], value: Decimal, then: date) -> Decimal | None:
    earlier = max((day for day in points if day <= then), default=None)
    # A reference point more than two weeks before the target date would misstate the period.
    if earlier is None or (then - earlier).days > 14:
        return None
    return value - Decimal(points[earlier][0])
