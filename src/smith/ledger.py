"""SQLite ledger of append-only record revisions.

The state of a record at time T is chosen from its revisions that no correction has
superseded: the latest `effective_at` not after T wins, and ties go to the higher revision.
Filtering by `recorded_at` reproduces what the ledger knew at an earlier time.
"""
import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import Any

from smith.announcements import Announcement
from smith.records import (
    REFERENCE_FIELDS, Action, ChangeType, ImportBatch, ImportRejected, ImportResult, Kind, Observation,
    RecordInput, Status, StoredRevision,
)

logger = logging.getLogger(__name__)
_END_OF_TIME = datetime.max.replace(tzinfo=timezone.utc)

SCHEMA_VERSION = 14
_V1 = (
    """CREATE TABLE imports (
        import_id TEXT PRIMARY KEY,
        payload_sha256 TEXT NOT NULL,
        source TEXT NOT NULL,
        mode TEXT NOT NULL,
        as_of TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    """CREATE TABLE owners (
        owner_id TEXT PRIMARY KEY,
        first_import_id TEXT NOT NULL REFERENCES imports(import_id)
    )""",
    """CREATE TABLE record_revisions (
        record_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        kind TEXT NOT NULL,
        owner_id TEXT NOT NULL REFERENCES owners(owner_id),
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        change_type TEXT NOT NULL,
        corrects_revision INTEGER,
        reason TEXT,
        effective_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        import_id TEXT NOT NULL REFERENCES imports(import_id),
        fields TEXT NOT NULL,
        PRIMARY KEY (record_id, revision)
    )""",
)
# Reference measurements kept out of net worth, such as broker buying power and FX rates.
_V2 = (
    """CREATE TABLE observations (
        import_id TEXT NOT NULL REFERENCES imports(import_id),
        subject TEXT NOT NULL,
        metric TEXT NOT NULL,
        currency TEXT NOT NULL,
        value TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (import_id, subject, metric, currency)
    )""",
)
# Every provider sync attempt, so a failed refresh is visible instead of silently leaving old data.
_V3 = (
    """CREATE TABLE sync_runs (
        source TEXT NOT NULL,
        attempted_at TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
        error_code TEXT,
        import_id TEXT,
        PRIMARY KEY (source, attempted_at)
    )""",
)
# Macro evidence such as policy and market rates. A revised value is added as a new row, so an
# earlier report can be reproduced from what was known when it was made.
_V4 = (
    """CREATE TABLE evidence_points (
        provider TEXT NOT NULL,
        series_id TEXT NOT NULL,
        observed_on TEXT NOT NULL,
        value TEXT NOT NULL,
        published_on TEXT,
        retrieved_at TEXT NOT NULL,
        PRIMARY KEY (provider, series_id, observed_on, retrieved_at)
    )""",
)
# Official announcement metadata (untrusted external text). A changed title or publication time is
# added as a new version, so both the current metadata and an earlier view can be read.
_V5 = (
    """CREATE TABLE announcements (
        feed_id TEXT NOT NULL,
        link TEXT NOT NULL,
        title TEXT NOT NULL,
        published_at TEXT NOT NULL,
        retrieved_at TEXT NOT NULL,
        PRIMARY KEY (feed_id, link, retrieved_at)
    )""",
)
# Each advice run with the exact sanitized payload, prompt version and model, so advice can be traced
# to the data, assumptions and evidence it was based on.
_V6 = (
    """CREATE TABLE advice_runs (
        run_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        use_case TEXT NOT NULL,
        question TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        payload TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        model TEXT NOT NULL,
        cost_usd TEXT,
        outcome TEXT NOT NULL,
        error_code TEXT,
        advice TEXT
    )""",
)
# Report publication: one row per scheduled slot (unique) or manual send. A slot is claimed before
# sending, so a crash or lost response can never lead to a second automatic send of the same slot.
_V7 = (
    """CREATE TABLE report_runs (
        report_id TEXT PRIMARY KEY,
        slot TEXT,
        kind TEXT NOT NULL,
        trigger TEXT NOT NULL CHECK (trigger IN ('scheduled', 'manual')),
        created_at TEXT NOT NULL,
        as_of TEXT NOT NULL,
        baseline TEXT,
        missed_slots TEXT NOT NULL,
        subject TEXT,
        html_sha256 TEXT,
        status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'failed', 'unknown', 'merged')),
        attempts INTEGER NOT NULL,
        message_id TEXT,
        error_code TEXT,
        finished_at TEXT
    )""",
    "CREATE UNIQUE INDEX report_runs_slot ON report_runs(slot) WHERE slot IS NOT NULL",
)
# Split the claimed run into `building` (no mail request yet: safe to retry after a lease expires) and
# `sending` (request started: an interrupted run becomes unknown and is never resent automatically).
_REPORT_COLUMNS = ("report_id, slot, kind, trigger, created_at, as_of, baseline, missed_slots, subject, html_sha256, "
                   "status, attempts, message_id, error_code, finished_at")
_V8 = (
    """CREATE TABLE report_runs_v8 (
        report_id TEXT PRIMARY KEY,
        slot TEXT,
        kind TEXT NOT NULL,
        trigger TEXT NOT NULL CHECK (trigger IN ('scheduled', 'manual')),
        created_at TEXT NOT NULL,
        as_of TEXT NOT NULL,
        baseline TEXT,
        missed_slots TEXT NOT NULL,
        subject TEXT,
        html_sha256 TEXT,
        status TEXT NOT NULL CHECK (status IN ('building', 'sending', 'sent', 'failed', 'unknown', 'merged')),
        attempts INTEGER NOT NULL,
        message_id TEXT,
        error_code TEXT,
        finished_at TEXT,
        send_started_at TEXT
    )""",
    f"INSERT INTO report_runs_v8 ({_REPORT_COLUMNS}) SELECT {_REPORT_COLUMNS} FROM report_runs",
    "DROP TABLE report_runs",
    "ALTER TABLE report_runs_v8 RENAME TO report_runs",
    "CREATE UNIQUE INDEX report_runs_slot ON report_runs(slot) WHERE slot IS NOT NULL",
)
# Research briefs (web findings gathered without household data) and the proposal follow-up log.
_V9 = (
    """CREATE TABLE research_briefs (
        brief_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        topics TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
        error_code TEXT,
        model TEXT,
        cost_usd TEXT,
        brief TEXT
    )""",
    """CREATE TABLE proposal_log (
        key TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open', 'accepted', 'declined', 'done')),
        first_shown_at TEXT NOT NULL,
        last_shown_at TEXT NOT NULL,
        last_report_id TEXT NOT NULL,
        times_shown INTEGER NOT NULL,
        decided_at TEXT,
        note TEXT
    )""",
)
# Proposal history: an instance per (rule, identity) with the figures it was raised on, and an
# append-only event log (shown, decisions, supersession). Replaces the overwrite-in-place proposal_log.
_V10 = (
    """CREATE TABLE proposal_instances (
        instance_id TEXT PRIMARY KEY,
        key TEXT NOT NULL,
        identity TEXT NOT NULL,
        figures TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open', 'accepted', 'declined', 'done', 'superseded')),
        created_at TEXT NOT NULL,
        status_at TEXT NOT NULL,
        times_shown INTEGER NOT NULL,
        last_shown_at TEXT,
        last_report_id TEXT
    )""",
    """CREATE TABLE proposal_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id TEXT NOT NULL REFERENCES proposal_instances(instance_id),
        at TEXT NOT NULL,
        event TEXT NOT NULL CHECK (event IN ('shown', 'accepted', 'declined', 'done', 'superseded')),
        report_id TEXT,
        figures TEXT,
        note TEXT
    )""",
    # Earlier rows carry no figures, so the next report raises them again as new instances.
    """INSERT INTO proposal_instances SELECT 'legacy-' || key, key, key, '{}', status, first_shown_at,
        COALESCE(decided_at, last_shown_at), times_shown, last_shown_at, last_report_id FROM proposal_log""",
    """INSERT INTO proposal_events (instance_id, at, event, report_id, figures, note)
        SELECT 'legacy-' || key, last_shown_at, 'shown', last_report_id, NULL, note FROM proposal_log""",
    "DROP TABLE proposal_log",
)
# Official apartment transactions (MOLIT) matched to a property record by complex and floor area.
# Only records of the same complex and size are kept; building and unit numbers are never stored.
_V11 = (
    """CREATE TABLE property_deals (
        property_ref TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('trade', 'rent')),
        deal_date TEXT NOT NULL,
        area TEXT NOT NULL,
        price TEXT NOT NULL,
        monthly_rent TEXT NOT NULL,
        floor TEXT NOT NULL,
        contract_type TEXT,
        cancelled INTEGER NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (property_ref, kind, deal_date, area, price, monthly_rent, floor)
    )""",
)
# Questions the client sent as replies to report e-mails (FR-18). A question is claimed before its
# answer is sent, so it is never answered twice; an interrupted or uncertain answer is not retried.
_V12 = (
    """CREATE TABLE mail_questions (
        message_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        report_id TEXT,
        received_at TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('answering', 'answered', 'failed', 'unknown', 'skipped')),
        answer_message_id TEXT,
        error_code TEXT,
        finished_at TEXT
    )""",
)
# property_deals: identical deals (no building number is kept) are counted instead of merged, and a sync
# replaces whole months so withdrawn or corrected records disappear. mail_questions: a sending state and
# attempts, so clean failures are retried and interrupted runs are recovered like reports.
_V13 = (
    """CREATE TABLE property_deals_v13 (
        property_ref TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('trade', 'rent')),
        month TEXT NOT NULL,
        deal_date TEXT NOT NULL,
        area TEXT NOT NULL,
        price TEXT NOT NULL,
        monthly_rent TEXT NOT NULL,
        floor TEXT NOT NULL,
        contract_type TEXT,
        active_count INTEGER NOT NULL,
        cancelled_count INTEGER NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (property_ref, kind, deal_date, area, price, monthly_rent, floor)
    )""",
    """INSERT INTO property_deals_v13 SELECT property_ref, kind, substr(replace(deal_date, '-', ''), 1, 6), deal_date,
        area, price, monthly_rent, floor, contract_type, 1 - cancelled, cancelled, fetched_at FROM property_deals""",
    "DROP TABLE property_deals",
    "ALTER TABLE property_deals_v13 RENAME TO property_deals",
    """CREATE TABLE mail_questions_v13 (
        message_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        report_id TEXT,
        received_at TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('answering', 'sending', 'answered', 'failed', 'unknown', 'skipped')),
        attempts INTEGER NOT NULL,
        answer_message_id TEXT,
        error_code TEXT,
        finished_at TEXT,
        sending_at TEXT
    )""",
    """INSERT INTO mail_questions_v13 SELECT message_id, thread_id, report_id, received_at, claimed_at, status, 1,
        answer_message_id, error_code, finished_at, NULL FROM mail_questions""",
    "DROP TABLE mail_questions",
    "ALTER TABLE mail_questions_v13 RENAME TO mail_questions",
)
# Report lineage: the ledger time, research brief, audited model runs, config, code and canonical data
# a report was built from, so "why did Smith say this" can be reconstructed and questions answered
# against the report they reply to.
_LINEAGE_COLUMNS = ("known_at", "brief_id", "advice_run_ids", "config_sha256", "code_version", "data_sha256")
_V14 = tuple(f"ALTER TABLE report_runs ADD COLUMN {column} TEXT" for column in _LINEAGE_COLUMNS)
_MIGRATIONS = {1: _V1, 2: _V2, 3: _V3, 4: _V4, 5: _V5, 6: _V6, 7: _V7, 8: _V8, 9: _V9, 10: _V10, 11: _V11, 12: _V12,
               13: _V13, 14: _V14}
_COLUMNS = ("record_id, revision, kind, owner_id, source, status, change_type, corrects_revision, "
            "reason, effective_at, recorded_at, import_id, fields")


class LedgerError(Exception):
    """The ledger file cannot be used by this version of Smith."""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a ledger in autocommit mode and create or check its schema. The caller closes it."""
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def connect_read_only(path: Path | str) -> sqlite3.Connection:
    """Open an initialized ledger without creating, migrating or writing the file.

    An older schema is read through an in-memory copy migrated to the current version.
    """
    return _read_view(path, empty_ok=False)


