"""Validate bootstrap settings; this module has no external side effects."""
import re
from datetime import datetime, time
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    "reports": {"enabled", "weekdays", "time", "delivery"},
    "mail": {"recipient", "answer_replies"},
    "household": {"birth_year", "partner_birth_year", "retirement_monthly_spend", "marriage_registered", "cohabiting"},
    "backup": {"dir", "keep"},
}
PROPERTY_KEYS = {"lawd_cd", "dong", "apt_name", "exclusive_area_m2", "acquired_year", "acquired_price"}


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
    mail = config.get("mail", {})
    if not isinstance(mail, dict) or ("recipient" in mail and not _is_email(mail["recipient"])):
        raise ValueError("mail.recipient must be one email address")
    if not isinstance(mail.get("answer_replies", False), bool):
        raise ValueError("mail.answer_replies must be a boolean")
    _check_household(config.get("household", {}))
    _check_backup(config.get("backup", {}))
    if "properties" in config:
        from smith.realestate import RealEstateError, validate_properties
        try:
            validate_properties(config["properties"])
        except RealEstateError as error:
            raise ValueError(f"Invalid property setting ({error.code})") from None
    return config


def _check_keys(config: dict) -> None:
    if config.get("config_version", CONFIG_VERSION) != CONFIG_VERSION:
        raise ValueError(f"config_version must be {CONFIG_VERSION}")
    unknown = sorted(set(config) - set(KNOWN_KEYS) - {"properties", "config_version"})
    for section, keys in KNOWN_KEYS.items():
        if isinstance(config.get(section), dict):
            unknown += [f"{section}.{key}" for key in sorted(set(config[section]) - keys)]
    properties = config.get("properties")
    if isinstance(properties, dict):
        unknown += [f"properties.{ref}.{key}" for ref, spec in sorted(properties.items()) if isinstance(spec, dict)
                    for key in sorted(set(spec) - PROPERTY_KEYS)]
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
    spend = household.get("retirement_monthly_spend")
    if spend is not None and (not isinstance(spend, int) or isinstance(spend, bool) or spend <= 0):
        raise ValueError("household.retirement_monthly_spend must be a positive whole number of won")
    for field in ("marriage_registered", "cohabiting"):
        if not isinstance(household.get(field, False), bool):
            raise ValueError(f"household.{field} must be a boolean")


def _check_backup(backup: object) -> None:
    """Optional `[backup]`: where `run-due` copies the ledger after each sent report, and how many to keep."""
    if not isinstance(backup, dict):
        raise ValueError("backup must be a table")
    if "dir" in backup and (not isinstance(backup["dir"], str) or not backup["dir"].strip()):
        raise ValueError("backup.dir must be a folder path")
    keep = backup.get("keep", 1)
    if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= 1000:
        raise ValueError("backup.keep must be a whole number from 1 to 1000")


def _is_email(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}", value) is not None
