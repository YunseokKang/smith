"""Scheduled and manual report publication with slot-level de-duplication.

A slot (e.g. Monday 06:00 Asia/Seoul) is claimed in the ledger before anything is sent. A slot that
was sent, is in an unknown state or is in progress is never sent again automatically; a clean failure
is retried up to ledger.MAX_SEND_ATTEMPTS. If the PC was off, the latest due slot is sent late in one
message that lists the missed slots, which are settled only once that message is (possibly) delivered;
nothing is backfilled on the very first run.
"""
import hashlib
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from smith import gmail, ledger
from smith.config import DEFAULT_TIMEZONE, local_time
from smith.proposals import snapshot
from smith.report_data import build_report
from smith.report_html import render

_WEEKDAY_INDEX = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
LATE_AFTER = timedelta(minutes=30)
Sender = Callable[..., str]
Narrator = Callable[[dict[str, Any]], None]


def slots_between(reports: dict[str, Any], tz_name: str, start: datetime, end: datetime) -> list[datetime]:
    """Scheduled slots in (start, end], oldest first, as aware datetimes in the configured timezone."""
    tz = ZoneInfo(tz_name)
    hour, minute = (int(part) for part in reports["time"].split(":"))
    days = {_WEEKDAY_INDEX[name] for name in reports["weekdays"]}
    local_start, local_end = start.astimezone(tz), end.astimezone(tz)
    slots, day = [], local_start.date() - timedelta(days=1)
    while day <= local_end.date():
        if day.weekday() in days:
            slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            if local_start < slot <= local_end:
                slots.append(slot)
        day += timedelta(days=1)
    return slots


def due(conn: Any, config: dict[str, Any], now: datetime) -> tuple[datetime, list[datetime]] | None:
    """The latest slot at or before `now` that has no row yet (or only a retryable failure), plus the
    earlier slots since the last sent report that were missed."""
    reports, tz = config["reports"], config["app"]["timezone"]
    recent = slots_between(reports, tz, now - timedelta(days=8), now)
    if not recent:
        return None
    latest = recent[-1]
    rows = {r["slot"]: r for r in ledger.report_runs(conn, limit=1000) if r["slot"]}
    row = rows.get(ledger._db_time(latest))
    if row is not None and (row["status"] != "failed" or row["attempts"] >= ledger.MAX_SEND_ATTEMPTS):
        return None
    # Missed slots are counted from the last scheduled slot that has any record (sent, merged or
    # the activation marker), so history before scheduling was enabled is never backfilled. An earlier
    # slot that failed (with attempts left or not) was not delivered either, so it counts as missed.
    recorded = [datetime.fromisoformat(s) for s, r in rows.items() if r["status"] in ("sent", "merged")]
    if not recorded:
        return latest, []
    since = max(recorded)
    missed = [s for s in slots_between(reports, tz, since, latest - timedelta(seconds=1))
              if ledger._db_time(s) not in rows or rows[ledger._db_time(s)]["status"] == "failed"]
    return latest, missed


def activate(conn: Any, config: dict[str, Any], now: datetime) -> datetime | None:
    """Start scheduled delivery at `now`: the latest slot that already passed is marked so it is not
    sent late. Slots after activation that are missed (PC off) are still sent late."""
    recent = slots_between(config["reports"], config["app"]["timezone"], now - timedelta(days=8), now)
    if not recent:
        return None
    slot = recent[-1]
    ledger.mark_slot(conn, slot=slot, kind="thursday" if slot.weekday() == 3 else "monday", now=now,
                     reason="before-activation")
    return slot


def recover(conn: Any, now: datetime) -> list[dict[str, Any]]:
    """Close runs interrupted by a crash or power loss before deciding what is due (see ledger)."""
    return ledger.recover_stale_reports(conn, now=now)


def record_failure(db: Path, *, now: datetime, slot: datetime | None, missed: list[datetime], kind: str,
                   code: str) -> dict[str, Any]:
    """Persist a failure that happened before a report could be built (missing setup), so it shows in
    `report status` even when the scheduler runs without a console. It counts as one attempt."""
    report_id = uuid.uuid4().hex
    with closing(ledger.connect(db)) as conn:
        if not ledger.claim_report(conn, report_id=report_id, slot=slot, kind=kind,
                                   trigger="manual" if slot is None else "scheduled", created_at=now, as_of=now,
                                   baseline=None, missed_slots=missed):
            return {"report_id": None, "status": "skipped"}
    return _finish(db, report_id, "failed", error_code=code)


