"""The client's Fitbit record through the Google Health API, read-only (FR-40, health design §5).

Verified against Google's docs and the API's discovery document on 2026-10-25: the service is
`https://health.googleapis.com/v4`, a data type's points are listed with `GET users/me/dataTypes/{type}/dataPoints`
and summed per civil day with `POST users/me/dataTypes/{type}/dataPoints:dailyRollUp` (body `{"range": {"start",
"end"}, "windowSizeDays": 1}`, a CivilDateTime being `{"date": {year, month, day}, "time": {hours, minutes, seconds}}`;
`StepsRollupValue.countSum`, `ActiveMinutesRollupValue.activeMinutesRollupByActivityLevel[].activeMinutesSum` with
levels LIGHT/MODERATE/VIGOROUS). The list endpoint, aggregated here, is the fallback should a roll-up be refused.

The grant is the API's own: a token that also carries Gmail, Calendar or Drive scopes is refused with 403
DISALLOWED_OAUTH_SCOPES (seen 2026-10-25), so `smith health fitbit login` consents to the three read scopes alone and
the refresh token is stored apart from the Gmail one (`credentials.load_health`).

What is kept: one small summary per day (steps, active minutes by level, exercise minutes and whether a session was
strength work, sleep minutes, resting heart rate, heart-rate variability, weight, body fat) in
`data/health/activity/days.json`, never minute-level samples. The letter reads a week of these (`weekly_summary`).
"""
import json
import os
import re
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smith import gmail

SCOPES = gmail.HEALTH_SCOPES
BASE_URL = "https://health.googleapis.com/v4/users/me/dataTypes"
DAYS_FILE = "days.json"
SYNC_FILE = "sync.json"
SYNC_EVERY_HOURS = 20
FETCH_DAYS = 14          # Each sync re-reads this many days: late-arriving syncs from the watch correct earlier days.
KEEP_DAYS = 400
PAGE_SIZE = 400
# Activity levels that count toward the guideline's moderate-to-vigorous minutes (the enum names Google uses; a level
# not listed here is kept in the raw breakdown but not summed).
MODERATE_LEVELS = ("MODERATE", "VIGOROUS", "FAIRLY_ACTIVE", "VERY_ACTIVE")
# Exercise types (discovery enum) that are strength work: WEIGHTS, FREE_WEIGHTS, WEIGHT_MACHINES, WEIGHTLIFTING,
# STRENGTH_TRAINING, FUNCTIONAL_STRENGTH_TRAINING, BODY_WEIGHT, CALISTHENICS, CROSSFIT; plus the client's own names.
STRENGTH_WORDS = ("WEIGHT", "STRENGTH", "RESISTANCE", "CALISTHENIC", "BODY_WEIGHT", "CROSSFIT", "근력", "웨이트")
AEROBIC_MINUTES_WEEK = 150
STRENGTH_DAYS_WEEK = 2
VIGOROUS_WEIGHT = 2        # The guideline counts one vigorous minute as two moderate ones (KDCA; review 2026-10-25).
MAX_PAGES = 20
# The day fields each source fills, with the value a successful read leaves when the day had none; a failed source
# leaves its old fields untouched (review 2026-10-25: a partial fetch used to wipe the day).
SOURCE_FIELDS: dict[str, dict[str, Any]] = {
    "steps": {"steps": None}, "active-minutes": {"active_minutes_by_level": None},
    "exercise": {"exercise_minutes": 0, "strength": False, "exercise_kinds": []}, "sleep": {"sleep_minutes": None},
    "daily-resting-heart-rate": {"resting_hr": None}, "daily-heart-rate-variability": {"hrv_ms": None},
    "weight": {"weight_kg": None}, "body-fat": {"body_fat_pct": None},
}
IDENTITY_URL = "https://health.googleapis.com/v4/users/me:getIdentity"


class FitbitError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code, self.message = code, message