def connect_for_planning(path: Path | str) -> sqlite3.Connection:
    """Open a connection for a dry run that never modifies the file at `path`.

    A missing or uninitialized (schema 0) file is planned against an empty in-memory ledger.
    """
    if not Path(path).exists():
        return connect(":memory:")
    return _read_view(path, empty_ok=True)


def _read_view(path: Path | str, *, empty_ok: bool) -> sqlite3.Connection:
    conn, version = _open_read_only(path)
    if version == SCHEMA_VERSION:
        return conn
    try:
        if version > SCHEMA_VERSION or (version == 0 and not empty_ok):
            raise LedgerError(f"ledger schema {version} is not readable by schema {SCHEMA_VERSION}")
        memory = sqlite3.connect(":memory:", isolation_level=None)
        conn.backup(memory)
    finally:
        conn.close()
    try:
        memory.execute("PRAGMA foreign_keys = ON")
        _migrate(memory)
    except BaseException:
        memory.close()
        raise
    return memory


def _open_read_only(path: Path | str) -> tuple[sqlite3.Connection, int]:
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, isolation_level=None, timeout=5)
    try:
        return conn, conn.execute("PRAGMA user_version").fetchone()[0]
    except BaseException:
        conn.close()
        raise


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise LedgerError(f"ledger schema {version} is newer than supported {SCHEMA_VERSION}")
        for step in range(version + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS[step]:
                conn.execute(statement)
        if version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def apply_import(conn: sqlite3.Connection, batch: ImportBatch, *, recorded_at: datetime,
                 dry_run: bool = False, close_missing: bool = False) -> ImportResult:
    """Apply a batch atomically, or plan it without writing when `dry_run` is set.

    In snapshot mode, active records of the same source and owners that are absent from the
    batch are closed only when `close_missing` is set; otherwise the batch is rejected.

    Raises:
        ImportRejected: the batch conflicts with stored revisions; nothing is written.
    """
    # A dry run only reads, so it must also work on a read-only connection.
    conn.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
    try:
        result = _apply(conn, batch, recorded_at, dry_run, close_missing)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if dry_run else "COMMIT")
    if result.outcome == "applied":
        logger.info("Import applied: import_id=%s revisions=%d", batch.import_id,
                    sum(action is not Action.UNCHANGED for _, _, action in result.actions))
    return result


