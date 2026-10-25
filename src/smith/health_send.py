"""Sending the weekly health letter (FR-38, health design §9): one run per ISO week, claimed atomically in the shared
run table (`product = "health"`, key "health:<week>"), `sending` committed before the Gmail request, never resent after
an uncertain outcome, retried a bounded number of times after a clean failure. A week whose Sunday slot was missed is
still sent later that week (its content stays valid), marked as late.

The snapshot kept with each sent letter (payload, prose, the actions as shown) is what next week's letter reviews and
what a reply to that letter is answered from.
"""
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import date, datetime, time as clock, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from smith import gmail, health, health_html, health_letter, ledger
from smith.config import WEEKDAYS, local_time

LEASE = timedelta(minutes=30)
MAX_ATTEMPTS = 3
LATE_AFTER = timedelta(minutes=30)
LATE_WINDOW_DAYS = 3     # A missed letter still goes out this many days after its day (then the week is let go).
PRODUCT = health_letter.PRODUCT
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
Sender = Callable[..., str]
Modeler = Callable[[Path, dict[str, Any], dict[str, Any], datetime, date], None]   # Adds prose in place.


def run_key(day: date) -> str:
    return f"{PRODUCT}:{health_letter.period(day).week_key}"


def letter_weekday(config: dict[str, Any]) -> int:
    name = str(config.get("health", {}).get("weekday", "sunday"))
    return WEEKDAY_NAMES.index(name) if name in WEEKDAYS else 6


def slot_for(config: dict[str, Any], day: date) -> datetime:
    """The letter's send time on `day` in the household zone (`[health] time`, default 07:00)."""
    tz = ZoneInfo(config["app"]["timezone"])
    hour, minute = (int(p) for p in str(config.get("health", {}).get("time", "07:00")).split(":"))
    return datetime.combine(day, clock(hour, minute), tz)


def letter_day(config: dict[str, Any], today: date) -> date | None:
    """The most recent letter day on or before `today`, while it is still within LATE_WINDOW_DAYS (None otherwise)."""
    back = (today.weekday() - letter_weekday(config)) % 7
    return today - timedelta(days=back) if back <= LATE_WINDOW_DAYS else None


def due(conn: Any, config: dict[str, Any], now: datetime) -> date | None:
    """This week's letter day when its slot has passed and the week has no final run (a manual send counts)."""
    today = local_time(now, config["app"]["timezone"]).date()
    day = letter_day(config, today)
    if day is None or now < slot_for(config, day):
        return None
    run = ledger.daily_run(conn, run_key(day))
    if run is None:
        return day
    if run["status"] in ledger.DAILY_FINAL or (run["status"] == "failed" and run["attempts"] >= MAX_ATTEMPTS):
        return None
    return day


def run_due(db: Path, config: dict[str, Any], *, now: datetime, recipient: str | None,
            credentials: tuple[str, str, str] | None, sender: Sender = gmail.send, modeler: Modeler | None = None) -> list[str]:
    """Called by `smith report run-due`: sends this week's letter when it is due. Returns log lines. Recovery of
    interrupted runs and the snapshot retention cover both products, so they hold when only this letter is enabled
    (review 2026-10-25)."""
    lines: list[str] = []
    with closing(ledger.connect(db)) as conn:
        for run in ledger.recover_daily_runs(conn, now=now, lease=LEASE):
            lines.append(f"{run['run_key']} interrupted while {run['was']}: closed")
        ledger.purge_daily_snapshots(conn, before=now - timedelta(days=ledger.SNAPSHOT_KEEP_DAYS))
        day = due(conn, config, now)
    if day is None:
        return lines
    result = publish(db, config, now=now, day=day, trigger="scheduled", recipient=recipient, credentials=credentials,
                     sender=sender, modeler=modeler)
    return lines + [f"health {result['run_key']}: {result['status']}" + (f" ({result['error_code']})" if result.get("error_code") else "")]