def _message(payload: Any) -> str:
    """The API's own error text (no identifiers in it by Google's design), for the sync notes."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ""
    reasons = [str(d.get("reason") or d.get("@type", "")).rsplit(".", 1)[-1] for d in error.get("details", []) if isinstance(d, dict)]
    return " ".join(p for p in (str(error.get("status", "")), str(error.get("message", ""))[:200], " ".join(r for r in reasons if r)) if p)


def has_scope(granted: set[str]) -> bool:
    return all(scope in granted for scope in SCOPES)


def directory(folder: Path) -> Path:
    return Path(folder) / "activity"


# ---- reading the API ----------------------------------------------------------------------------------------------

FATAL = ("login-required", "no-scope", "account-not-linked")   # Nothing a retry of the next source can fix.


def fetch_days(token: str, start: date, end: date, *, getter: gmail.Getter = gmail.get, poster: gmail.Poster = gmail.post,
               notes: list[str] | None = None, failed: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Daily summaries for start..end (inclusive). Each source fails on its own: a successful source sets its fields
    on every day (its no-data value where the day had none), a failed one is listed in `failed` with a note and sets
    nothing, so the caller can keep what it already had for it. Only an auth or account failure raises."""
    notes = notes if notes is not None else []
    failed = failed if failed is not None else []
    days: dict[str, dict[str, Any]] = {d.isoformat(): {} for d in _days(start, end)}
    for data_type, field in (("steps", "steps"), ("active-minutes", "active_minutes_by_level")):
        try:
            rolled = _daily_rollup(token, data_type, start, end, poster=poster, getter=getter, notes=notes)
        except FitbitError as error:
            if error.code in FATAL:
                raise
            failed.append(data_type)
            notes.append(f"{data_type}: {error.code}" + (f" — {error.message}" if error.message else ""))
            continue
        _blank(days, data_type)
        for day, value in rolled.items():
            if day in days:
                days[day][field] = value
    for data_type, reader in (("exercise", _exercise), ("sleep", _sleep), ("daily-resting-heart-rate", _resting_hr),
                              ("daily-heart-rate-variability", _hrv), ("weight", _weight), ("body-fat", _body_fat)):
        try:
            points = _list(token, data_type, start, end, getter=getter)
        except FitbitError as error:
            if error.code in FATAL:
                raise
            failed.append(data_type)
            notes.append(f"{data_type}: {error.code}" + (f" — {error.message}" if error.message else ""))
            continue
        _blank(days, data_type)
        for point in points:
            day, fields = reader(point)
            if day in days and fields:
                _merge_day(days[day], fields)
    return days


def _blank(days: dict[str, dict[str, Any]], data_type: str) -> None:
    for fields in days.values():
        for key, empty in SOURCE_FIELDS[data_type].items():
            fields[key] = list(empty) if isinstance(empty, list) else empty


def check_linked(token: str, *, getter: gmail.Getter = gmail.get) -> tuple[bool | None, str]:
    """(linked, detail): whether the Google account has a Google Health profile linked (Fitbit migrated). Google asks
    for this check right after consent; None when the answer is not conclusive (an unexpected status)."""
    status, payload = getter(IDENTITY_URL, {"Authorization": f"Bearer {token}"})
    message = _message(payload)
    if status == 200:
        return True, ""
    if "ACCOUNT_NOT_LINKED" in message or status == 400 and "link" in message.lower():
        return False, message
    return None, f"http-{status} {message}".strip()


