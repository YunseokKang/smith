"""Answers to the spouse's replies to their own letter (report design §42.11, FR-51).

The spouse's letter (partner_letter) shows the household from the spouse's side. A reply to it is answered from that
same side: the ledger at the letter's snapshot built with audience "partner", and the letter's own narrative. The
research brief, ledger time and lineage are those of the client's report the letter went out with (same edition, same
snapshot). The client memory is never used and nothing the spouse states is kept (`qa.answer(spouse=True)`); which
messages count as the spouse's and where the answer goes is `qa.find` and `qa.poll`'s.
"""
import subprocess
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from smith import editions, headless, ledger, qa, qa_lookup
from smith.config import DEFAULT_TIMEZONE
from smith.report_data import build_report

AUDIENCE = "partner"


def letter(db: Path, run_key: str) -> dict[str, Any] | None:
    """The record `qa.answer` reads for the spouse's letter `run_key` ("report-partner:{kind}:{day}", a manual one with
    "#manual-…"): the client's report of the same edition and ledger time for its brief, baseline and lineage, with the
    spouse's letter's own narrative runs and creation time. Without that report (it should not happen) the letter's
    own snapshot and baseline stand and the brief is none. None when the letter is unknown or kept no snapshot."""
    with closing(ledger.connect_read_only(db)) as conn:
        run = ledger.daily_run(conn, run_key)
        kept = ledger.daily_snapshot(conn, run_key) or {}
        if run is None or not kept.get("snapshot_at"):
            return None
        kind = run_key.split("#", 1)[0].split(":")[1]
        at = datetime.fromisoformat(kept["snapshot_at"])
        found = next((r for r in ledger.report_runs(conn, limit=200)
                      if r["kind"] == kind and r["as_of"] and datetime.fromisoformat(r["as_of"]) == at), None)
        client = ledger.get_report(conn, found["report_id"]) if found else None
    record = dict(client or {"kind": kind, "as_of": kept["snapshot_at"], "known_at": kept["snapshot_at"],
                             "baseline": kept.get("baseline"), "brief_id": None})
    # The spouse's own narrative (audited under narrative.PARTNER_USE_CASE), never the client's.
    record.update(advice_run_ids=list(kept.get("narrative_run_ids") or []), created_at=run["created_at"].isoformat(),
                  report_id=run_key)
    return record


def rebuild(db: Path, record: dict[str, Any], *, tz: str = DEFAULT_TIMEZONE, household: dict[str, Any] | None = None,
            lineage: dict[str, str | None] | None = None) -> dict[str, Any]:
    """The household from the spouse's side at the letter's ledger time. Like `qa.rebuild`, a question may be about any
    area whatever the day's theme was, so the full data is built; `lineage_drift` says what changed since."""
    at = datetime.fromisoformat(record["as_of"])
    known = datetime.fromisoformat(record.get("known_at") or record["as_of"])
    baseline = None if not record.get("baseline") else datetime.fromisoformat(record["baseline"])
    kind = "monday" if editions.of(record["kind"]) is not None else record["kind"]
    with closing(ledger.connect_read_only(db)) as conn:
        data = build_report(conn, as_of=at, known_at=known, baseline=baseline, kind=kind, tz=tz, household=household,
                            audience=AUDIENCE)
    # The full data, but the holdings, their week and the loans in the payload are the spouse's own, as in their
    # letter (narrative.per_person; Codex review 2026-11-10).
    data["per_person"] = True
    data["lineage_drift"] = qa.drift(record, lineage)
    return data


def answerer(db: Path, *, now: datetime, tz: str, secrets: list[str], household: dict[str, Any] | None,
             lineage: dict[str, str | None] | None, executable: Path | None = None,
             runner: headless.Runner = subprocess.run, verifier: qa_lookup.Verifier | None = None,
             dart_key: str | None = None) -> Callable[..., tuple[str, str, dict[str, Any]]]:
    """The callable `qa.poll` uses for a question in a spouse's letter thread: (question, history) -> (html, text,
    result). Raises LookupError for a letter Smith has no record of (a clean, retryable failure in `qa.poll`)."""
    cache: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    def call(question: qa.Question, history: list[dict[str, str | None]]) -> tuple[str, str, dict[str, Any]]:
        key = str(question.report_id or "")
        if key not in cache:
            record = letter(db, key)
            if record is None:
                raise LookupError("partner-letter-unknown")
            cache[key] = (record, rebuild(db, record, tz=tz, household=household, lineage=lineage))
        record, data = cache[key]
        result = qa.answer(db, data, question, now=now, secrets=secrets, executable=executable or headless.find_claude(),
                           runner=runner, report=record, history=history, tz=tz, verifier=verifier, dart_key=dart_key,
                           spouse=True)
        html, text = qa.render(question, result, data)
        return html, text, result
    return call
