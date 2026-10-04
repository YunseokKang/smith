"""SQLite ledger of append-only record revisions.

The state of a record at time T is chosen from its revisions that no correction has
superseded: the latest `effective_at` not after T wins, and ties go to the higher revision.
Filtering by `recorded_at` reproduces what the ledger knew at an earlier time.
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path

from smith.records import (
    REFERENCE_FIELDS, Action, ChangeType, ImportBatch, ImportRejected, ImportResult, Kind, RecordInput,
    Status, StoredRevision,
)

logger = logging.getLogger(__name__)
_END_OF_TIME = datetime.max.replace(tzinfo=timezone.utc)

SCHEMA_VERSION = 1
_SCHEMA = (
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
    """Open an existing, initialized ledger without creating, migrating or writing anything."""
    conn, version = _open_read_only(path)
    if version != SCHEMA_VERSION:
        conn.close()
        raise LedgerError(f"ledger schema {version} is not the supported version {SCHEMA_VERSION}")
    return conn


def connect_for_planning(path: Path | str) -> sqlite3.Connection:
    """Open a connection for a dry run that never modifies the file at `path`.

    A missing or uninitialized (schema 0) file is planned against an empty in-memory ledger.
    """
    if not Path(path).exists():
        return connect(":memory:")
    conn, version = _open_read_only(path)
    if version == SCHEMA_VERSION:
        return conn
    conn.close()
    if version != 0:
        raise LedgerError(f"ledger schema {version} is not the supported version {SCHEMA_VERSION}")
    return connect(":memory:")


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
        if version == 0:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def apply_import(conn: sqlite3.Connection, batch: ImportBatch, *, recorded_at: datetime,
                 dry_run: bool = False) -> ImportResult:
    """Apply a batch atomically, or plan it without writing when `dry_run` is set.

    Raises:
        ImportRejected: the batch conflicts with stored revisions; nothing is written.
    """
    # A dry run only reads, so it must also work on a read-only connection.
    conn.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
    try:
        result = _apply(conn, batch, recorded_at, dry_run)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if dry_run else "COMMIT")
    if result.outcome == "applied":
        logger.info("Import applied: import_id=%s revisions=%d", batch.import_id,
                    sum(action is not Action.UNCHANGED for _, _, action in result.actions))
    return result


def _apply(conn: sqlite3.Connection, batch: ImportBatch, recorded_at: datetime,
           dry_run: bool) -> ImportResult:
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
    planned: list[tuple[RecordInput, Action]] = []
    batch_kinds = {record.record_id: record.kind for record in batch.records}
    for index, record in enumerate(batch.records):
        path = f"records[{index}]"
        action = _plan(record, batch.source, _revisions(conn, record.record_id), path, problems)
        if action is not None:
            planned.append((record, action))
        _check_references(conn, record, batch_kinds, path, problems)
    if problems:
        raise ImportRejected(problems)
    if not dry_run:
        _write(conn, batch, planned, recorded_at)
    actions = tuple((record.record_id, record.revision, action) for record, action in planned)
    return ImportResult(batch.import_id, "dry_run" if dry_run else "applied", actions)


def _plan(record: RecordInput, source: str, history: list[StoredRevision], path: str,
          problems: list[str]) -> Action | None:
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
    current = _state_at(history, _END_OF_TIME)
    if current.record.status is Status.CLOSED:
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