def _daily_rollup(token: str, data_type: str, start: date, end: date, *, poster: gmail.Poster, getter: gmail.Getter,
                  notes: list[str]) -> dict[str, Any]:
    """Per-day values from the roll-up endpoint; falls back to the list endpoint summed per day."""
    url = f"{BASE_URL}/{data_type}/dataPoints:dailyRollUp"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"range": {"start": _civil(start), "end": _civil(end + timedelta(days=1))}, "windowSizeDays": 1}
    status, payload = poster(url, json.dumps(body).encode("utf-8"), headers)
    if status == 200:
        found = {}
        for point in payload.get("rollupDataPoints", []) if isinstance(payload, dict) else []:
            day = _civil_day(point.get("civilStartTime"))
            if day is None:
                continue
            if data_type == "steps":
                found[day] = _int((point.get("steps") or {}).get("countSum"))
            else:
                levels = (point.get("activeMinutes") or {}).get("activeMinutesRollupByActivityLevel") or []
                found[day] = {str(l.get("activityLevel")): _int(l.get("activeMinutesSum")) for l in levels if isinstance(l, dict)}
        return found
    _raise_for(status, payload, retryable_400=True)
    notes.append(f"{data_type}: rollup rejected ({status}: {_message(payload)})")
    # The list endpoint: minute-level points summed here (the page cap bounds the cost).
    found: dict[str, Any] = {}
    for point in _list(token, data_type, start, end, getter=getter, page_size=10000):
        day = _point_day(point)
        if day is None:
            continue
        if data_type == "steps":
            found[day] = (found.get(day) or 0) + _int((point.get("steps") or {}).get("count"))
        else:
            levels = (point.get("activeMinutes") or {}).get("activeMinutesByActivityLevel") or []
            bucket = found.setdefault(day, {})
            for level in levels if isinstance(levels, list) else []:
                name = str(level.get("activityLevel"))
                bucket[name] = bucket.get(name, 0) + _int(level.get("activeMinutes") or level.get("minutes"))
    notes.append(f"{data_type}: summed from the list endpoint")
    return found


# The list filter field per data type (discovery `filter` description 2026-10-25): intervals by civil start, a night
# of sleep by its civil end, daily summaries by date, samples by civil sample time. Newest first in the response.
_FILTER_FIELDS = {"exercise": "exercise.interval.civil_start_time", "sleep": "sleep.interval.civil_end_time",
                  "daily-resting-heart-rate": "daily_resting_heart_rate.date", "daily-heart-rate-variability": "daily_heart_rate_variability.date",
                  "weight": "weight.sample_time.civil_time", "body-fat": "body_fat.sample_time.civil_time",
                  "steps": "steps.interval.civil_start_time", "active-minutes": "active_minutes.interval.civil_start_time"}