def _apply(conn: sqlite3.Connection, batch: ImportBatch, recorded_at: datetime,
           dry_run: bool, close_missing: bool) -> ImportResult:
    stored = conn.execute("SELECT payload_sha256 FROM imports WHERE import_id = ?",
                          (batch.import_id,)).fetchone()
    if stored is not None:
        if stored[0] == batch.payload_sha256:
            return ImportResult(batch.import_id, "already_imported", ())
        raise ImportRejected([f"import_id: {batch.import_id} was already imported with different "
                              "content; use a new import_id"])
    problems: list[str] = []
    if batch.as_of > recorded_at:
        problems.append("as_of: must not be in the future")
    snapshot = batch.mode == "snapshot"
    records = _resolve_snapshot(conn, batch, close_missing, problems) if snapshot else list(batch.records)
    planned: list[tuple[RecordInput, Action]] = []
    batch_kinds = {record.record_id: record.kind for record in records}
    for index, record in enumerate(records):
        path = f"records[{index}]"
        action = _plan(record, batch.source, _revisions(conn, record.record_id), path, problems,
                       allow_reopen=snapshot)
        if action is not None:
            planned.append((record, action))
        _check_references(conn, record, batch_kinds, path, problems)
    if problems:
        raise ImportRejected(problems)
    if not dry_run:
        _write(conn, batch, planned, recorded_at)
    actions = tuple((record.record_id, record.revision, action) for record, action in planned)
    return ImportResult(batch.import_id, "dry_run" if dry_run else "applied", actions)


def _resolve_snapshot(conn: sqlite3.Connection, batch: ImportBatch, close_missing: bool,
                      problems: list[str]) -> list[RecordInput]:
    """Number snapshot records against the ledger and add closures for records the source no longer reports.

    A record whose fields match its latest active revision is passed through unchanged, so a
    repeated snapshot adds no revisions.
    """
    records = []
    for record in batch.records:
        history = _revisions(conn, record.record_id)
        latest = history[-1].record if history else None
        if latest is not None and latest.status is Status.ACTIVE and latest.fields == record.fields:
            records.append(latest)
        else:
            records.append(replace(record, revision=latest.revision + 1 if latest else 1))
    reported = {record.record_id for record in batch.records}
    placeholders = ", ".join("?" * len(batch.owner_ids))
    scope = conn.execute(f"SELECT DISTINCT record_id FROM record_revisions WHERE source = ? "
                         f"AND owner_id IN ({placeholders}) ORDER BY record_id",
                         (batch.source, *batch.owner_ids)).fetchall()
    missing = []
    for (record_id,) in scope:
        history = _revisions(conn, record_id)
        if record_id in reported or _state_at(history, _END_OF_TIME).record.status is Status.CLOSED:
            continue
        latest = history[-1].record
        missing.append(RecordInput(record_id, latest.kind, latest.owner_id, batch.as_of, latest.revision + 1,
                                   Status.CLOSED, ChangeType.UPDATE, None, None, {}))
    if missing and not close_missing:
        problems.append(f"snapshot: {len(missing)} active record(s) are missing from this snapshot "
                        f"({', '.join(r.record_id for r in missing)}); close them explicitly to continue")
    return records + missing


def _plan(record: RecordInput, source: str, history: list[StoredRevision], path: str,
          problems: list[str], *, allow_reopen: bool = False) -> Action | None:
    if not history:
        if record.revision != 1:
            problems.append(f"{path}.revision: a new record must start at revision 1")
            return None
        return Action.CREATED
    first, latest = history[0], history[-1].record
    if (record.kind, record.owner_id, source) != (first.record.kind, first.record.owner_id, first.source):
        problems.append(f"{path}: kind, owner_id and source of {record.record_id} cannot change; "
                        "close it and create a new record instead")
        return None
    if record.revision == latest.revision:
        if record == latest:
            return Action.UNCHANGED
        problems.append(f"{path}.revision: revision {latest.revision} of {record.record_id} already exists "
                        f"with different content; use revision {latest.revision + 1}")
        return None
    if record.revision != latest.revision + 1:
        problems.append(f"{path}.revision: expected {latest.revision + 1} for {record.record_id}, "
                        f"got {record.revision}")
        return None
    if record.change_type is ChangeType.CORRECTION:
        return _plan_correction(record, history, path, problems)
    # Real changes move forward in time from the record's current state; past errors are corrections.
    # A provider snapshot may report a closed position again (for example, a stock bought back).
    current = _state_at(history, _END_OF_TIME)
    if current.record.status is Status.CLOSED and not allow_reopen:
        problems.append(f"{path}: {record.record_id} is closed; only corrections are allowed "
                        "(use a new record id to reopen)")
        return None
    if record.effective_at <= current.record.effective_at:
        problems.append(f"{path}.effective_at: must be later than {current.record.effective_at.isoformat()} "
                        f"(revision {current.record.revision}); use a correction to fix past values")
        return None
    return Action.CLOSED if record.status is Status.CLOSED else Action.UPDATED


