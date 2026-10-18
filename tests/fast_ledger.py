"""Test helper: on-disk ledgers without an fsync on every commit.

The ledger opens SQLite with its default durability (synchronous FULL), which on Windows costs 10-15 ms per
committed write; tests that publish reports through a file ledger spent most of their time there and pushed
the suite past its budget (AGENTS test cost budget, review 2026-10-18). Production code is unchanged: only a
test module that calls `install()` from `setUpModule` gets connections with `synchronous = OFF`, which keeps
every result and only drops the crash-safety the tests never exercise.
"""
import sqlite3
from unittest import mock

_REAL_CONNECT = sqlite3.connect
_patcher = mock.patch("sqlite3.connect")


def _connect(*args, **kwargs):
    conn = _REAL_CONNECT(*args, **kwargs)
    conn.execute("PRAGMA synchronous = OFF")
    return conn


def install() -> None:
    _patcher.start().side_effect = _connect


def uninstall() -> None:
    _patcher.stop()
