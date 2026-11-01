"""The trade ledger: plans, order legs and an append-only event trail in their own SQLite file (`data/trades.db`).

Trade state is kept apart from the financial ledger so the order path has one small source of truth that the report
code never writes. Every transition is a conditional UPDATE on the expected status, so two runs cannot both act on
one plan. The authenticator code itself is never stored, only its time step (to refuse a second use).
"""
import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
PLAN_STATUSES = ("proposed", "confirm_sent", "confirm_unknown", "approved", "executing", "done", "rejected",
                 "expired", "declined", "halted", "unknown")
LEG_STATUSES = ("pending", "submitting", "submitted", "filled", "partial", "canceled", "rejected", "unknown",
                "failed", "skipped")
# A plan in one of these still needs work from the scheduled run.
OPEN_PLAN = ("confirm_sent", "approved", "executing", "unknown")
_SCHEMA = (
    f"""CREATE TABLE trade_plans (
        plan_id TEXT PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN {PLAN_STATUSES}),
        plan TEXT NOT NULL,
        plan_sha256 TEXT NOT NULL,
        not_before TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        confirm_message_id TEXT,
        confirm_sent_at TEXT,
        thread_id TEXT,
        approval_message_id TEXT,
        approved_at TEXT,
        session_end TEXT,
        note TEXT
    )""",
    f"""CREATE TABLE trade_legs (
        plan_id TEXT NOT NULL REFERENCES trade_plans (plan_id),
        leg INTEGER NOT NULL,
        client_order_id TEXT NOT NULL UNIQUE,
        side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        symbol TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN {LEG_STATUSES}),
        quantity TEXT,
        limit_price TEXT,
        notional_krw INTEGER,
        order_id TEXT,
        first_submit_at TEXT,
        updated_at TEXT NOT NULL,
        filled_quantity TEXT,
        average_price TEXT,
        commission TEXT,
        tax TEXT,
        settlement_date TEXT,
        error_code TEXT,
        prior_orders TEXT,
        PRIMARY KEY (plan_id, leg)
    )""",
    """CREATE TABLE trade_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        plan_id TEXT,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL
    )""",
    """CREATE TABLE trade_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
)


class StoreError(Exception):
    """The trade ledger cannot be used by this version of Smith."""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and create) the trade ledger in autocommit mode. The caller closes it."""
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreError(f"trade ledger schema {version} is newer than supported {SCHEMA_VERSION}")
        if version == 0:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        raise
    return conn


def event(conn: sqlite3.Connection, at: datetime, plan_id: str | None, kind: str, detail: str = "") -> None:
    conn.execute("INSERT INTO trade_events (at, plan_id, kind, detail) VALUES (?, ?, ?, ?)",
                 (at.isoformat(), plan_id, kind, detail))


def add_plan(conn: sqlite3.Connection, plan: dict[str, Any], *, sha256: str, now: datetime) -> str | None:
    """Store a new plan, or return the code of the plan still awaiting approval and store nothing.

    Only one plan can await approval, and a new one never replaces it: an authenticator code is not bound to a plan,
    so the client should only ever be sending a code to the one confirmation that is open.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        waiting = conn.execute("SELECT code FROM trade_plans WHERE status IN ('proposed', 'confirm_sent')").fetchone()
        if waiting is not None:
            conn.execute("ROLLBACK")
            return waiting[0]
        conn.execute("INSERT INTO trade_plans (plan_id, code, created_at, updated_at, status, plan, plan_sha256, "
                     "not_before, expires_at) VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?)",
                     (plan["plan_id"], plan["code"], now.isoformat(), now.isoformat(), json.dumps(plan, sort_keys=True),
                      sha256, plan["not_before"], plan["expires_at"]))
        for leg in plan["legs"]:
            conn.execute("INSERT INTO trade_legs (plan_id, leg, client_order_id, side, symbol, status, quantity, "
                         "updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                         (plan["plan_id"], leg["leg"], leg["client_order_id"], leg["side"], leg["symbol"],
                          leg.get("quantity"), now.isoformat()))
        event(conn, now, plan["plan_id"], "proposed", plan["code"])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return None


def move(conn: sqlite3.Connection, plan_id: str, *, expected: tuple[str, ...], status: str, now: datetime,
         note: str | None = None, **fields: str | None) -> bool:
    """Move a plan to `status` only if it is in one of `expected`; records the event. False if it was not."""
    sets = ", ".join(f"{name} = ?" for name in fields)
    cursor = conn.execute(
        f"UPDATE trade_plans SET status = ?, updated_at = ?, note = COALESCE(?, note){', ' + sets if sets else ''} "
        f"WHERE plan_id = ? AND status IN ({', '.join('?' * len(expected))})",
        (status, now.isoformat(), note, *fields.values(), plan_id, *expected))
    if cursor.rowcount:
        event(conn, now, plan_id, status, note or "")
    return bool(cursor.rowcount)


def update_leg(conn: sqlite3.Connection, plan_id: str, leg: int, *, now: datetime, expected: tuple[str, ...] | None = None,
               **fields: Any) -> bool:
    """Set leg fields (status included); with `expected`, only from those statuses. Records a status change."""
    sets = ", ".join(f"{name} = ?" for name in fields)
    where, args = "plan_id = ? AND leg = ?", [plan_id, leg]
    if expected:
        where += f" AND status IN ({', '.join('?' * len(expected))})"
        args += list(expected)
    cursor = conn.execute(f"UPDATE trade_legs SET {sets}, updated_at = ? WHERE {where}",
                          (*fields.values(), now.isoformat(), *args))
    if cursor.rowcount and "status" in fields:
        event(conn, now, plan_id, f"leg{leg}:{fields['status']}", str(fields.get("error_code") or ""))
    return bool(cursor.rowcount)


def plan(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT plan_id, code, status, plan, plan_sha256, not_before, expires_at, confirm_message_id, "
                       "confirm_sent_at, thread_id, approval_message_id, approved_at, note, created_at, session_end "
                       "FROM trade_plans WHERE plan_id = ? OR code = ?", (plan_id, plan_id)).fetchone()
    if row is None:
        return None
    keys = ("plan_id", "code", "status", "plan", "plan_sha256", "not_before", "expires_at", "confirm_message_id",
            "confirm_sent_at", "thread_id", "approval_message_id", "approved_at", "note", "created_at", "session_end")
    found = dict(zip(keys, row))
    found["plan"] = json.loads(found["plan"])
    found["legs"] = legs(conn, found["plan_id"])
    return found


def legs(conn: sqlite3.Connection, plan_id: str) -> list[dict[str, Any]]:
    keys = ("leg", "client_order_id", "side", "symbol", "status", "quantity", "limit_price", "notional_krw", "order_id",
            "first_submit_at", "filled_quantity", "average_price", "commission", "tax", "settlement_date", "error_code",
            "prior_orders")
    rows = conn.execute(f"SELECT {', '.join(keys)} FROM trade_legs WHERE plan_id = ? ORDER BY leg", (plan_id,))
    return [dict(zip(keys, row)) for row in rows]


def plans(conn: sqlite3.Connection, *, statuses: tuple[str, ...] | None = None, limit: int = 20,
          oldest_first: bool = False) -> list[dict[str, Any]]:
    where = f"WHERE status IN ({', '.join('?' * len(statuses))})" if statuses else ""
    order = "ASC" if oldest_first else "DESC"
    ids = [r[0] for r in conn.execute(f"SELECT plan_id FROM trade_plans {where} ORDER BY created_at {order} LIMIT ?",
                                      (*(statuses or ()), limit))]
    return [p for p in (plan(conn, i) for i in ids) if p is not None]


def spent_krw(conn: sqlite3.Connection, *, since: datetime, until: datetime) -> int:
    """Won sent to the broker in [since, until): legs that left Smith (submitted or later, unknown included)."""
    row = conn.execute("SELECT COALESCE(SUM(notional_krw), 0) FROM trade_legs WHERE first_submit_at >= ? "
                       "AND first_submit_at < ? AND status NOT IN ('pending', 'skipped', 'failed')",
                       (since.isoformat(), until.isoformat())).fetchone()
    return int(row[0])


def reserve_leg(conn: sqlite3.Connection, plan_id: str, leg: int, *, now: datetime, day: tuple[datetime, datetime],
                max_daily_krw: int, session_end: datetime, **fields: Any) -> str:
    """Mark a pending leg `submitting` if not halted and the day's total stays within the limit: "ok", "halted",
    "limit" or "taken".

    One transaction, so two runs cannot both pass the daily check. The plan keeps the end of the session its first
    order went into: a later leg never goes into another session.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = conn.execute("SELECT status FROM trade_plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if halted(conn) or state is None or state[0] not in ("executing", "unknown"):
            conn.execute("ROLLBACK")
            return "halted"
        if spent_krw(conn, since=day[0], until=day[1]) + int(fields["notional_krw"]) > max_daily_krw:
            conn.execute("ROLLBACK")
            return "limit"
        if not update_leg(conn, plan_id, leg, now=now, expected=("pending",), status="submitting",
                          first_submit_at=now.isoformat(), **fields):
            conn.execute("ROLLBACK")
            return "taken"
        conn.execute("UPDATE trade_plans SET session_end = COALESCE(session_end, ?) WHERE plan_id = ?",
                     (session_end.isoformat(), plan_id))
        conn.execute("COMMIT")
        return "ok"
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM trade_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO trade_state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, value))


