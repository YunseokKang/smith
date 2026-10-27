"""Validate bootstrap settings; this module has no external side effects."""
import re
from datetime import datetime, time
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from smith import editions

# The household's local timezone for commands that run without a config file (preview, advise, summary).
DEFAULT_TIMEZONE = "Asia/Seoul"
WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
# The client's stated risk appetite, passed to the narrative (FR-07: growth-oriented by default).
RISK_PREFERENCES = ("conservative", "balanced", "growth", "growth_aggressive")
CONFIG_VERSION = 1
# Every known key: a typo (e.g. `answer_reply`) is an error instead of a silently ignored setting.
KNOWN_KEYS = {
    "app": {"timezone", "financial_access"},
    "advice": {"risk_preference", "include_alternatives", "require_macro_context"},
    "privacy": {"remove_personal_identifiers"},
    "reports": {"enabled", "weekdays", "time", "delivery", "editions"},
    "mail": {"recipient", "answer_replies"},
    "household": {"birth_year", "partner_birth_year", "retirement_monthly_spend", "marriage_registered", "cohabiting",
                  "annual_gross_salary", "retirement_age", "emergency_months",
                  "emergency_months_confirmed"},
    "backup": {"dir", "keep"},
    # Same-day event notes (report design §29): the daily check time and the holdings' move that counts.
    "events": {"enabled", "check_time", "portfolio_move_pct"},
    # The daily-routine mail (daily design §2, §3.1, §7): schedule, news topics, the district for weather and the
    # household's airway sensitivity that moves air-quality and pollen cautions one grade earlier.
    "daily": {"enabled", "time", "weekdays", "news_count", "news_topics", "airways_sensitive", "weather", "contact_reminders"},
    # The weekly health letter (health design §3, §9): the Sunday plan, the optional midweek check, the rotation
    # of topics and the client's priority goals shown on the dashboard line.
    "health": {"enabled", "weekday", "time", "topics", "goals", "midweek"},
    # The evening 내일 준비 mail (daily design §16): its time and days, how the reader calls his wife, the talk kinds.
    "evening": {"enabled", "time", "weekdays", "partner", "talk_kinds", "commute_hours", "rest_hours", "netflix", "sleep"},
}
HEALTH_MIDWEEK_KEYS = {"enabled", "weekday", "time"}
HEALTH_TOPICS = ("vascular", "uric-weight", "nutrition-gut", "balance", "stomach-habits")
HEALTH_GOALS = ("posture", "body_fat", "ldl", "uric_acid", "vitamin_d", "weight", "blood_pressure", "glucose")
DAILY_WEATHER_KEYS = {"lat", "lon", "air_station", "air_region", "pollen_area_no", "warning_keywords", "label"}
DAILY_WEATHER_OPTIONAL = {"label"}   # The district name the mail shows as the weather's basis ("성남시 분당구").
# `lease_exit`: how the client plans to return a lease deposit on this home ("relet": a new tenant, the default;
# "sell": the home is sold when the lease ends, client e-mail 2026-10-19). `residence_years`: whole years the
# owner's household has lived in the home (client e-mail 2026-10-21): with two or more, and two years held, the
# one-home exemption is the figure the reports lead with.
PROPERTY_KEYS = {"lawd_cd", "dong", "apt_name", "exclusive_area_m2", "acquired_year", "acquired_price", "lease_exit",
                 "residence_years"}
# `[targets."<key>"]`: the complexes the household may move to (report design §18.3). Same official-data
# matching as a property, plus a label for the report, a region (city/district) for research questions, and
# a reference price (an asking or sale-launch price the client enters) for a complex with no recorded trades.
TARGET_KEYS = {"label", "lawd_cd", "dong", "apt_name", "exclusive_area_m2", "region", "reference_price"}


def local_time(moment: datetime, tz_name: str = DEFAULT_TIMEZONE) -> datetime:
    """The same instant in the household timezone. Calculations take calendar dates (cash-flow start
    and end, goal dates) from `as_of.date()`, so `as_of` must be local: 06:00 KST is the previous day
    in UTC. Storage still normalizes every time to UTC."""
    return moment.astimezone(ZoneInfo(tz_name))


