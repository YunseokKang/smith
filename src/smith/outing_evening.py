"""The '이번 주말 나들이' card in Thursday's evening 내일 준비 mail (FR-57, daily design §18.1; client 2026-11-18:
"매주 목요일 밤 9시에 보내는 내일 일과 메일에도 넣어 줘").

The same computation as `smith outing search` (outing_command.search). The card is optional: a failure is named in
the mail's notes and never fails the mail, as for the restaurant picks. Picks enter the repeat history only after
Gmail accepted the mail (`after_send`), so a preview or a failed send does not use them up.
"""
from datetime import date, datetime
from pathlib import Path
from typing import Any

from smith import outing, outing_command
from smith.config import local_time

FRIDAY = 4   # The mail's day is tomorrow; Thursday evening's mail is about Friday.


def wanted(config: dict[str, Any], data: dict[str, Any]) -> bool:
    return bool(config.get("outing", {}).get("evening", True)) and data["day"].weekday() == FRIDAY


def step(db: Path, config: dict[str, Any], data: dict[str, Any], now: datetime) -> None:
    """Fill data["outing"] with the dict form on Thursday evenings (or {"error": code})."""
    if not wanted(config, data):
        return
    try:
        # The mail passes UTC; the weekend, the forecast runs and the history are household-local.
        result = outing_command.search(db, config, local_time(now, config["app"]["timezone"]))
    except outing.OutingUnavailable as error:
        data["outing"] = {"error": error.code[:40]}
    except Exception as error:  # noqa: BLE001 - the card is optional; the mail goes out without it.
        data["outing"] = {"error": f"outing-{type(error).__name__}"}
    else:
        data["outing"] = outing.to_dict(result)
        return
    data["failures"]["outing"] = data["outing"]["error"]


def after_send(db: Path, data: dict[str, Any], today: date) -> None:
    """Record the card's picks as sent on the household-local `today` (REPEAT_DAYS). Never raises: the mail is out."""
    picks = (data.get("outing") or {}).get("picks") or []
    if not picks:
        return
    try:
        outing.remember(outing.history_path(db), [{"key": f"{p['source']}:{p['id']}", "title": p["title"]} for p in picks],
                        today)
    except (OSError, outing.OutingUnavailable):
        pass


def for_prose(found: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What the reply answerer may read about the card: public event facts only."""
    return [{"order": n, **{k: p.get(k) for k in ("title", "place", "district", "start", "end", "fee", "link", "day")}}
            for n, p in enumerate((found or {}).get("picks") or [], 1)]
