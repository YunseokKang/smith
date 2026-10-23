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
                  "annual_gross_salary", "retirement_age"},
    "backup": {"dir", "keep"},
    # Same-day event notes (report design §29): the daily check time and the holdings' move that counts.
    "events": {"enabled", "check_time", "portfolio_move_pct"},
}
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
    for field in ("marriage_registered", "cohabiting"):
        if not isinstance(household.get(field, False), bool):
            raise ValueError(f"household.{field} must be a boolean")
    age = household.get("retirement_age")
    if age is not None and (not isinstance(age, int) or isinstance(age, bool) or not 40 <= age <= 90):
        raise ValueError("household.retirement_age must be an age from 40 to 90")


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


def _is_email(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}", value) is not None
