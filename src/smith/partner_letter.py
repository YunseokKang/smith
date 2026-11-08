"""The spouse's own financial letter (FR-51, report design §42; user decisions 2026-11-08).

Each member gets a letter of their own: the reader's accounts ("나의 …") and the household's ("우리 집 …"), both
calling the reader 고객님. The client's letter is the regular report (delivery.publish); this module sends the
spouse's: the same edition built from the spouse's side (report_data.build_report audience "partner"), to
`[mail] partner_recipient`, only for an edition already split (editions.personalized). No model is called for it:
the reader's sections are computed by code (user decision 2026-11-08: the household narrative is written once).

It keeps its own run state in daily_runs (product PRODUCT, one run per edition and local day), so the client's
report runs, their baselines, the reply answering and the client's proposal history never see it. The proposals it
carried are recorded under the spouse's scope (smith.perspective). Replies to it are not answered.
"""
import hashlib
import json
import logging
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smith import delivery, editions, gmail, ledger, perspective, proposals
from smith.config import local_time, partner_recipient
from smith.report_data import build_report
from smith.report_html import render

logger = logging.getLogger(__name__)

PRODUCT = "report-partner"
AUDIENCE = "partner"
LEASE = timedelta(minutes=30)
MAX_ATTEMPTS = 3


def run_key(kind: str, day: date) -> str:
    return f"{PRODUCT}:{kind}:{day.isoformat()}"


def publish(db: Path, config: dict[str, Any], *, now: datetime, kind: str, trigger: str,
            credentials: tuple[str, str, str] | None, household: dict[str, Any] | None,
            day: date | None = None, sender: delivery.Sender = gmail.send,
            household_story: str | None = None, opinions: dict[str, Any] | None = None,
            snapshot_at: datetime | None = None) -> dict[str, Any]:
    """Claim, build, send and record the spouse's letter of one edition. A scheduled letter is one per edition and
    local `day`; a manual one always gets a fresh key. `household_story` is the household paragraph the client's
    report narrated (both letters carry it); `opinions` are that narrative's holding opinions by position key (per
    member, `holdings.per_owner`); `snapshot_at` is the ledger time the client's report was built and verified on,
    and the spouse's letter is built on the same snapshot (Codex review 2026-11-08: a ledger update in between let a
    verified opinion meet other figures). The paragraph and that time are kept with the run, so a retry rebuilds the
    same snapshot; a retry goes without the opinions. Returns {"status", "run_key", "subject", "error_code"}."""
    recipient = partner_recipient(config)
    if recipient is None:
        return {"status": "skipped", "run_key": None, "subject": None, "error_code": "no-partner-recipient"}
    if not editions.personalized(kind):
        return {"status": "skipped", "run_key": None, "subject": None, "error_code": "not-personalized"}
    tz = config["app"]["timezone"]
    day = day or local_time(now, tz).date()
    key = run_key(kind, day) if trigger == "scheduled" else f"{run_key(kind, day)}#manual-{uuid.uuid4().hex[:6]}"
    run_id = uuid.uuid4().hex
    with closing(ledger.connect(db)) as conn:
        token, outcome = ledger.claim_daily_run(conn, run_key=key, day=day, run_id=run_id, trigger=trigger, now=now,
                                                lease=LEASE, max_attempts=MAX_ATTEMPTS, product=PRODUCT)
    if token is None:
        return {"status": "skipped", "run_key": key, "subject": None, "error_code": outcome}
    if credentials is None:
        return _finish(db, key, token, "building", "failed", error_code="no-mail-setup")
    # The settings the paragraph was written with: a retry under changed settings (a new retirement target, say)
    # leaves the old paragraph out rather than set it beside figures it no longer matches (Codex review 2026-11-08).
    settings = hashlib.sha256(json.dumps(household or {}, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    with closing(ledger.connect(db)) as conn:
        if snapshot_at is not None:
            ledger.record_daily_snapshot(conn, run_key=key, day=day, created_at=now,
                                         snapshot={"product": PRODUCT, "household_story": household_story or "",
                                                   "snapshot_at": snapshot_at.isoformat(), "settings": settings})
        else:
            kept = ledger.daily_snapshot(conn, key) or {}
            if kept.get("settings") == settings:
                household_story = household_story or kept.get("household_story")
            if kept.get("snapshot_at"):
                snapshot_at = datetime.fromisoformat(kept["snapshot_at"])
    at = snapshot_at or now
    try:
        with closing(ledger.connect_read_only(db)) as conn:
            data = build_report(conn, as_of=at, known_at=at, baseline=None, kind=kind, tz=tz, household=household,
                                schedule=config.get("reports"), audience=AUDIENCE)
        # No narrative of its own: only the household paragraph shared with the client's letter (§42).
        data["narrative"] = {"status": {"outcome": "shared"}, "household": household_story or "",
                             "holdings": opinions or {}}
        subject, html = render(data)
    except Exception as error:  # noqa: BLE001 - any build failure releases the claim as a clean, retryable failure.
        return _finish(db, key, token, "building", "failed", error_code=f"build-{type(error).__name__}")
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    with closing(ledger.connect(db)) as conn:
        # Committed before the request: a run that dies after Gmail accepted the mail never sends it again.
        held = ledger.advance_daily_run(conn, run_key=key, token=token, from_status="building", status="sending",
                                        subject=subject, html_sha256=digest)
    if not held:
        return {"status": "skipped", "run_key": key, "subject": subject, "error_code": "claim-lost"}
    shown = perspective.scoped([proposals.snapshot(p) for p in data["advice"]["proposals"]], AUDIENCE)
    try:
        mail_html, images = delivery.inline_images(html)
        message_id = sender(credentials, recipient=recipient, subject=subject, html=mail_html,
                            text=delivery.plain_text(subject), **({"images": images} if images else {}))
    except gmail.MailError as error:
        status = "unknown" if error.uncertain else "failed"
        return _finish(db, key, token, "sending", status, error_code=error.code, subject=subject,
                       run_id=run_id, shown=shown if status == "unknown" else None)
    return _finish(db, key, token, "sending", "sent", message_id=message_id, subject=subject, run_id=run_id, shown=shown)


def _finish(db: Path, key: str, token: str, from_status: str, status: str, *, error_code: str | None = None,
            subject: str | None = None, message_id: str | None = None, run_id: str | None = None,
            shown: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Record the outcome; a delivered (or possibly delivered) letter records the proposals it carried."""
    now = datetime.now(timezone.utc)
    with closing(ledger.connect(db)) as conn:
        if not ledger.advance_daily_run(conn, run_key=key, token=token, from_status=from_status, status=status,
                                        error_code=error_code, subject=subject, message_id=message_id, finished_at=now):
            logger.warning("Partner letter %s finished as %s but its state could not be recorded", key, status)
        if shown and run_id is not None:
            ledger.record_proposals_shown(conn, report_id=run_id, at=now, shown=shown)
    return {"status": status, "run_key": key, "subject": subject, "error_code": error_code}
