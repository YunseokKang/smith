"""Validate bootstrap settings; this module has no external side effects."""
import re
from datetime import datetime, time
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The household's local timezone for commands that run without a config file (preview, advise, summary).
DEFAULT_TIMEZONE = "Asia/Seoul"
WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def local_time(moment: datetime, tz_name: str = DEFAULT_TIMEZONE) -> datetime:
    """The same instant in the household timezone. Calculations take calendar dates (cash-flow start
    and end, goal dates) from `as_of.date()`, so `as_of` must be local: 06:00 KST is the previous day
    in UTC. Storage still normalizes every time to UTC."""
    return moment.astimezone(ZoneInfo(tz_name))


def load_config(path: Path) -> dict:
    # Notepad and PowerShell 5.1 write a UTF-8 BOM, which tomllib rejects; accept it for edited files.
    config = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
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
    if "properties" in config:
        from smith.realestate import RealEstateError, validate_properties
        try:
            validate_properties(config["properties"])
        except RealEstateError as error:
            raise ValueError(f"Invalid property setting ({error.code})") from None
    return config


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


def _is_email(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}", value) is not None