def _list(token: str, data_type: str, start: date, end: date, *, getter: gmail.Getter, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    """The type's points for start..end, newest first, following `nextPageToken` (exercise and sleep pages hold 25
    sessions at most) up to MAX_PAGES."""
    field = _FILTER_FIELDS[data_type]
    after = (end + timedelta(days=1)).isoformat()
    found: list[dict[str, Any]] = []
    token_param: dict[str, str] = {}
    for _ in range(MAX_PAGES):
        query = urllib.parse.urlencode({"pageSize": page_size, "filter": f'{field} >= "{start.isoformat()}" AND {field} < "{after}"',
                                        **token_param})
        status, payload = getter(f"{BASE_URL}/{data_type}/dataPoints?{query}", {"Authorization": f"Bearer {token}"})
        _raise_for(status, payload)
        points = payload.get("dataPoints", []) if isinstance(payload, dict) else []
        found += [p for p in points if isinstance(p, dict)]
        next_token = payload.get("nextPageToken") if isinstance(payload, dict) else None
        if not next_token:
            break
        token_param = {"pageToken": str(next_token)}
    return found


def _raise_for(status: int, payload: Any, *, retryable_400: bool = False) -> None:
    if status == 200:
        return
    message = _message(payload)
    if "ACCOUNT_NOT_LINKED" in message:
        raise FitbitError("account-not-linked", message)
    if status == 401:
        raise FitbitError("login-required", message)
    if status == 403:
        raise FitbitError("no-scope", message)
    if status == 429:
        raise FitbitError("rate-limited", message)
    if status == 400 and retryable_400:
        return
    raise FitbitError(f"http-{status}", message)


def _civil(day: date) -> dict[str, Any]:
    """A CivilDateTime at midnight (discovery: `date` is a google.type.Date, `time` a TimeOfDay)."""
    return {"date": {"year": day.year, "month": day.month, "day": day.day}, "time": {"hours": 0, "minutes": 0, "seconds": 0}}


def _civil_day(value: Any) -> str | None:
    """The civil day of a Date, a CivilDateTime ({"date": Date, "time": ...}), an ObservationSampleTime ({"civilTime":
    CivilDateTime, "physicalTime": RFC 3339}) or an RFC 3339 string."""
    if isinstance(value, dict):
        if isinstance(value.get("date"), dict):
            return _civil_day(value["date"])
        if isinstance(value.get("civilTime"), dict):
            return _civil_day(value["civilTime"])
        if all(k in value for k in ("year", "month", "day")):
            try:
                return date(int(value["year"]), int(value["month"]), int(value["day"])).isoformat()
            except (TypeError, ValueError):
                return None
        if isinstance(value.get("physicalTime"), str):
            return _day_of(value["physicalTime"])
        return None
    if isinstance(value, str):
        return _day_of(value)
    return None


def _point_day(point: dict[str, Any], *, end: bool = False) -> str | None:
    """The civil day of a point from whatever time field its type carries: a daily type's `date`, an interval's civil
    start (or end, for a night of sleep), a sample's civil time."""
    for holder in (point, *[v for v in point.values() if isinstance(v, dict)]):
        if not isinstance(holder, dict):
            continue
        found = _civil_day(holder.get("date"))
        if found:
            return found
        interval = holder.get("interval")
        if isinstance(interval, dict):
            keys = ("civilEndTime", "endTime") if end else ("civilStartTime", "startTime")
            for key in keys:
                found = _civil_day(interval.get(key))
                if found:
                    return found
        for key in ("sampleTime", "startTime"):
            found = _civil_day(holder.get(key))
            if found:
                return found
    return None


def _day_of(stamp: str) -> str | None:
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", stamp)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None


def _seconds(value: Any) -> int:
    """A protobuf Duration ("1800s") or a number of seconds."""
    if isinstance(value, str) and value.endswith("s"):
        try:
            return int(float(value[:-1]))
        except ValueError:
            return 0
    return _int(value)


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _minutes_between(interval: Any) -> int | None:
    if not isinstance(interval, dict):
        return None
    try:
        start = datetime.fromisoformat(str(interval.get("startTime")).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(interval.get("endTime")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0, int((end - start).total_seconds() // 60))


def _exercise(point: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    body = point.get("exercise") if isinstance(point.get("exercise"), dict) else point
    minutes = _seconds(body.get("activeDuration")) // 60 if body.get("activeDuration") else (_minutes_between(body.get("interval")) or 0)
    kind = " ".join(str(body.get(k, "")) for k in ("exerciseType", "displayName")).upper()
    return _point_day(point), {"exercise_minutes": minutes, "strength": any(w in kind for w in STRENGTH_WORDS),
                               "exercise_kinds": [kind.strip()[:40]] if kind.strip() else []}


def _sleep(point: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Minutes asleep for the night, booked on the morning it ends (the letter's "last night"): the summary's
    `minutesAsleep` when present, else the session minus its AWAKE stages (stages carry their own start/end)."""
    body = point.get("sleep") if isinstance(point.get("sleep"), dict) else point
    summary = body.get("summary") if isinstance(body.get("summary"), dict) else {}
    end_day = _point_day(point, end=True)
    if summary.get("minutesAsleep") is not None:
        return end_day, {"sleep_minutes": _int(summary["minutesAsleep"])}
    total = _minutes_between(body.get("interval"))
    if total is None:
        return None, {}
    awake = 0
    for stage in body.get("stages") or []:
        if isinstance(stage, dict) and str(stage.get("type", stage.get("stage", ""))).upper() == "AWAKE":
            awake += _minutes_between(stage if "startTime" in stage else stage.get("interval")) or 0
    return end_day, {"sleep_minutes": max(0, total - awake)}


def _resting_hr(point: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    body = point.get("dailyRestingHeartRate") if isinstance(point.get("dailyRestingHeartRate"), dict) else point
    value = body.get("beatsPerMinute")
    return _point_day(point), ({"resting_hr": _int(value)} if value is not None else {})


def _hrv(point: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    body = point.get("dailyHeartRateVariability") if isinstance(point.get("dailyHeartRateVariability"), dict) else point
    value = next((body.get(k) for k in ("averageHeartRateVariabilityMilliseconds",
                                        "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds", "rmssdMilliseconds")
                  if body.get(k) is not None), None)
    return _point_day(point), ({"hrv_ms": round(float(value), 1)} if value is not None else {})


def _weight(point: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    body = point.get("weight") if isinstance(point.get("weight"), dict) else point
    grams = body.get("weightGrams")
    return _point_day(point), ({"weight_kg": round(_int(grams) / 1000, 1)} if grams else {})


def _body_fat(point: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    body = point.get("bodyFat") if isinstance(point.get("bodyFat"), dict) else point
    value = body.get("percentage")
    return _point_day(point), ({"body_fat_pct": round(float(value), 1)} if value is not None else {})


def _merge_day(day: dict[str, Any], fields: dict[str, Any]) -> None:
    for key, value in fields.items():
        if key in ("exercise_minutes", "sleep_minutes"):
            day[key] = (day.get(key) or 0) + value
        elif key == "strength":
            day[key] = bool(day.get(key)) or value
        elif key == "exercise_kinds":
            day[key] = (day.get(key) or []) + value
        else:
            day[key] = value


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# ---- storage -------------------------------------------------------------------------------------------------------

def load_days(folder: Path) -> dict[str, dict[str, Any]]:
    file = directory(folder) / DAYS_FILE
    if not file.exists():
        return {}
    try:
        raw = json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}


def sync_state(folder: Path) -> dict[str, Any]:
    file = directory(folder) / SYNC_FILE
    try:
        raw = json.loads(file.read_text(encoding="utf-8-sig")) if file.exists() else {}
    except (OSError, ValueError):
        raw = {}
    return raw if isinstance(raw, dict) else {}


def sync_due(folder: Path, now: datetime) -> bool:
    """Once a day, counted from the last attempt, so a failing sync is not retried at every scheduler tick."""
    last = sync_state(folder).get("checked_at")
    try:
        when = datetime.fromisoformat(str(last)) if last else None
    except ValueError:
        when = None
    return when is None or (now - when).total_seconds() >= SYNC_EVERY_HOURS * 3600


def sync(folder: Path, token: str, *, now: datetime, getter: gmail.Getter = gmail.get, poster: gmail.Poster = gmail.post) -> dict[str, Any]:
    """Fetch the last FETCH_DAYS days and merge them into the stored days (a re-read replaces a day: the watch syncs
    late). Never raises for an API failure; the outcome and notes are recorded. Returns {"outcome": "synced" |
    "error", "days", "error_code", "notes"}."""
    today = now.astimezone().date() if now.tzinfo else now.date()
    start, end = today - timedelta(days=FETCH_DAYS - 1), today
    notes: list[str] = []
    failed: list[str] = []
    try:
        fresh = fetch_days(token, start, end, getter=getter, poster=poster, notes=notes, failed=failed)
    except FitbitError as error:
        notes.append(f"stopped: {error.code}" + (f" — {error.message}" if error.message else ""))
        _write(directory(folder) / SYNC_FILE, {**sync_state(folder), "checked_at": now.isoformat(), "last_error": error.code,
                                                "last_error_at": now.isoformat(), "notes": notes})
        return {"outcome": "error", "days": 0, "error_code": error.code, "notes": notes, "failed": failed}
    stored = load_days(folder)
    for day, fields in fresh.items():
        if fields:
            stored[day] = {**stored.get(day, {}), **fields}   # A failed source's old fields stay; the others are replaced.
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    stored = {k: v for k, v in sorted(stored.items()) if k >= cutoff and any(v.get(f) not in (None, 0, False, []) for f in v)}
    outcome = "partial" if failed else "synced"
    _write(directory(folder) / DAYS_FILE, stored)
    _write(directory(folder) / SYNC_FILE, {"checked_at": now.isoformat(), "synced_at": now.isoformat(),
                                            "last_error": "partial-" + ",".join(failed) if failed else None,
                                            "last_error_at": now.isoformat() if failed else None, "notes": notes, "days": len(stored)})
    return {"outcome": outcome, "days": len(stored), "error_code": ("partial-" + ",".join(failed)) if failed else None,
            "notes": notes, "failed": failed}


def _write(file: Path, payload: Any) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, file)


# ---- what the letter reads --------------------------------------------------------------------------------------

def weekly_summary(folder: Path, start: date, end: date) -> dict[str, Any] | None:
    """One week (start..end inclusive) against the guideline, from the stored days; None when no day of that week is
    on file (the letter then estimates from the routine). Resting heart rate is set beside its 4-week baseline."""
    days = load_days(folder)
    week = {d: days[d] for d in (x.isoformat() for x in _days(start, end)) if d in days and days[d]}
    if not week:
        return None
    aerobic = 0
    strength_days = 0
    steps, sleeps, hrs = [], [], []
    for fields in week.values():
        levels = fields.get("active_minutes_by_level") or {}
        moderate = 0
        if isinstance(levels, dict):
            for key, value in levels.items():
                name = key.upper()
                if "VIGOROUS" in name or "VERY" in name:
                    moderate += VIGOROUS_WEIGHT * _int(value)     # Moderate-equivalent minutes.
                elif any(m in name for m in MODERATE_LEVELS):
                    moderate += _int(value)
        aerobic += moderate if moderate else _int(fields.get("exercise_minutes", 0))
        strength_days += 1 if fields.get("strength") else 0
        if fields.get("steps") is not None:
            steps.append(fields["steps"])
        if fields.get("sleep_minutes"):
            sleeps.append(fields["sleep_minutes"])
        if fields.get("resting_hr"):
            hrs.append(fields["resting_hr"])
    baseline_days = [days[d] for d in (x.isoformat() for x in _days(start - timedelta(days=28), start - timedelta(days=1)))
                     if d in days and days[d].get("resting_hr")]
    baseline = round(sum(d["resting_hr"] for d in baseline_days) / len(baseline_days), 1) if baseline_days else None
    latest_weight = next((week[d]["weight_kg"] for d in sorted(week, reverse=True) if week[d].get("weight_kg")), None)
    state = sync_state(folder)
    return {"source": "fitbit", "week": [start.isoformat(), end.isoformat()], "days_with_data": len(week),
            "aerobic_minutes": aerobic, "strength_days": strength_days,
            "steps_avg": round(sum(steps) / len(steps)) if steps else None,
            "sleep_avg_minutes": round(sum(sleeps) / len(sleeps)) if sleeps else None,
            "resting_hr_avg": round(sum(hrs) / len(hrs), 1) if hrs else None, "resting_hr_baseline": baseline,
            "weight_latest_kg": latest_weight,
            "guideline": {"aerobic_minutes": AEROBIC_MINUTES_WEEK, "strength_days": STRENGTH_DAYS_WEEK,
                          "source": "보건복지부·한국건강증진개발원, 한국인을 위한 신체활동 지침서(2023 개정)"},
            "meets_aerobic": aerobic >= AEROBIC_MINUTES_WEEK, "meets_strength": strength_days >= STRENGTH_DAYS_WEEK,
            "stale": bool(state.get("last_error")), "synced_at": state.get("synced_at")}


def describe(folder: Path) -> list[str]:
    """Lines for `smith health fitbit status`: the sync state and the last few stored days."""
    state = sync_state(folder)
    days = load_days(folder)
    lines = [f"synced_at {state.get('synced_at') or '-'} · checked_at {state.get('checked_at') or '-'} · "
             f"{len(days)} day(s) stored" + (f" · last error {state['last_error']}" if state.get("last_error") else "")]
    for note in state.get("notes") or []:
        lines.append(f"  note: {note}")
    for day in sorted(days)[-7:]:
        f = days[day]
        levels = f.get("active_minutes_by_level") or {}
        kinds = ",".join(f.get("exercise_kinds") or [])
        lines.append(f"  {day}  steps {f.get('steps', '-')}  active {levels if levels else '-'}  exercise {f.get('exercise_minutes', 0)}min"
                     f"{' [' + kinds + ']' if kinds else ''}{' 근력' if f.get('strength') else ''}  sleep {f.get('sleep_minutes', '-')}min"
                     f"  rhr {f.get('resting_hr', '-')}  hrv {f.get('hrv_ms', '-')}  weight {f.get('weight_kg', '-')}")
    return lines
