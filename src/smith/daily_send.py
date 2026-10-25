"""Sending the daily-routine mail (FR-31 §7): one run per local day, claimed atomically, `sending` committed before
the Gmail request, never resent after an uncertain outcome, retried a bounded number of times after a clean failure.

The shape follows the event notes (ledger.claim_daily_run / advance_daily_run): the claim time is the token every
later transition must match, so a run whose lease expired cannot write or send. A missed day is not sent afterwards
(yesterday's agenda is no use this morning); its reminders carry over once as "지난 알림" through `daily_reminders`.
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

from smith import daily, daily_art, daily_html, gmail, ledger, people
from smith.config import local_time

LEASE = timedelta(minutes=30)
MAX_ATTEMPTS = 3
LATE_AFTER = timedelta(minutes=30)
CARRY_OVER_DAYS = 3      # A reminder from a day whose mail never went out rides along for this long.
Sender = Callable[..., str]
Collector = Callable[..., daily.Inputs]
Modeler = Callable[[Path, dict[str, Any], dict[str, Any], datetime, date], None]   # Adds news and prose in place.


def slot_for(config: dict[str, Any], day: date) -> datetime:
    """The day's send time in the household zone (`[daily] time`, default 06:00)."""
    tz = ZoneInfo(config["app"]["timezone"])
    hour, minute = (int(p) for p in str(config.get("daily", {}).get("time", "06:00")).split(":"))
    return datetime.combine(day, clock(hour, minute), tz)


def is_mail_day(config: dict[str, Any], day: date) -> bool:
    days = config.get("daily", {}).get("weekdays")
    names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    return days is None or names[day.weekday()] in days


def due(conn: Any, config: dict[str, Any], now: datetime) -> date | None:
    """Today (household date) when its send time has passed, it is a mail day, and no final run exists for it."""
    tz = config["app"]["timezone"]
    today = local_time(now, tz).date()
    if not is_mail_day(config, today) or now < slot_for(config, today):
        return None
    runs = ledger.daily_runs_for_day(conn, today)
    # A manual send that morning counts: the client has the day's mail, so the slot is not sent again.
    if any(r["status"] in ledger.DAILY_FINAL for r in runs):
        return None
    scheduled = next((r for r in runs if r["run_key"] == today.isoformat()), None)
    if scheduled is not None and scheduled["status"] == "failed" and scheduled["attempts"] >= MAX_ATTEMPTS:
        return None
    return today


def run_due(db: Path, config: dict[str, Any], *, now: datetime, recipient: str | None,
            credentials: tuple[str, str, str] | None, sender: Sender = gmail.send, collector: Collector | None = None,
            modeler: Modeler | None = None) -> list[str]:
    """Called by `smith report run-due`: sends today's mail when it is due. Returns log lines."""
    with closing(ledger.connect(db)) as conn:
        day = due(conn, config, now)
    if day is None:
        return []
    result = publish(db, config, now=now, day=day, trigger="scheduled", recipient=recipient, credentials=credentials,
                     sender=sender, collector=collector, modeler=modeler)
    return [f"daily {day.isoformat()}: {result['status']}" + (f" ({result['error_code']})" if result.get("error_code") else "")]