def load_config(path: Path) -> dict:
    # Notepad and PowerShell 5.1 write a UTF-8 BOM, which tomllib rejects; accept it for edited files.
    config = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    _check_keys(config)
    for section in ("app", "advice", "privacy", "reports"):
        if not isinstance(config.get(section), dict):
            raise ValueError(f"Missing section: {section}")
    app, advice, privacy, reports = (config[s] for s in ("app", "advice", "privacy", "reports"))
    if app.get("financial_access") != "read_only":
        raise ValueError("Financial access must remain read_only")
    try:
        ZoneInfo(app.get("timezone", ""))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("Invalid timezone") from None
    if advice.get("risk_preference", "growth_aggressive") not in RISK_PREFERENCES:
        raise ValueError(f"advice.risk_preference must be one of {', '.join(RISK_PREFERENCES)}")
    for section, field in ((privacy, "remove_personal_identifiers"),
                           (advice, "include_alternatives"), (advice, "require_macro_context")):
        if section.get(field) is not True:
            raise ValueError(f"{field} must be true")
    if not isinstance(reports.get("enabled"), bool):
        raise ValueError("reports.enabled must be a boolean")
    days = reports.get("weekdays")
    if (not isinstance(days, list) or not days
            or any(not isinstance(d, str) or d not in WEEKDAYS for d in days)
            or len(set(days)) != len(days)):
        raise ValueError("weekdays must contain unique weekday names")
    clock = reports.get("time")
    if not isinstance(clock, str) or len(clock) != 5 or clock[2] != ":":
        raise ValueError("Report time must be HH:MM")
    try:
        parsed = time.fromisoformat(clock)
        if parsed.tzinfo is not None:
            raise ValueError()
    except ValueError:
        raise ValueError("Report time must be a valid HH:MM") from None
    if reports.get("delivery") != "gmail":
        raise ValueError("Only the planned gmail delivery is supported by this scaffold")
    plan = reports.get("editions", {})
    if not isinstance(plan, dict):
        raise ValueError("reports.editions must be a table of weekday = edition")
    for day, edition in plan.items():
        # A plan may name days that are not scheduled now (a day paused in `weekdays` keeps its theme).
        if day not in WEEKDAYS:
            raise ValueError(f"reports.editions.{day}: not a weekday name")
        if edition not in editions.KINDS:
            raise ValueError(f"reports.editions.{day} must be one of {', '.join(editions.KINDS)}")
    mail = config.get("mail", {})
    if not isinstance(mail, dict) or ("recipient" in mail and not _is_email(mail["recipient"])):
        raise ValueError("mail.recipient must be one email address")
    if not isinstance(mail.get("answer_replies", False), bool):
        raise ValueError("mail.answer_replies must be a boolean")
    _check_household(config.get("household", {}))
    _check_backup(config.get("backup", {}))
    _check_events(config.get("events", {}))
    _check_daily(config.get("daily", {}))
    _check_health(config.get("health", {}))
    _check_evening(config.get("evening", {}))
    if "properties" in config or "targets" in config:
        from smith.realestate import RealEstateError, validate_properties, validate_targets
        try:
            validate_properties(config.get("properties") or {})
            validate_targets(config.get("targets") or {})
        except RealEstateError as error:
            raise ValueError(f"Invalid property setting ({error.code})") from None
    return config


def _check_keys(config: dict) -> None:
    if config.get("config_version", CONFIG_VERSION) != CONFIG_VERSION:
        raise ValueError(f"config_version must be {CONFIG_VERSION}")
    unknown = sorted(set(config) - set(KNOWN_KEYS) - {"properties", "targets", "config_version"})
    for section, keys in KNOWN_KEYS.items():
        if isinstance(config.get(section), dict):
            unknown += [f"{section}.{key}" for key in sorted(set(config[section]) - keys)]
    properties = config.get("properties")
    if isinstance(properties, dict):
        unknown += [f"properties.{ref}.{key}" for ref, spec in sorted(properties.items()) if isinstance(spec, dict)
                    for key in sorted(set(spec) - PROPERTY_KEYS)]
    targets = config.get("targets")
    if isinstance(targets, dict):
        unknown += [f"targets.{ref}.{key}" for ref, spec in sorted(targets.items()) if isinstance(spec, dict)
                    for key in sorted(set(spec) - TARGET_KEYS)]
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(unknown)}")