def _plan_correction(record: RecordInput, history: list[StoredRevision], path: str,
                     problems: list[str]) -> Action | None:
    """A correction replaces its target for the target's own period, so it keeps the target's time."""
    if record.corrects_revision in {r.record.corrects_revision for r in history}:
        problems.append(f"{path}.corrects_revision: revision {record.corrects_revision} was already "
                        "corrected; correct the latest correction instead")
        return None
    target = history[record.corrects_revision - 1].record  # Revisions are contiguous from 1.
    if record.effective_at != target.effective_at:
        problems.append(f"{path}.effective_at: a correction must keep the effective_at of revision "
                        f"{target.revision} ({target.effective_at.isoformat()})")
        return None
    return Action.CORRECTED


def _check_references(conn: sqlite3.Connection, record: RecordInput, batch_kinds: dict[str, Kind],
                      path: str, problems: list[str]) -> None:
    for field, expected in REFERENCE_FIELDS.items():
        target = record.fields.get(field)
        if target is None:
            continue
        row = conn.execute("SELECT kind FROM record_revisions WHERE record_id = ? LIMIT 1", (target,)).fetchone()
        kind = batch_kinds.get(target) or (Kind(row[0]) if row else None)
        if kind is not expected:
            problems.append(f"{path}.{field}: must reference an existing {expected} record")


def _write(conn: sqlite3.Connection, batch: ImportBatch, planned: list[tuple[RecordInput, Action]],
           recorded_at: datetime) -> None:
    now = _db_time(recorded_at)
    conn.execute("INSERT INTO imports VALUES (?, ?, ?, ?, ?, ?)",
                 (batch.import_id, batch.payload_sha256, batch.source, batch.mode, _db_time(batch.as_of), now))
    conn.executemany("INSERT OR IGNORE INTO owners VALUES (?, ?)",
                     [(owner_id, batch.import_id) for owner_id in batch.owner_ids])
    conn.executemany(
        f"INSERT INTO record_revisions ({_COLUMNS}) VALUES ({', '.join('?' * 13)})",
        [(r.record_id, r.revision, r.kind, r.owner_id, batch.source, r.status, r.change_type,
          r.corrects_revision, r.reason, _db_time(r.effective_at), now, batch.import_id,
          json.dumps(r.fields, sort_keys=True, ensure_ascii=False))
         for r, action in planned if action is not Action.UNCHANGED],
    )
    conn.executemany("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?)",
                     [(batch.import_id, o.subject, o.metric, o.currency, o.value, _db_time(o.observed_at))
                      for o in batch.observations])


def record_sync_run(conn: sqlite3.Connection, *, source: str, attempted_at: datetime, outcome: str,
                    error_code: str | None = None, import_id: str | None = None) -> None:
    """Persist one sync attempt. `error_code` is a short provider or Smith code, never a message."""
    conn.execute("INSERT INTO sync_runs VALUES (?, ?, ?, ?, ?)",
                 (source, _db_time(attempted_at), outcome, error_code, import_id))


def sync_status(conn: sqlite3.Connection, *, known_at: datetime) -> dict[str, dict[str, Any]]:
    """Return, per source, the last successful and last failed attempt recorded by `known_at`."""
    rows = conn.execute("SELECT source, attempted_at, outcome, error_code FROM sync_runs "
                        "WHERE attempted_at <= ? ORDER BY attempted_at", (_db_time(known_at),)).fetchall()
    status: dict[str, dict[str, Any]] = {}
    for source, attempted_at, outcome, error_code in rows:
        entry = status.setdefault(source, {"last_success": None, "last_failure": None, "last_error": None})
        if outcome == "success":
            entry["last_success"] = datetime.fromisoformat(attempted_at)
        else:
            entry["last_failure"], entry["last_error"] = datetime.fromisoformat(attempted_at), error_code
    return status


def store_evidence(conn: sqlite3.Connection, points: list[tuple[str, str, date, str, date | None]], *,
                   retrieved_at: datetime) -> int:
    """Store (provider, series_id, observed_on, value, published_on) points atomically and return how
    many were new. A point whose latest stored value is unchanged is skipped."""
    # Read, compare and insert under one write lock so concurrent syncs cannot both add a value.
    conn.execute("BEGIN IMMEDIATE")
    try:
        known = load_evidence(conn, known_at=_END_OF_TIME)
        rows = [(provider, series_id, observed_on.isoformat(), value,
                 published_on.isoformat() if published_on else None, _db_time(retrieved_at))
                for provider, series_id, observed_on, value, published_on in points
                if known.get((provider, series_id), {}).get(observed_on, (None,))[0] != value]
        conn.executemany("INSERT INTO evidence_points VALUES (?, ?, ?, ?, ?, ?)", rows)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return len(rows)


def load_evidence(conn: sqlite3.Connection, *,
                  known_at: datetime) -> dict[tuple[str, str], dict[date, tuple[str, date | None]]]:
    """Return {(provider, series_id): {observed_on: (value, published_on)}} as known at `known_at`."""
    rows = conn.execute("SELECT provider, series_id, observed_on, value, published_on FROM evidence_points "
                        "WHERE retrieved_at <= ? ORDER BY retrieved_at", (_db_time(known_at),)).fetchall()
    series: dict[tuple[str, str], dict[date, tuple[str, date | None]]] = {}
    for provider, series_id, observed_on, value, published_on in rows:
        series.setdefault((provider, series_id), {})[date.fromisoformat(observed_on)] = (
            value, date.fromisoformat(published_on) if published_on else None)
    return series