def publish(db: Path, config: dict[str, Any], *, now: datetime, day: date, trigger: str, recipient: str | None,
            credentials: tuple[str, str, str] | None, sender: Sender = gmail.send, collector: Collector | None = None,
            modeler: Modeler | None = None) -> dict[str, Any]:
    """Claim, build, send and record one day's mail. Returns {"status", "run_key", "subject", "error_code"}."""
    key = day.isoformat() if trigger == "scheduled" else f"{day.isoformat()}#manual-{uuid.uuid4().hex[:6]}"
    run_id = uuid.uuid4().hex
    with closing(ledger.connect(db)) as conn:
        token, outcome = ledger.claim_daily_run(conn, run_key=key, day=day, run_id=run_id, trigger=trigger, now=now,
                                                lease=LEASE, max_attempts=MAX_ATTEMPTS)
    if token is None:
        return {"status": "skipped", "run_key": key, "subject": None, "error_code": outcome}
    if recipient is None or credentials is None:
        return _finish(db, key, token, "building", "failed", error_code="no-mail-setup")
    started = time.monotonic()
    try:
        tz = config["app"]["timezone"]
        inputs = (collector or daily.collect)(db, config, now=now, day=day)
        data = daily.assemble(inputs, day=day, tz=tz, now=now,
                              airways_sensitive=bool(config.get("daily", {}).get("airways_sensitive", False)))
        carried = carry_over(db, inputs.book, day)
        data["carried_reminders"] = carried
        if modeler is not None:
            modeler(db, config, data, now, day)
        subject, html, text = daily_html.render(data)
        send_at = now + timedelta(seconds=time.monotonic() - started)
        if trigger == "scheduled" and send_at - slot_for(config, day) > LATE_AFTER:
            subject = subject.replace(daily_html.PREFIX, "[Smith][하루 일과 · 지연 발송]", 1)
    except Exception as error:  # noqa: BLE001 - any build failure releases the claim as a clean, retryable failure.
        return _finish(db, key, token, "building", "failed", error_code=f"build-{type(error).__name__}")
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    with closing(ledger.connect(db)) as conn:
        # Committed before the request: a run that dies after Gmail accepted the mail never sends it again.
        held = ledger.advance_daily_run(conn, run_key=key, token=token, from_status="building", status="sending",
                                        subject=subject, html_sha256=digest)
    if not held:
        return {"status": "skipped", "run_key": key, "subject": subject, "error_code": "claim-lost"}
    shown = [r.occurrence.key + f":D-{r.days_left}" for r in data["reminders"]] + [c["key"] for c in carried]
    images = daily_art.payload(daily_art.images_for(data))
    try:
        message_id = sender(credentials, recipient=recipient, subject=subject, html=html, text=text, images=images)
    except gmail.MailError as error:
        status = "unknown" if error.uncertain else "failed"
        if status == "unknown":
            _mark_shown(db, shown, day, run_id)
        return _finish(db, key, token, "sending", status, error_code=error.code, subject=subject)
    _mark_shown(db, shown, day, run_id)
    return _finish(db, key, token, "sending", "sent", message_id=message_id, subject=subject)


def carry_over(db: Path, book: people.Book | None, day: date) -> list[dict[str, Any]]:
    """Reminders of the last CARRY_OVER_DAYS days that no sent mail carried, for days still ahead ("지난 알림")."""
    if book is None:
        return []
    candidates = []
    for back in range(1, CARRY_OVER_DAYS + 1):
        then = day - timedelta(days=back)
        for item in people.reminders(book, then):
            if item.occurrence.on >= day:
                candidates.append({"key": f"{item.occurrence.key}:D-{item.days_left}", "label": item.occurrence.label,
                                   "date": item.occurrence.on, "missed_on": then, "milestone": item.occurrence.milestone})
    if not candidates:
        return []
    with closing(ledger.connect(db)) as conn:   # Not read-only: the first run of a fresh ledger has no file yet.
        already = ledger.shown_reminder_keys(conn, [c["key"] for c in candidates])
    # Today's own reminder for the same day supersedes a missed one (D-7 missed, D-0 today: say it once).
    today_keys = {r.occurrence.key for r in people.reminders(book, day)}
    return [c for c in candidates if c["key"] not in already and c["key"].rsplit(":D-", 1)[0] not in today_keys]


def _mark_shown(db: Path, keys: list[str], day: date, run_id: str) -> None:
    with closing(ledger.connect(db)) as conn:
        ledger.record_reminders_shown(conn, keys, shown_on=day, run_id=run_id)


def _finish(db: Path, key: str, token: str, from_status: str, status: str, *, error_code: str | None = None,
            subject: str | None = None, message_id: str | None = None) -> dict[str, Any]:
    with closing(ledger.connect(db)) as conn:
        ledger.advance_daily_run(conn, run_key=key, token=token, from_status=from_status, status=status,
                                 error_code=error_code, subject=subject, message_id=message_id,
                                 finished_at=datetime.now(timezone.utc))
    return {"status": status, "run_key": key, "subject": subject, "error_code": error_code}


def describe(conn: Any, limit: int = 10) -> list[str]:
    """Recent runs for `smith daily status`."""
    lines = []
    for run in ledger.daily_runs(conn, limit=limit):
        lines.append(f"{run['run_key']:<24} {run['status']:<8} attempts {run['attempts']} "
                     f"{run['error_code'] or ''} {run['subject'] or ''}".rstrip())
    return lines or ["(no daily runs yet)"]


def as_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)