def publish(db: Path, config: dict[str, Any], *, now: datetime, day: date, trigger: str, recipient: str | None,
            credentials: tuple[str, str, str] | None, sender: Sender = gmail.send, modeler: Modeler | None = None,
            force: bool = False, topic: str | None = None) -> dict[str, Any]:
    """Claim, build, send and record one week's letter. Returns {"status", "run_key", "subject", "error_code"}."""
    key = run_key(day) if not force else f"{run_key(day)}#manual-{uuid.uuid4().hex[:6]}"
    run_id = uuid.uuid4().hex
    with closing(ledger.connect(db)) as conn:
        token, outcome = ledger.claim_daily_run(conn, run_key=key, day=day, run_id=run_id, trigger=trigger, now=now,
                                                lease=LEASE, max_attempts=MAX_ATTEMPTS, product=PRODUCT)
    if token is None:
        return {"status": "skipped", "run_key": key, "subject": None, "error_code": outcome}
    if recipient is None or credentials is None:
        return _finish(db, key, token, "building", "failed", error_code="no-mail-setup")
    started = time.monotonic()
    try:
        from smith import fitbit

        today = local_time(now, config["app"]["timezone"]).date()
        folder = health.path(db)
        ledger_data = health.load(folder)
        previous = previous_snapshot(db, day)
        # The letter reviews the seven full days before its own day (a Sunday letter: Sunday to Saturday), from the stored days.
        activity = fitbit.weekly_summary(folder, day - timedelta(days=7), day - timedelta(days=1))
        data = health_letter.assemble(ledger_data, config, day=day, today=today, topic=topic, previous=previous, fitbit=activity)
        if modeler is not None:
            modeler(db, config, data, now, day)
        subject, html, text = health_html.render(data)
        send_at = now + timedelta(seconds=time.monotonic() - started)
        if trigger == "scheduled" and send_at - slot_for(config, day) > LATE_AFTER:
            subject = subject.replace(health_html.PREFIX, "[Smith][건강 · 지연 발송]", 1)
    except Exception as error:  # noqa: BLE001 - any build failure releases the claim as a clean, retryable failure.
        return _finish(db, key, token, "building", "failed", error_code=f"build-{type(error).__name__}")
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    with closing(ledger.connect(db)) as conn:
        # Committed before the request: a run that dies after Gmail accepted the mail never sends it again.
        held = ledger.advance_daily_run(conn, run_key=key, token=token, from_status="building", status="sending",
                                        subject=subject, html_sha256=digest)
        if held:
            ledger.record_daily_snapshot(conn, run_key=key, day=day, created_at=now, snapshot=snapshot(data, subject))
    if not held:
        return {"status": "skipped", "run_key": key, "subject": subject, "error_code": "claim-lost"}
    try:
        # The charts and the action pictures travel as inline attachments the HTML references by cid (mail clients
        # drop SVG and data URIs).
        message_id = sender(credentials, recipient=recipient, subject=subject, html=html, text=text, images=health_html.attachments(data))
    except gmail.MailError as error:
        return _finish(db, key, token, "sending", "unknown" if error.uncertain else "failed", error_code=error.code, subject=subject)
    return _finish(db, key, token, "sending", "sent", message_id=message_id, subject=subject)


def snapshot(data: dict[str, Any], subject: str) -> dict[str, Any]:
    """The letter as sent: the model-facing payload, the prose, the actions as shown (prose or fallback) and the
    topic, so next week reviews these actions and a reply is answered from this letter. No HTML, no page refs."""
    prose = data.get("prose") or {}
    return {"version": 1, "product": PRODUCT, "week_key": data["week_key"], "day": data["day"].isoformat(), "subject": subject,
            "topic": data["topic"]["key"], "payload": health_letter.payload(data), "prose": prose or None,
            "actions": prose.get("actions") or data["fallback_actions"], "actions_from": "writer" if prose.get("actions") else "guideline"}


def previous_snapshot(db: Path, day: date) -> dict[str, Any] | None:
    """The last sent letter before `day`'s week: its actions are this week's review."""
    with closing(ledger.connect(db)) as conn:
        runs = ledger.sent_daily_runs(conn, since=datetime.now(timezone.utc) - timedelta(days=120), product=PRODUCT)
        this_week = health_letter.period(day).week_key
        for run in runs:
            if health_letter.period(run["day"]).week_key < this_week or run["day"] < day - timedelta(days=day.weekday()):
                found = ledger.daily_snapshot(conn, run["run_key"])
                if found:
                    return found
    return None


def _finish(db: Path, key: str, token: str, from_status: str, status: str, *, error_code: str | None = None,
            subject: str | None = None, message_id: str | None = None) -> dict[str, Any]:
    with closing(ledger.connect(db)) as conn:
        ledger.advance_daily_run(conn, run_key=key, token=token, from_status=from_status, status=status,
                                 error_code=error_code, subject=subject, message_id=message_id,
                                 finished_at=datetime.now(timezone.utc))
    return {"status": status, "run_key": key, "subject": subject, "error_code": error_code}


def describe(conn: Any, limit: int = 10) -> list[str]:
    """Recent runs for `smith health status`."""
    lines = []
    for run in ledger.daily_runs(conn, limit=limit, product=PRODUCT):
        lines.append(f"{run['run_key']:<24} {run['status']:<8} attempts {run['attempts']} "
                     f"{run['error_code'] or ''} {run['subject'] or ''}".rstrip())
    return lines or ["(no health letters yet)"]


def as_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)
