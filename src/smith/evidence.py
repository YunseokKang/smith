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
    period: str = "D"  # ECOS cycle: "D" daily or "M" monthly (observed_on is the first day of the month).
    # A required series that comes back empty fails the provider (a renamed code must not leave old values
    # looking fresh). An optional one is simply absent that day: used for a judgement that says "unknown".
    required: bool = True
    # A policy rate set in steps at meetings: describe() lists its moves (date, size) over the past year, so a
    # report can say how the rate got where it is (report design §28).
    policy: bool = False

    @property
    def link(self) -> str:
        """The URL part of `source_url`, without the readable series code."""
        return self.source_url.split(" ", 1)[0]


BOK_BASE_RATE = "722Y001/0101000"
FED_TARGET_UPPER = "DFEDTARU"
# Bank of Korea rate-setting meetings (통화정책방향 결정회의), eight a year. The 2026 dates are the Bank's published
# schedule (https://www.bok.or.kr/portal/singl/crncyPolicyDrcMtg/listYear.do?mtgSe=A&menuNo=200755, read on
# 2026-10-22); the stored base-rate series steps on 2026-07-16 and 2026-08-27 fall on two of them. Add each new
# year's dates when the Bank publishes them (December). Once the table is behind the report day,
# next_policy_meeting() returns None and the reports say nothing about the next meeting rather than guess.
BOK_RATE_MEETINGS = (date(2026, 1, 15), date(2026, 2, 26), date(2026, 4, 10), date(2026, 5, 28), date(2026, 7, 16),
                     date(2026, 8, 27), date(2026, 10, 22), date(2026, 11, 26))

SERIES = (
    SeriesSpec("ecos", BOK_BASE_RATE, "Bank of Korea base rate", "%",
               "https://ecos.bok.or.kr/ (722Y001 0101000)", 10, policy=True),
    SeriesSpec("ecos", "817Y002/010200000", "Korea treasury 3Y", "%", "https://ecos.bok.or.kr/ (817Y002 010200000)", 10),
    SeriesSpec("ecos", "817Y002/010210000", "Korea treasury 10Y", "%", "https://ecos.bok.or.kr/ (817Y002 010210000)", 10),
    SeriesSpec("ecos", "817Y002/010502000", "Korea CD 91D", "%", "https://ecos.bok.or.kr/ (817Y002 010502000)", 10),
    SeriesSpec("ecos", "731Y001/0000001", "KRW per USD (BOK reference rate)", "KRW",
               "https://ecos.bok.or.kr/ (731Y001 0000001)", 10),
    # Deposit banks' weighted average mortgage rate on new loans (monthly, published about a month later):
    # the market band a household mortgage is compared with for the refinancing verdict (report design §18).
    # Code checked against two independent integrations and the live API on 2026-10-18. Optional: a wrong or
    # renamed code leaves the verdict "unknown" instead of failing the provider. A month's figure is published
    # about eight weeks after the month starts, so the newest one is normally up to ~88 days old; beyond 100
    # days a release was missed and the figure is stale.
    SeriesSpec("ecos", "121Y006/BECBLA0302", "Korea bank mortgage rate (new loans, weighted average)", "%",
               "https://ecos.bok.or.kr/ (121Y006 BECBLA0302)", 100, period="M", required=False),
    SeriesSpec("fred", FED_TARGET_UPPER, "Fed funds target upper", "%", "https://fred.stlouisfed.org/series/DFEDTARU", 10,
               policy=True),
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
        # Every required series must answer. An empty one (a renamed code, a provider change)
        # must not leave old values looking freshly confirmed. An optional series may be absent.
        if not series and spec.required:
            raise EvidenceError(provider, f"missing-series:{spec.series_id}")
        points.extend(series)
    return points


def _fetch_ecos(spec: SeriesSpec, key: str, start: date, end: date, get: Getter) -> list[Point]:
    stat, item = spec.series_id.split("/")
    monthly = spec.period == "M"
    first, last = (f"{start:%Y%m}", f"{end:%Y%m}") if monthly else (f"{start:%Y%m%d}", f"{end:%Y%m%d}")
    status, payload = _json(get(f"https://ecos.bok.or.kr/api/StatisticSearch/{urllib.parse.quote(key)}/json/kr/"
                                f"1/1000/{stat}/{spec.period}/{first}/{last}/{item}"))
    if "RESULT" in payload:  # ECOS reports errors and empty results in-band.
        code = payload["RESULT"]["CODE"]
        if code == "INFO-200":  # No data in the requested range; the caller treats it as missing.
            return []
        raise EvidenceError("ecos", str(code))
    if status != 200:
        raise EvidenceError("ecos", f"http-{status}")
    # TIME is YYYYMMDD for a daily series and YYYYMM for a monthly one (the first day of that month).
    return [("ecos", spec.series_id, _ecos_time(row["TIME"]), canonical_decimal(row["DATA_VALUE"]), None)
            for row in payload["StatisticSearch"]["row"] if row.get("DATA_VALUE") not in (None, "")]


def _ecos_time(text: str) -> date:
    return datetime.strptime(text, "%Y%m" if len(text) == 6 else "%Y%m%d").date()


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
    points for rates) against about 3 and 12 months earlier, a stale flag, and for a policy rate its steps
    over the past year (`moves`, empty for the other series)."""
    rows = []
    for spec in SERIES:
        points = {day: value for day, value in series.get((spec.provider, spec.series_id), {}).items()
                  if day <= as_of}
        latest_day = max(points, default=None)
        row: dict[str, Any] = {"spec": spec, "observed_on": latest_day, "value": None, "published_on": None,
                               "change_3m": None, "change_12m": None, "stale": True, "moves": []}
        if latest_day is not None:
            value = Decimal(points[latest_day][0])
            row.update(value=value, published_on=points[latest_day][1],
                       stale=(as_of - latest_day).days > spec.max_age_days,
                       change_3m=_change(points, value, latest_day - timedelta(days=91)),
                       change_12m=_change(points, value, latest_day - timedelta(days=365)),
                       moves=_moves(points, latest_day) if spec.policy else [])
        rows.append(row)
    return rows


def _moves(points: dict[date, tuple[str, date | None]], latest_day: date) -> list[tuple[date, Decimal]]:
    """The steps of a policy rate in the year to `latest_day`: (date, size) wherever the value differs from the
    previous observation. The comparison starts from the last observation before the window, so a step on the
    window's first day counts too."""
    start = latest_day - timedelta(days=365)
    before = max((day for day in points if day < start), default=None)
    previous = None if before is None else Decimal(points[before][0])
    moves = []
    for day in sorted(day for day in points if start <= day <= latest_day):
        value = Decimal(points[day][0])
        if previous is not None and value != previous:
            moves.append((day, value - previous))
        previous = value
    return moves


def next_policy_meeting(today: date, meetings: tuple[date, ...] = BOK_RATE_MEETINGS) -> date | None:
    """The Bank of Korea rate meeting on or after `today`, or None when the published table has no later date."""
    return next((day for day in meetings if day >= today), None)


def _change(points: dict[date, tuple[str, date | None]], value: Decimal, then: date) -> Decimal | None:
    earlier = max((day for day in points if day <= then), default=None)
    # A reference point more than two weeks before the target date would misstate the period.
    if earlier is None or (then - earlier).days > 14:
        return None
    return value - Decimal(points[earlier][0])