def store_announcements(conn: sqlite3.Connection, items: list[Announcement], *, retrieved_at: datetime) -> int:
    """Store new announcements and changed metadata as new versions; return how many were added."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        latest = {(a.feed_id, a.link): a for a in _announcement_versions(conn, _END_OF_TIME)}
        rows = [(a.feed_id, a.link, a.title, _db_time(a.published_at), _db_time(retrieved_at))
                for a in items if latest.get((a.feed_id, a.link)) != a]
        conn.executemany("INSERT INTO announcements VALUES (?, ?, ?, ?, ?)", rows)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return len(rows)


def load_announcements(conn: sqlite3.Connection, *, since: datetime, until: datetime,
                       known_at: datetime) -> list[Announcement]:
    """Latest known version of each announcement published in [since, until], newest first."""
    current = [a for a in _announcement_versions(conn, known_at) if since <= a.published_at <= until]
    return sorted(current, key=lambda a: a.published_at, reverse=True)


def _announcement_versions(conn: sqlite3.Connection, known_at: datetime) -> list[Announcement]:
    rows = conn.execute("SELECT feed_id, link, title, published_at FROM announcements WHERE retrieved_at <= ? "
                        "ORDER BY retrieved_at", (_db_time(known_at),)).fetchall()
    latest = {(feed_id, link): Announcement(feed_id, link, title, datetime.fromisoformat(published_at))
              for feed_id, link, title, published_at in rows}
    return list(latest.values())


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Hold one read transaction so several queries see the same ledger state. Nested use is a no-op."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("ROLLBACK")


def record_advice_run(conn: sqlite3.Connection, *, run_id: str, created_at: datetime, use_case: str,
                      question: str, payload: str, prompt_version: str, model: str, cost_usd: str | None,
                      outcome: str, error_code: str | None = None, advice: str | None = None) -> None:
    """Persist one advice attempt; the payload is the sanitized text that was (or would have been) sent."""
    conn.execute("INSERT INTO advice_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (run_id, _db_time(created_at), use_case, question,
                  hashlib.sha256(payload.encode("utf-8")).hexdigest(), payload, prompt_version, model, cost_usd,
                  outcome, error_code, advice))


MAX_SEND_ATTEMPTS = 3
# A run still `building` or `sending` after this long was interrupted (crash, power, killed task).
REPORT_LEASE = timedelta(minutes=30)
# Final outcomes that settle the slots a run reported as missed: the message was or may have been delivered.
_DELIVERED = ("sent", "unknown")


def claim_report(conn: sqlite3.Connection, *, report_id: str, slot: datetime | None, kind: str, trigger: str,
                 created_at: datetime, as_of: datetime, baseline: datetime | None,
                 missed_slots: list[datetime]) -> bool:
    """Atomically claim a report run in the `building` state. Returns False when the slot was already
    sent, is in an unknown state, is in progress, or has used all attempts. Missed slots are only
    settled (merged) when this run is finished as sent or unknown."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = None
        if slot is not None:
            row = conn.execute("SELECT report_id, status, attempts FROM report_runs WHERE slot = ?",
                               (_db_time(slot),)).fetchone()
            if row is not None and (row[1] != "failed" or row[2] >= MAX_SEND_ATTEMPTS):
                conn.execute("ROLLBACK")
                return False
        missed = json.dumps([_db_time(m) for m in missed_slots])
        if row is not None:
            conn.execute("UPDATE report_runs SET report_id = ?, status = 'building', attempts = attempts + 1, "
                         "created_at = ?, as_of = ?, baseline = ?, missed_slots = ?, error_code = NULL, "
                         "finished_at = NULL, send_started_at = NULL WHERE slot = ?",
                         (report_id, _db_time(created_at), _db_time(as_of), _optional_time(baseline), missed,
                          _db_time(slot)))
        else:
            conn.execute(f"INSERT INTO report_runs ({_REPORT_COLUMNS}) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'building', 1, NULL, NULL, NULL)",
                         (report_id, _optional_time(slot), kind, trigger, _db_time(created_at), _db_time(as_of),
                          _optional_time(baseline), missed))
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return True


def start_sending(conn: sqlite3.Connection, *, report_id: str, now: datetime, subject: str,
                  html_sha256: str) -> bool:
    """Mark the point after which the mail request may have reached Gmail. Committed before the request.

    Returns False when this run no longer holds its claim (its lease expired and it was recovered or
    the slot was claimed again by a newer run); the caller must then not send.
    """
    cursor = conn.execute("UPDATE report_runs SET status = 'sending', send_started_at = ?, subject = ?, "
                          "html_sha256 = ? WHERE report_id = ? AND status = 'building'",
                          (_db_time(now), subject, html_sha256, report_id))
    return cursor.rowcount == 1


def finish_report(conn: sqlite3.Connection, *, report_id: str, status: str, finished_at: datetime,
                  subject: str | None = None, html_sha256: str | None = None, message_id: str | None = None,
                  error_code: str | None = None, shown_proposals: list[dict[str, Any]] | None = None) -> None:
    """Record the final outcome. A delivered (or possibly delivered) run settles its missed slots and
    records the proposals it carried in the same transaction; after a clean failure nothing else changes."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE report_runs SET status = ?, finished_at = ?, subject = COALESCE(?, subject), "
                     "html_sha256 = COALESCE(?, html_sha256), message_id = ?, error_code = ? WHERE report_id = ?",
                     (status, _db_time(finished_at), subject, html_sha256, message_id, error_code, report_id))
        if status in _DELIVERED:
            _settle_missed(conn, report_id, finished_at)
            _record_shown(conn, report_id, finished_at, shown_proposals or [])
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def recover_stale_reports(conn: sqlite3.Connection, *, now: datetime) -> list[dict[str, Any]]:
    """Close runs interrupted longer than REPORT_LEASE ago and return them.

    `building` had not contacted Gmail, so it becomes a retryable failure. `sending` may have been
    delivered, so it becomes unknown and is never resent automatically.
    """
    cutoff = _db_time(now - REPORT_LEASE)
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Rows migrated from v7 have no send_started_at; their claim time stands in for it.
        stale = conn.execute("SELECT report_id, slot, status FROM report_runs WHERE (status = 'building' AND "
                             "created_at < ?) OR (status = 'sending' AND COALESCE(send_started_at, created_at) < ?)",
                             (cutoff, cutoff)).fetchall()
        for report_id, _, status in stale:
            outcome, code = (("failed", "interrupted-before-send") if status == "building"
                             else ("unknown", "interrupted-during-send"))
            conn.execute("UPDATE report_runs SET status = ?, error_code = ?, finished_at = ? WHERE report_id = ?",
                         (outcome, code, _db_time(now), report_id))
            if outcome in _DELIVERED:
                _settle_missed(conn, report_id, now)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return [{"report_id": r, "slot": s, "was": status} for r, s, status in stale]


def _settle_missed(conn: sqlite3.Connection, report_id: str, now: datetime) -> None:
    row = conn.execute("SELECT kind, missed_slots FROM report_runs WHERE report_id = ?", (report_id,)).fetchone()
    if row is None:
        return
    kind, missed = row
    for slot in json.loads(missed):
        # A missed slot may be an earlier scheduled run that failed; it is covered by this report now.
        conn.execute("UPDATE report_runs SET status = 'merged', error_code = ?, finished_at = ? "
                     "WHERE slot = ? AND status = 'failed'", (f"merged-into:{report_id}", _db_time(now), slot))
        conn.execute(f"INSERT OR IGNORE INTO report_runs ({_REPORT_COLUMNS}) "
                     "VALUES (?, ?, ?, 'scheduled', ?, ?, NULL, '[]', NULL, NULL, 'merged', 0, NULL, ?, ?)",
                     (f"{report_id}-merged-{slot}", slot, kind, _db_time(now), _db_time(now),
                      f"merged-into:{report_id}", _db_time(now)))


def report_runs(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    """Most recent report runs first."""
    columns = tuple(_REPORT_COLUMNS.split(", ")) + ("send_started_at",)
    rows = conn.execute(f"SELECT {', '.join(columns)} FROM report_runs ORDER BY created_at DESC, slot DESC LIMIT ?",
                        (limit,)).fetchall()
    return [dict(zip(columns, row)) for row in rows]


def unresolved_runs(conn: sqlite3.Connection, *, since: datetime | None) -> list[dict[str, Any]]:
    """Runs after the last sent report that may have been delivered (unknown), oldest first. Failed
    scheduled slots are not listed here: `delivery.due` reports them as missed and the next delivered
    report settles them."""
    floor = "" if since is None else _db_time(since)
    runs = [r for r in report_runs(conn, limit=1000) if r["created_at"] > floor and r["status"] == "unknown"]
    return sorted(runs, key=lambda r: r["created_at"])


def record_research(conn: sqlite3.Connection, *, brief_id: str, created_at: datetime, topics: list[dict[str, str]],
                    outcome: str, error_code: str | None = None, model: str | None = None, cost_usd: str | None = None,
                    brief: dict[str, Any] | None = None) -> None:
    """Store one research run. Topics and briefs never contain household amounts or identifiers."""
    conn.execute("INSERT INTO research_briefs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (brief_id, _db_time(created_at), json.dumps(topics, ensure_ascii=False), outcome, error_code, model,
                  cost_usd, None if brief is None else json.dumps(brief, ensure_ascii=False)))


def latest_research(conn: sqlite3.Connection, *, since: datetime,
                    topics: list[dict[str, str]] | None = None) -> dict[str, Any] | None:
    """The newest successful brief created at or after `since`; with `topics`, only one researched for
    exactly that topic set."""
    rows = conn.execute("SELECT brief_id, created_at, topics, model, brief FROM research_briefs WHERE outcome = "
                        "'success' AND created_at >= ? ORDER BY created_at DESC", (_db_time(since),)).fetchall()
    for row in rows:
        found = _brief(row)
        if topics is None or found["topics"] == topics:
            return found
    return None


def get_research(conn: sqlite3.Connection, brief_id: str) -> dict[str, Any] | None:
    """One successful brief by id (the brief a report is pinned to)."""
    row = conn.execute("SELECT brief_id, created_at, topics, model, brief FROM research_briefs WHERE brief_id = ? "
                       "AND outcome = 'success'", (brief_id,)).fetchone()
    return None if row is None else _brief(row)


def _brief(row: tuple) -> dict[str, Any]:
    brief_id, created_at, topics, model, brief = row
    return {"brief_id": brief_id, "created_at": datetime.fromisoformat(created_at), "topics": json.loads(topics),
            "model": model, "brief": json.loads(brief)}


PROPOSAL_DECISIONS = ("accepted", "declined", "done")
_INSTANCE_COLUMNS = ("instance_id", "key", "identity", "figures", "status", "created_at", "status_at", "times_shown",
                     "last_shown_at", "last_report_id")


def active_proposals(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The current (not superseded) instance per proposal identity, with its anchor figures."""
    rows = conn.execute(f"SELECT {', '.join(_INSTANCE_COLUMNS)} FROM proposal_instances WHERE status != 'superseded' "
                        "ORDER BY created_at").fetchall()
    found = {}
    for row in rows:
        item = dict(zip(_INSTANCE_COLUMNS, row))
        item["figures"] = json.loads(item["figures"])
        found[item["identity"]] = item
    return found