def _check_household(household: object) -> None:
    """Optional `[household]` facts: wrong types would silently change tax and retirement figures."""
    if not isinstance(household, dict):
        raise ValueError("household must be a table")
    for field in ("birth_year", "partner_birth_year"):
        value = household.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 1900 <= value <= 2100):
            raise ValueError(f"household.{field} must be a year")
    for field in ("retirement_monthly_spend", "annual_gross_salary"):
        amount = household.get(field)
        if amount is not None and (not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0):
            raise ValueError(f"household.{field} must be a positive whole number of won")
    for field in ("marriage_registered", "cohabiting", "emergency_months_confirmed"):
        if not isinstance(household.get(field, False), bool):
            raise ValueError(f"household.{field} must be a boolean")
    age = household.get("retirement_age")
    if age is not None and (not isinstance(age, int) or isinstance(age, bool) or not 40 <= age <= 90):
        raise ValueError("household.retirement_age must be an age from 40 to 90")
    months = household.get("emergency_months")
    if months is not None and (not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= 12):
        raise ValueError("household.emergency_months must be a whole number of months from 1 to 12")


def _check_backup(backup: object) -> None:
    """Optional `[backup]`: where `run-due` copies the ledger after each sent report, and how many to keep."""
    if not isinstance(backup, dict):
        raise ValueError("backup must be a table")
    if "dir" in backup and (not isinstance(backup["dir"], str) or not backup["dir"].strip()):
        raise ValueError("backup.dir must be a folder path")
    keep = backup.get("keep", 1)
    if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= 1000:
        raise ValueError("backup.keep must be a whole number from 1 to 1000")


def _check_events(events: object) -> None:
    """Optional `[events]` (report design §29): whether same-day event notes go out, the local time of the daily
    check, and the day's move of the listed holdings (percent) that counts as an event."""
    if not isinstance(events, dict):
        raise ValueError("events must be a table")
    if not isinstance(events.get("enabled", False), bool):
        raise ValueError("events.enabled must be a boolean")
    clock = events.get("check_time", "16:10")
    try:
        if not isinstance(clock, str) or len(clock) != 5 or time.fromisoformat(clock).tzinfo is not None:
            raise ValueError()
    except ValueError:
        raise ValueError("events.check_time must be HH:MM") from None
    pct = events.get("portfolio_move_pct", 2.0)
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0.5 <= pct <= 20:
        raise ValueError("events.portfolio_move_pct must be a number from 0.5 to 20")


def _check_evening(evening: object) -> None:
    """Optional `[evening]` (daily design §16): whether the 내일 준비 mail goes out, its local time and days, the word
    the mail uses for the reader's wife, and which talk kinds rotate (default all six)."""
    from smith.talk import KINDS

    if not isinstance(evening, dict):
        raise ValueError("evening must be a table")
    for key in ("enabled", "netflix", "sleep"):
        if not isinstance(evening.get(key, False), bool):
            raise ValueError(f"evening.{key} must be a boolean")
    clock = evening.get("time", "21:00")
    try:
        if not isinstance(clock, str) or len(clock) != 5 or time.fromisoformat(clock).tzinfo is not None:
            raise ValueError()
    except ValueError:
        raise ValueError("evening.time must be HH:MM") from None
    days = evening.get("weekdays", sorted(WEEKDAYS))
    if not isinstance(days, list) or not days or any(d not in WEEKDAYS for d in days):
        raise ValueError("evening.weekdays must list weekday names")
    partner = evening.get("partner", "아내")
    if not isinstance(partner, str) or not partner.strip() or len(partner) > 10:
        raise ValueError("evening.partner must be a short word such as 아내")
    kinds = evening.get("talk_kinds", list(KINDS))
    if not isinstance(kinds, list) or not kinds or any(k not in KINDS for k in kinds):
        raise ValueError(f"evening.talk_kinds must list some of {', '.join(KINDS)}")
    for key in ("commute_hours", "rest_hours"):   # Two [start, end] hour pairs: morning and evening.
        spans = evening.get(key, [[7, 9], [18, 20]])
        if (not isinstance(spans, list) or len(spans) != 2
                or any(not isinstance(s, list) or len(s) != 2 or any(isinstance(h, bool) or not isinstance(h, int) for h in s)
                       or not 0 <= s[0] <= s[1] <= 23 for s in spans)):
            raise ValueError(f"evening.{key} must be two [start, end] hour pairs such as [[7, 9], [18, 20]]")


