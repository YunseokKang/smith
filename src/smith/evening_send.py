"""Sending the evening "내일 준비" mail (daily design §16.5): one run per tomorrow, claimed atomically in the shared run
table (`product = "evening"`, key "<tomorrow>#evening"), `sending` committed before the Gmail request, never resent
after an uncertain outcome, retried a bounded number of times after a clean failure.

The key starts with the day the mail is about, so a reply to it is answered with that day's agenda even without its
snapshot (daily_qa reads the date from the key). A missed evening is not sent the next morning: after midnight the
household's today has moved on and that day's own morning mail covers it.
"""
import hashlib
import time
import uuid
from contextlib import closing
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from smith import daily_art, daily_send, evening, evening_html, gmail, ledger, talk
from smith.config import local_time

LEASE = daily_send.LEASE
MAX_ATTEMPTS = daily_send.MAX_ATTEMPTS
LATE_AFTER = daily_send.LATE_AFTER
PRODUCT = evening.PRODUCT
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
Collector = daily_send.Collector
Modeler = daily_send.Modeler


def run_key(day: date) -> str:
    return f"{day.isoformat()}#{PRODUCT}"


def settings(config: dict[str, Any]) -> dict[str, Any]:
    """`[evening]` with its defaults: 21:00 every day, the partner called 아내, the full talk rotation."""
    section = config.get("evening", {})
    kinds = tuple(k for k in talk.ROTATION if k in section.get("talk_kinds", talk.ROTATION)) or talk.ROTATION

    def spans(key: str, default: tuple[tuple[str, int, int], ...]) -> tuple[tuple[str, int, int], ...]:
        given = section.get(key)
        return default if not given else tuple((label, int(a), int(b)) for (label, _, _), (a, b) in zip(default, given))
    return {"time": str(section.get("time", "21:00")), "weekdays": section.get("weekdays"),
            "partner": str(section.get("partner", "아내")), "kinds": kinds, "netflix": bool(section.get("netflix", True)),
            "sleep": bool(section.get("sleep", True)),
            "commute_hours": spans("commute_hours", evening.COMMUTE_HOURS), "rest_hours": spans("rest_hours", evening.REST_HOURS)}


def slot_for(config: dict[str, Any], evening_of: date) -> datetime:
    """The send time on the evening of `evening_of` in the household zone."""
    hour, minute = (int(p) for p in settings(config)["time"].split(":"))
    return datetime.combine(evening_of, clock(hour, minute), ZoneInfo(config["app"]["timezone"]))


def is_mail_evening(config: dict[str, Any], evening_of: date) -> bool:
    days = settings(config)["weekdays"]
    return days is None or WEEKDAY_NAMES[evening_of.weekday()] in days


def due(conn: Any, config: dict[str, Any], now: datetime) -> date | None:
    """Tomorrow (the mail's day) once tonight's slot has passed, when tonight is a mail evening and no final run exists
    for tomorrow (a manual send counts)."""
    tonight = local_time(now, config["app"]["timezone"]).date()
    if not is_mail_evening(config, tonight) or now < slot_for(config, tonight):
        return None
    day = tonight + timedelta(days=1)
    runs = ledger.daily_runs_for_day(conn, day, product=PRODUCT)
    if any(r["status"] in ledger.DAILY_FINAL for r in runs):
        return None
    scheduled = next((r for r in runs if r["run_key"] == run_key(day)), None)
    if scheduled is not None and scheduled["status"] == "failed" and scheduled["attempts"] >= MAX_ATTEMPTS:
        return None
    return day


def run_due(db: Path, config: dict[str, Any], *, now: datetime, recipient: str | None,
            credentials: tuple[str, str, str] | None, sender: daily_send.Sender = gmail.send,
            collector: Collector | None = None, modeler: Modeler | None = None) -> list[str]:
    """Called by `smith report run-due`: sends tonight's mail when it is due. Returns log lines. Interrupted runs of
    every product are closed first, so recovery holds when only this mail is enabled."""
    lines: list[str] = []
    with closing(ledger.connect(db)) as conn:
        for run in ledger.recover_daily_runs(conn, now=now, lease=LEASE):
            lines.append(f"{run['run_key']} interrupted while {run['was']}: closed")
        ledger.purge_daily_snapshots(conn, before=now - timedelta(days=ledger.SNAPSHOT_KEEP_DAYS))
        day = due(conn, config, now)
    if day is None:
        return lines
    result = publish(db, config, now=now, day=day, trigger="scheduled", recipient=recipient, credentials=credentials,
                     sender=sender, collector=collector, modeler=modeler)
    return lines + [f"evening {result['run_key']}: {result['status']}" + (f" ({result['error_code']})" if result.get("error_code") else "")]


def build(db: Path, config: dict[str, Any], *, now: datetime, day: date, collector: Collector | None = None,
          modeler: Modeler | None = None) -> dict[str, Any]:
    """The data dict for the mail about `day` (tomorrow), with the model steps when `modeler` is given."""
    inputs = (collector or evening.collect)(db, config, now=now, day=day)
    data = evening.assemble(inputs, day=day, tz=config["app"]["timezone"], now=now,
                            airways_sensitive=bool(config.get("daily", {}).get("airways_sensitive", False)),
                            kinds=settings(config)["kinds"], commute_hours=settings(config)["commute_hours"],
                            rest_hours=settings(config)["rest_hours"])
    data["talk_recent"] = talk.recent(db, now=now, product=PRODUCT)
    if settings(config)["sleep"]:
        from smith import health

        evening.add_sleep(data, health_folder=health.path(db), now=now)
    if modeler is not None:
        modeler(db, config, data, now, day)
    return data