def publish(db: Path, *, recipient: str, credentials: tuple[str, str, str], now: datetime, slot: datetime | None,
            missed: list[datetime], kind: str, sender: Sender = gmail.send, tz: str = DEFAULT_TIMEZONE,
            sync_failures: list[str] | None = None, narrator: Narrator | None = None,
            household: dict[str, Any] | None = None) -> dict[str, Any]:
    """Claim, build, send and record one report. Returns the final run record fields.

    `narrator(data)` adds the verified narrative (6c/6d) to the report data in place; it handles its own
    failures, so a report always goes out with at least its deterministic content.

    The run is `building` until the message is rendered and `sending` from just before the Gmail
    request, so an interrupted run can be told apart: the first is retried, the second never is.
    """
    report_id = uuid.uuid4().hex
    with closing(ledger.connect(db)) as conn:
        last = ledger.last_sent_report(conn)
        baseline = None if last is None else datetime.fromisoformat(last["as_of"])
        unresolved = ledger.unresolved_runs(conn, since=None if last is None else datetime.fromisoformat(last["created_at"]))
        claimed = ledger.claim_report(conn, report_id=report_id, slot=slot, kind=kind,
                                      trigger="manual" if slot is None else "scheduled", created_at=now, as_of=now,
                                      baseline=baseline, missed_slots=missed)
    if not claimed:
        return {"report_id": None, "status": "skipped"}
    started = time.monotonic()
    try:
        with closing(ledger.connect_read_only(db)) as conn:
            data = build_report(conn, as_of=now, known_at=now, baseline=baseline, kind=kind, tz=tz,
                                household=household)
        if narrator is not None:
            narrator(data)
        # Lateness is judged at send time: the narrative stage can take many minutes after `now`.
        send_at = now + timedelta(seconds=time.monotonic() - started)
        data["delivery_note"] = _delivery_note(None if slot is None else local_time(slot, tz),
                                               [local_time(m, tz) for m in missed], local_time(send_at, tz),
                                               [r for r in unresolved if r["report_id"] != report_id], tz,
                                               sync_failures or [])
        subject, html = render(data)
        if slot is not None and send_at - slot > LATE_AFTER:
            subject = subject.replace("[Smith]", "[Smith · 지연 발송]", 1)
    except Exception as error:  # noqa: BLE001 - any build failure must release the claim as a clean failure.
        return _finish(db, report_id, "failed", error_code=f"build-{type(error).__name__}")
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    with closing(ledger.connect(db)) as conn:
        holds_claim = ledger.start_sending(conn, report_id=report_id, now=datetime.now(timezone.utc),
                                           subject=subject, html_sha256=digest)
    if not holds_claim:
        # Fencing: this run outlived its lease and was recovered or superseded; another run owns the slot.
        return {"report_id": report_id, "status": "skipped", "subject": subject, "error_code": "claim-lost"}
    # The proposals the message carried are recorded in the same transaction as its final status.
    shown = [snapshot(p) for p in data["advice"]["proposals"]]
    try:
        message_id = sender(credentials, recipient=recipient, subject=subject, html=html, text=_text(subject))
    except gmail.MailError as error:
        return _finish(db, report_id, "unknown" if error.uncertain else "failed", subject=subject, digest=digest,
                       error_code=error.code, shown=shown)
    return _finish(db, report_id, "sent", subject=subject, digest=digest, message_id=message_id, shown=shown)


def _finish(db: Path, report_id: str, status: str, *, subject: str | None = None, digest: str | None = None,
            message_id: str | None = None, error_code: str | None = None,
            shown: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    with closing(ledger.connect(db)) as conn:
        ledger.finish_report(conn, report_id=report_id, status=status, finished_at=datetime.now(timezone.utc),
                             subject=subject, html_sha256=digest, message_id=message_id, error_code=error_code,
                             shown_proposals=shown)
    return {"report_id": report_id, "status": status, "subject": subject, "error_code": error_code}


def _delivery_note(slot: datetime | None, missed: list[datetime], now: datetime, unresolved: list[dict[str, Any]],
                   tz: str, sync_failures: list[str]) -> str | None:
    """Times are local. Covers late delivery, missed slots, earlier runs that failed or may not have
    arrived, and data sources that could not be refreshed before this report."""
    parts = []
    if slot is not None and now - slot > LATE_AFTER:
        parts.append(f"이 보고서는 정기 발행 시각({slot:%m월 %d일 %H:%M})보다 늦게 발송되었습니다"
                     "(발행 시각에 PC가 꺼져 있었거나 실행되지 못했습니다).")
    if missed:
        listed = ", ".join(f"{m:%m월 %d일 %H:%M}" for m in missed)
        parts.append(f"그 사이 보내드리지 못한 정기 보고({listed})는 이 보고서에 합쳐 보고드립니다.")
    for run in unresolved:
        when = local_time(datetime.fromisoformat(run["slot"] or run["created_at"]), tz)
        parts.append(f"{when:%m월 %d일 %H:%M} 보고는 발송 결과를 확인하지 못했습니다. 받은편지함에 없다면 "
                     "이 보고서가 그 내용을 대신합니다.")
    if sync_failures:
        parts.append(f"보고서 작성 전 일부 데이터({', '.join(sync_failures)})를 새로 받지 못해 이전 값을 사용했습니다.")
    return " ".join(parts) or None


def _text(subject: str) -> str:
    return f"{subject}\n\n이 보고서는 HTML 형식입니다. HTML을 지원하는 메일 앱에서 확인하시기 바랍니다."