def _check_daily(daily: object) -> None:
    """Optional `[daily]` (daily design §7): the morning mail's schedule and news topics, plus `[daily.weather]`, the
    district-level location every weather source is asked about (a point for the KMA grid, an AirKorea station, a
    dust-forecast region, a pollen area code, the words that make a warning relevant)."""
    if not isinstance(daily, dict):
        raise ValueError("daily must be a table")
    for key in ("enabled", "airways_sensitive"):
        if not isinstance(daily.get(key, False), bool):
            raise ValueError(f"daily.{key} must be a boolean")
    clock = daily.get("time", "06:00")
    try:
        if not isinstance(clock, str) or len(clock) != 5 or time.fromisoformat(clock).tzinfo is not None:
            raise ValueError()
    except ValueError:
        raise ValueError("daily.time must be HH:MM") from None
    days = daily.get("weekdays", sorted(WEEKDAYS))
    if not isinstance(days, list) or not days or any(d not in WEEKDAYS for d in days):
        raise ValueError("daily.weekdays must list weekday names")
    count = daily.get("news_count", 5)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
        raise ValueError("daily.news_count must be a whole number from 1 to 10")
    topics = daily.get("news_topics", [])
    if not isinstance(topics, list) or len(topics) > 10 or any(not isinstance(t, str) or not t.strip() for t in topics):
        raise ValueError("daily.news_topics must be up to 10 short texts")
    if daily.get("contact_reminders", "noted") not in ("starred", "noted", "all"):
        raise ValueError("daily.contact_reminders must be starred, noted or all")
    if "weather" not in daily:
        return
    spot = daily["weather"]
    if not isinstance(spot, dict):
        raise ValueError("daily.weather must be a table")
    unknown = sorted(set(spot) - DAILY_WEATHER_KEYS)
    missing = sorted(DAILY_WEATHER_KEYS - DAILY_WEATHER_OPTIONAL - set(spot))
    if unknown or missing:
        raise ValueError("daily.weather: " + "; ".join(p for p in (f"unknown {', '.join(unknown)}" if unknown else "",
                                                                 f"missing {', '.join(missing)}" if missing else "") if p))
    lat, lon = spot["lat"], spot["lon"]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lat, lon)) or not (33 <= lat <= 39 and 124 <= lon <= 132):
        raise ValueError("daily.weather.lat/lon must be a point in Korea (a district office, not a home address)")
    for key in ("air_station", "air_region", "pollen_area_no", "label"):
        if key in spot and (not isinstance(spot[key], str) or not spot[key].strip()):
            raise ValueError(f"daily.weather.{key} must be text")
    words = spot["warning_keywords"]
    if not isinstance(words, list) or not words or any(not isinstance(w, str) or not w.strip() for w in words):
        raise ValueError("daily.weather.warning_keywords must list the district's names, such as [\"경기\", \"성남\"]")


def _check_health(health: object) -> None:
    """Optional `[health]` (health design §9): the Sunday letter's weekday and time, the optional `[health.midweek]`
    check, the topic rotation and the goals the dashboard line tracks."""
    if not isinstance(health, dict):
        raise ValueError("health must be a table")
    _check_letter_slot("health", health, "sunday", "07:00")
    topics = health.get("topics", list(HEALTH_TOPICS))
    if not isinstance(topics, list) or not topics or any(t not in HEALTH_TOPICS for t in topics) or len(set(topics)) != len(topics):
        raise ValueError(f"health.topics must list distinct topics from {', '.join(HEALTH_TOPICS)}")
    goals = health.get("goals", [])
    if not isinstance(goals, list) or len(goals) > 5 or any(g not in HEALTH_GOALS for g in goals):
        raise ValueError(f"health.goals must list up to 5 of {', '.join(HEALTH_GOALS)}")
    midweek = health.get("midweek", {})
    if not isinstance(midweek, dict):
        raise ValueError("health.midweek must be a table")
    unknown = sorted(set(midweek) - HEALTH_MIDWEEK_KEYS)
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join('health.midweek.' + k for k in unknown)}")
    _check_letter_slot("health.midweek", midweek, "wednesday", "18:00")


def _check_letter_slot(where: str, table: dict, default_day: str, default_time: str) -> None:
    if not isinstance(table.get("enabled", False), bool):
        raise ValueError(f"{where}.enabled must be a boolean")
    if table.get("weekday", default_day) not in WEEKDAYS:
        raise ValueError(f"{where}.weekday must be a weekday name")
    clock = table.get("time", default_time)
    try:
        if not isinstance(clock, str) or len(clock) != 5 or time.fromisoformat(clock).tzinfo is not None:
            raise ValueError()
    except ValueError:
        raise ValueError(f"{where}.time must be HH:MM") from None


def _is_email(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}", value) is not None