def use_totp_step(conn: sqlite3.Connection, step: int) -> bool:
    """Claim an authenticator time step once: False if it was used before. (Each code must also come after its own
    confirmation mail, which the executor checks, so an old code cannot approve a newer plan.)"""
    try:
        conn.execute("INSERT INTO trade_state (key, value) VALUES (?, '1')", (f"totp_step:{step}",))
    except sqlite3.IntegrityError:
        return False
    return True


def approve(conn: sqlite3.Connection, plan_id: str, *, step: int, message_id: str, sent_at: datetime,
            now: datetime) -> bool:
    """Use the code's time step and move the plan confirm_sent -> approved in one transaction; False if either fails
    (the step was used, or the plan stopped awaiting approval meanwhile, e.g. a newer plan retired it)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not use_totp_step(conn, step) or not move(conn, plan_id, expected=("confirm_sent",), status="approved",
                                                     now=now, approval_message_id=message_id,
                                                     approved_at=sent_at.isoformat()):
            conn.execute("ROLLBACK")
            return False
        conn.execute("COMMIT")
        return True
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def halted(conn: sqlite3.Connection) -> bool:
    return get_state(conn, "halted") == "1"


@contextmanager
def exclusive(path: Path | str) -> Iterator[bool]:
    """One trade run at a time across processes: an OS lock on `path`, released by the OS if the process dies.

    Never taken over by age: a run that is slow (or paused) between reserving a leg and sending it keeps its lock,
    so no other run can settle that leg as never sent while it may still go out.
    """
    handle = open(path, "a+b")
    try:
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        handle.close()          # closing the handle releases the lock on both platforms


def owns_leg(conn: sqlite3.Connection, plan_id: str, leg: int, first_submit_at: str) -> bool:
    """The leg is still awaiting this send (submitting or unknown, same first send): checked right before a POST."""
    row = conn.execute("SELECT status, first_submit_at FROM trade_legs WHERE plan_id = ? AND leg = ?",
                       (plan_id, leg)).fetchone()
    return row is not None and row[0] in ("submitting", "unknown") and row[1] == first_submit_at


def seen_replies(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT detail FROM trade_events WHERE kind = 'reply'")}
