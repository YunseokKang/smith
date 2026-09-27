"""Validate bootstrap settings; this module has no external side effects."""
from datetime import time
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def load_config(path: Path) -> dict:
    with path.open("rb") as stream:
        config = tomllib.load(stream)
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
    return config