def proposal_events(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    columns = ("event_id", "instance_id", "at", "event", "report_id", "note")
    rows = conn.execute(f"SELECT e.{', e.'.join(columns)}, i.key FROM proposal_events e JOIN proposal_instances i "
                        "USING (instance_id) ORDER BY e.event_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(zip(columns + ("key",), row)) for row in rows]


def _record_shown(conn: sqlite3.Connection, report_id: str, at: datetime, shown: list[dict[str, Any]]) -> None:
    """Inside the caller's transaction: a renewed proposal (new identity or materially changed figures)
    supersedes the previous instance and starts a new one; otherwise the instance is shown again."""
    active = active_proposals(conn)
    for proposal in shown:
        current = active.get(proposal["identity"])
        figures = json.dumps(proposal["figures"], ensure_ascii=False, sort_keys=True)
        if current is None or proposal["renewed"]:
            if current is not None:
                conn.execute("UPDATE proposal_instances SET status = 'superseded', status_at = ? WHERE instance_id = ?",
                             (_db_time(at), current["instance_id"]))
                conn.execute("INSERT INTO proposal_events (instance_id, at, event, report_id) VALUES (?, ?, "
                             "'superseded', ?)", (current["instance_id"], _db_time(at), report_id))
            instance_id = f"{report_id}-{proposal['key']}"
            conn.execute(f"INSERT INTO proposal_instances ({', '.join(_INSTANCE_COLUMNS)}) "
                         "VALUES (?, ?, ?, ?, 'open', ?, ?, 1, ?, ?)",
                         (instance_id, proposal["key"], proposal["identity"], figures, _db_time(at), _db_time(at),
                          _db_time(at), report_id))
        else:
            instance_id = current["instance_id"]
            conn.execute("UPDATE proposal_instances SET times_shown = times_shown + 1, last_shown_at = ?, "
                         "last_report_id = ? WHERE instance_id = ?", (_db_time(at), report_id, instance_id))
        conn.execute("INSERT INTO proposal_events (instance_id, at, event, report_id, figures) VALUES (?, ?, 'shown', "
                     "?, ?)", (instance_id, _db_time(at), report_id, figures))


def decide_proposal(conn: sqlite3.Connection, *, key: str, status: str, decided_at: datetime,
                    note: str | None = None) -> bool:
    """Record the client's decision on the latest current instance of a proposal. The decision is an
    event; nothing earlier is overwritten. Returns False when no such proposal was delivered."""
    if status not in PROPOSAL_DECISIONS:
        raise ValueError(f"status must be one of {', '.join(PROPOSAL_DECISIONS)}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT instance_id FROM proposal_instances WHERE key = ? AND status != 'superseded' "
                           "ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
        if row is not None:
            conn.execute("UPDATE proposal_instances SET status = ?, status_at = ? WHERE instance_id = ?",
                         (status, _db_time(decided_at), row[0]))
            conn.execute("INSERT INTO proposal_events (instance_id, at, event, note) VALUES (?, ?, ?, ?)",
                         (row[0], _db_time(decided_at), status, note))
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return row is not None


_DEAL_COLUMNS = ("property_ref", "kind", "month", "deal_date", "area", "price", "monthly_rent", "floor",
                 "contract_type", "active_count", "cancelled_count")
_DEAL_KEY = ("property_ref", "kind", "deal_date", "area", "price", "monthly_rent", "floor")


def replace_deals(conn: sqlite3.Connection, deals: list[dict[str, Any]], *, months: set[tuple[str, str, str]],
                  properties: set[str], fetched_at: datetime) -> int:
    """Make the stored deals match one sync exactly: every fetched (property, kind, YYYYMM) is replaced
    as a whole, and properties no longer configured are removed. Identical deals are counted (active and
    cancelled separately), so the result does not depend on the order the API lists them in.
    Returns the number of stored rows after the sync."""
    grouped: dict[tuple, dict[str, Any]] = {}
    for deal in deals:
        key = tuple(deal[c] for c in _DEAL_KEY)
        row = grouped.setdefault(key, {**{c: deal[c] for c in _DEAL_KEY}, "month": deal["deal_date"][:7].replace("-", ""),
                                       "contract_type": deal.get("contract_type"), "active_count": 0, "cancelled_count": 0})
        row["cancelled_count" if deal["cancelled"] else "active_count"] += 1
        row["contract_type"] = row["contract_type"] or deal.get("contract_type")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for ref, kind, month in months:
            conn.execute("DELETE FROM property_deals WHERE property_ref = ? AND kind = ? AND month = ?", (ref, kind, month))
        placeholders = ", ".join("?" for _ in properties) or "''"
        conn.execute(f"DELETE FROM property_deals WHERE property_ref NOT IN ({placeholders})", tuple(properties))
        for row in grouped.values():
            conn.execute(f"INSERT INTO property_deals ({', '.join(_DEAL_COLUMNS)}, fetched_at) VALUES "
                         f"({', '.join('?' for _ in _DEAL_COLUMNS)}, ?)",
                         (*(row[c] for c in _DEAL_COLUMNS), _db_time(fetched_at)))
        stored = conn.execute("SELECT count(*) FROM property_deals").fetchone()[0]
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return stored


def load_deals(conn: sqlite3.Connection, *, since: date) -> list[dict[str, Any]]:
    rows = conn.execute(f"SELECT {', '.join(_DEAL_COLUMNS)} FROM property_deals WHERE deal_date >= ? ORDER BY deal_date",
                        (since.isoformat(),)).fetchall()
    return [dict(zip(_DEAL_COLUMNS, row)) for row in rows]


def latest_advice(conn: sqlite3.Connection, *, use_case: str) -> dict[str, Any] | None:
    """The newest successful audited model output for a use case (parsed JSON), or None."""
    row = conn.execute("SELECT created_at, advice FROM advice_runs WHERE use_case = ? AND outcome = 'success' "
                       "AND advice IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1", (use_case,)).fetchone()
    return None if row is None else {"created_at": datetime.fromisoformat(row[0]), "content": json.loads(row[1])}


MAX_ANSWER_ATTEMPTS = 3
QUESTION_LEASE = timedelta(minutes=30)


def claim_question(conn: sqlite3.Connection, *, message_id: str, thread_id: str, report_id: str | None,
                   received_at: datetime, now: datetime, daily_limit: int | None = None) -> bool:
    """Atomically claim a question for answering: a new question, or a clean failure with attempts left.
    With `daily_limit`, the claim is refused once that many answers were claimed in the last 24 hours."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if daily_limit is not None and _answers_since(conn, now - timedelta(days=1)) >= daily_limit:
            conn.execute("ROLLBACK")
            return False
        row = conn.execute("SELECT status, attempts FROM mail_questions WHERE message_id = ?", (message_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO mail_questions (message_id, thread_id, report_id, received_at, claimed_at, status, "
                         "attempts) VALUES (?, ?, ?, ?, ?, 'answering', 1)",
                         (message_id, thread_id, report_id, _db_time(received_at), _db_time(now)))
        elif row[0] == "failed" and row[1] < MAX_ANSWER_ATTEMPTS:
            conn.execute("UPDATE mail_questions SET status = 'answering', attempts = attempts + 1, claimed_at = ?, "
                         "error_code = NULL, finished_at = NULL, sending_at = NULL WHERE message_id = ?",
                         (_db_time(now), message_id))
        else:
            conn.execute("ROLLBACK")
            return False
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return True


def skip_question(conn: sqlite3.Connection, *, message_id: str, thread_id: str, now: datetime, reason: str) -> None:
    conn.execute("INSERT OR IGNORE INTO mail_questions (message_id, thread_id, report_id, received_at, claimed_at, status, "
                 "attempts, error_code, finished_at) VALUES (?, ?, NULL, ?, ?, 'skipped', 0, ?, ?)",
                 (message_id, thread_id, _db_time(now), _db_time(now), reason, _db_time(now)))


def start_answer(conn: sqlite3.Connection, *, message_id: str, now: datetime) -> bool:
    """Mark the point after which the reply may have been sent; False if the claim was lost."""
    cursor = conn.execute("UPDATE mail_questions SET status = 'sending', sending_at = ? WHERE message_id = ? AND "
                          "status = 'answering'", (_db_time(now), message_id))
    return cursor.rowcount == 1


def finish_question(conn: sqlite3.Connection, *, message_id: str, status: str, now: datetime,
                    answer_message_id: str | None = None, error_code: str | None = None) -> None:
    conn.execute("UPDATE mail_questions SET status = ?, answer_message_id = ?, error_code = ?, finished_at = ? "
                 "WHERE message_id = ?", (status, answer_message_id, error_code, _db_time(now), message_id))


def recover_questions(conn: sqlite3.Connection, *, now: datetime) -> list[dict[str, Any]]:
    """Interrupted answers: `answering` (no reply sent yet) becomes a retryable failure; `sending` may have
    been delivered, so it becomes unknown and is never resent."""
    cutoff = _db_time(now - QUESTION_LEASE)
    stale = conn.execute("SELECT message_id, status FROM mail_questions WHERE (status = 'answering' AND claimed_at < ?) "
                         "OR (status = 'sending' AND sending_at < ?)", (cutoff, cutoff)).fetchall()
    for message_id, status in stale:
        outcome, code = ("failed", "interrupted-before-send") if status == "answering" else ("unknown", "interrupted-during-send")
        finish_question(conn, message_id=message_id, status=outcome, now=now, error_code=code)
    return [{"message_id": m, "was": status} for m, status in stale]


def seen_questions(conn: sqlite3.Connection) -> set[str]:
    """Messages not to pick up again: everything except clean failures that still have attempts left."""
    return {row[0] for row in conn.execute("SELECT message_id FROM mail_questions WHERE NOT (status = 'failed' AND "
                                           "attempts < ?)", (MAX_ANSWER_ATTEMPTS,))}


def answer_message_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT answer_message_id FROM mail_questions WHERE answer_message_id IS NOT NULL")}


def _answers_since(conn: sqlite3.Connection, since: datetime) -> int:
    return conn.execute("SELECT count(*) FROM mail_questions WHERE status IN ('answering', 'sending', 'answered', "
                        "'unknown') AND claimed_at >= ?", (_db_time(since),)).fetchone()[0]


def record_lineage(conn: sqlite3.Connection, *, report_id: str, known_at: datetime, brief_id: str | None,
                   advice_run_ids: list[str], config_sha256: str | None, code_version: str | None,
                   data_sha256: str) -> None:
    """Store what a report was built from (before it is sent, so an uncertain send keeps it too)."""
    conn.execute("UPDATE report_runs SET known_at = ?, brief_id = ?, advice_run_ids = ?, config_sha256 = ?, "
                 "code_version = ?, data_sha256 = ? WHERE report_id = ?",
                 (_db_time(known_at), brief_id, json.dumps(advice_run_ids), config_sha256, code_version, data_sha256,
                  report_id))


def get_report(conn: sqlite3.Connection, report_id: str) -> dict[str, Any] | None:
    """One report run with its lineage (lineage fields are None for reports sent before v14)."""
    columns = tuple(_REPORT_COLUMNS.split(", ")) + _LINEAGE_COLUMNS
    row = conn.execute(f"SELECT {', '.join(columns)} FROM report_runs WHERE report_id = ?", (report_id,)).fetchone()
    if row is None:
        return None
    found = dict(zip(columns, row))
    found["advice_run_ids"] = json.loads(found["advice_run_ids"]) if found["advice_run_ids"] else []
    return found


def get_advice(conn: sqlite3.Connection, run_ids: list[str], *, use_case: str,
               created_at: str | None = None) -> dict[str, Any] | None:
    """The last successful audited output among `run_ids` for a use case (a report's own narrative).
    Reports sent before lineage existed (no run ids) match on the report's creation time instead: the
    publish run audits its narrative with that same time."""
    if run_ids:
        where, params = f"run_id IN ({', '.join('?' * len(run_ids))})", tuple(run_ids)
    elif created_at is not None:
        where, params = "created_at = ?", (created_at,)
    else:
        return None
    row = conn.execute(f"SELECT created_at, advice FROM advice_runs WHERE {where} AND use_case = ? "
                       "AND outcome = 'success' AND advice IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1",
                       (*params, use_case)).fetchone()
    return None if row is None else {"created_at": datetime.fromisoformat(row[0]), "content": json.loads(row[1])}


def changed_since(conn: sqlite3.Connection, known_at: datetime) -> bool:
    """Whether any import was recorded after `known_at` (the ledger moved on since a report)."""
    return conn.execute("SELECT 1 FROM imports WHERE recorded_at > ? LIMIT 1", (_db_time(known_at),)).fetchone() is not None


def last_sent_report(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The latest successfully sent report; its snapshot time is the next report's baseline."""
    sent = [r for r in report_runs(conn, limit=1000) if r["status"] == "sent"]
    return max(sent, key=lambda r: r["as_of"], default=None)


def mark_slot(conn: sqlite3.Connection, *, slot: datetime, kind: str, now: datetime, reason: str) -> None:
    """Record a slot that must never be sent (for example one that passed before scheduling was enabled)."""
    conn.execute(f"INSERT OR IGNORE INTO report_runs ({_REPORT_COLUMNS}) "
                 "VALUES (?, ?, ?, 'scheduled', ?, ?, NULL, '[]', NULL, NULL, 'merged', 0, NULL, ?, ?)",
                 (f"marker-{_db_time(slot)}", _db_time(slot), kind, _db_time(now), _db_time(now), reason, _db_time(now)))


def known_slots(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT slot FROM report_runs WHERE slot IS NOT NULL")}


def _optional_time(value: datetime | None) -> str | None:
    return None if value is None else _db_time(value)


def latest_observations(conn: sqlite3.Connection, *, as_of: datetime,
                        known_at: datetime) -> dict[tuple[str, str, str], tuple[Observation, str]]:
    """Return the latest observation per (subject, metric, currency) observed by `as_of` and recorded
    by `known_at`, together with its source."""
    rows = conn.execute(
        "SELECT o.subject, o.metric, o.currency, o.value, o.observed_at, i.source FROM observations o "
        "JOIN imports i ON i.import_id = o.import_id WHERE o.observed_at <= ? AND i.recorded_at <= ? "
        "ORDER BY o.observed_at", (_db_time(as_of), _db_time(known_at))).fetchall()
    latest = {}
    for subject, metric, currency, value, observed_at, source in rows:
        latest[(subject, metric, currency)] = (
            Observation(subject, metric, currency, value, datetime.fromisoformat(observed_at)), source)
    return latest


def record_history(conn: sqlite3.Connection, record_id: str) -> list[StoredRevision]:
    """Return every stored revision of a record in revision order."""
    return _revisions(conn, record_id)


def record_states(conn: sqlite3.Connection, *, as_of: datetime, known_at: datetime) -> list[StoredRevision]:
    """Return the revision in effect at `as_of` for each record, using only revisions recorded by `known_at`.

    Closed records are included with status closed; records not yet effective are omitted.
    """
    rows = conn.execute(f"SELECT {_COLUMNS} FROM record_revisions WHERE recorded_at <= ? "
                        "ORDER BY record_id, revision", (_db_time(known_at),)).fetchall()
    states = []
    for _, group in groupby((_from_row(row) for row in rows), key=lambda r: r.record.record_id):
        state = _state_at(list(group), as_of)
        if state is not None:
            states.append(state)
    return states


def _state_at(history: list[StoredRevision], as_of: datetime) -> StoredRevision | None:
    superseded = {r.record.corrects_revision for r in history}
    candidates = [r for r in history if r.record.revision not in superseded and r.record.effective_at <= as_of]
    return max(candidates, key=lambda r: (r.record.effective_at, r.record.revision), default=None)


def _revisions(conn: sqlite3.Connection, record_id: str) -> list[StoredRevision]:
    rows = conn.execute(f"SELECT {_COLUMNS} FROM record_revisions WHERE record_id = ? ORDER BY revision",
                        (record_id,)).fetchall()
    return [_from_row(row) for row in rows]


def _from_row(row: tuple) -> StoredRevision:
    (record_id, revision, kind, owner_id, source, status, change_type, corrects, reason,
     effective_at, recorded_at, import_id, fields) = row
    record = RecordInput(record_id, Kind(kind), owner_id, datetime.fromisoformat(effective_at), revision,
                         Status(status), ChangeType(change_type), corrects, reason, json.loads(fields))
    return StoredRevision(record, source, datetime.fromisoformat(recorded_at), import_id)


def _db_time(value: datetime) -> str:
    # One fixed UTC format keeps text comparison in SQL chronological.
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