def publish(db: Path, config: dict[str, Any], *, now: datetime, day: date, trigger: str, recipient: str | None,
            credentials: tuple[str, str, str] | None, sender: daily_send.Sender = gmail.send,
            collector: Collector | None = None, modeler: Modeler | None = None, force: bool = False) -> dict[str, Any]:
    """Claim, build, send and record the mail about `day`. Returns {"status", "run_key", "subject", "error_code"}.
    The day is the unit of claiming for scheduled and manual sends alike; only `force` sends a second mail."""
    key = run_key(day) if not force else f"{run_key(day)}-manual-{uuid.uuid4().hex[:6]}"
    run_id = uuid.uuid4().hex
    with closing(ledger.connect(db)) as conn:
        token, outcome = ledger.claim_daily_run(conn, run_key=key, day=day, run_id=run_id, trigger=trigger, now=now,
                                                lease=LEASE, max_attempts=MAX_ATTEMPTS, product=PRODUCT)
    if token is None:
        return {"status": "skipped", "run_key": key, "subject": None, "error_code": outcome}
    if recipient is None or credentials is None:
        return daily_send._finish(db, key, token, "building", "failed", error_code="no-mail-setup")
    started = time.monotonic()
    partner = settings(config)["partner"]
    try:
        data = build(db, config, now=now, day=day, collector=collector, modeler=modeler)
        subject, html, text = evening_html.render(data, partner=partner)
        send_at = now + timedelta(seconds=time.monotonic() - started)
        if trigger == "scheduled" and local_time(send_at, config["app"]["timezone"]).date() >= day:
            # Built past midnight (review 2026-10-27): tomorrow has begun and its morning mail covers it.
            return daily_send._finish(db, key, token, "building", "skipped", error_code="past-midnight")
        if trigger == "scheduled" and send_at - slot_for(config, day - timedelta(days=1)) > LATE_AFTER:
            subject = subject.replace(evening_html.PREFIX, "[Smith][내일 준비 · 지연 발송]", 1)
        item = evening_html.talk_item(data)
    except Exception as error:  # noqa: BLE001 - any build failure releases the claim as a clean, retryable failure.
        return daily_send._finish(db, key, token, "building", "failed", error_code=f"build-{type(error).__name__}")
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    with closing(ledger.connect(db)) as conn:
        # Committed before the request: a run that dies after Gmail accepted the mail never sends it again.
        held = ledger.advance_daily_run(conn, run_key=key, token=token, from_status="building", status="sending",
                                        subject=subject, html_sha256=digest)
        if held:
            ledger.record_daily_snapshot(conn, run_key=key, day=day, created_at=now, snapshot=snapshot(data, subject, item))
    if not held:
        return {"status": "skipped", "run_key": key, "subject": subject, "error_code": "claim-lost"}
    shown = [r.occurrence.key + f":D-{r.days_left}" for r in data["reminders"]]
    images = daily_art.payload(evening_html.images_for(data))
    try:
        message_id = sender(credentials, recipient=recipient, subject=subject, html=html, text=text, images=images)
    except gmail.MailError as error:
        status = "unknown" if error.uncertain else "failed"
        if status == "unknown":
            daily_send._mark_shown(db, shown, day, run_id)
        return daily_send._finish(db, key, token, "sending", status, error_code=error.code, subject=subject)
    daily_send._mark_shown(db, shown, day, run_id)
    return daily_send._finish(db, key, token, "sending", "sent", message_id=message_id, subject=subject)


def snapshot(data: dict[str, Any], subject: str, item: dict[str, str]) -> dict[str, Any]:
    """The mail as sent, in the morning snapshot's shape (a reply is answered by the daily thread's answerer) plus
    tonight's checklist and the talk item (later evenings read it so they do not repeat themselves)."""
    stored = daily_send.snapshot(data, subject, [])
    shown = {k: item.get(k, "") for k in ("kind", "label", "title", "line", "note", "url")}
    # Tonight's Netflix picks, in the order shown: what later evenings do not offer again.
    picks = [{"order": n, **{k: p.get(k, "") for k in ("title", "original_title", "year", "kind", "genre", "why", "netflix_url")}}
             for n, p in enumerate(data.get("netflix") or [], start=1)]
    # The talk item and the picks as shown go into the prose too: the reply answerer reads the prose (daily_qa
    # .context_from_snapshot), so "두 번째 거 어떤 내용이야?" and a night the code's own line stood in are answerable.
    stored.update({"product": PRODUCT, "evening_of": data["evening_of"].isoformat(), "prep": [p["text"] for p in data["prep"]],
                   "talk": shown, "netflix": picks,
                   "prose": {**(stored.get("prose") or {}), "talk": shown, "netflix": picks, "prep": [p["text"] for p in data["prep"]]},
                   # The sleep corner is kept beside the prose, never in it: the reply answerer passes the prose to a model,
                   # and the sleep figures go to no model (review 2026-10-27, P0).
                   "sleep_shown": data.get("sleep") is not None})
    return stored


def describe(conn: Any, limit: int = 10) -> list[str]:
    """Recent runs for `smith evening status`."""
    lines = [f"{run['run_key']:<34} {run['status']:<8} attempts {run['attempts']} {run['error_code'] or ''} "
             f"{run['subject'] or ''}".rstrip() for run in ledger.daily_runs(conn, limit=limit, product=PRODUCT)]
    return lines or ["(no evening runs yet)"]
