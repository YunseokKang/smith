"""`smith backup`: a consistent, verified copy of the ledger.

The copy is taken with SQLite's online backup API from a read-only connection, so it is consistent even
while a scheduled run writes, and it never changes the ledger. Each copy is written under a temporary
name, checked with `PRAGMA integrity_check`, and only then given its final name; a copy that fails the
check is deleted.
The client memory (`data/memory/*.md`, FR-19) is copied with it as `smith-<UTC time>.memory/`.
Old copies made by this command (named `smith-<UTC time>.db` and `.memory`) beyond `keep` are removed;
other files in the folder are never touched.

Backups hold the same real financial data as the ledger: keep them on a drive you control (an external
disk is a good second place) and out of Git and shared cloud folders unless you decide otherwise.
With `[backup] dir` in the local config, `smith report run-due` also makes a copy after each sent report.
"""
import argparse
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith import memory

DEFAULT_DIR = Path("data/backups")
DEFAULT_KEEP = 14
_NAME = re.compile(r"smith-\d{8}T\d{6}Z\.db")


class BackupError(Exception):
    """The copy could not be made or failed its integrity check."""


def add_parser(sub: Any, default_db: Path) -> None:
    backup = sub.add_parser("backup", help="Make a verified copy of the ledger (never changes the ledger)")
    backup.add_argument("--db", type=Path, default=default_db)
    backup.add_argument("--config", type=Path, default=Path("config/smith.local.toml"),
                        help="Local config whose [backup] dir and keep are used when --out/--keep are not given")
    backup.add_argument("--out", type=Path, help=f"Folder for the copies (default: [backup] dir or {DEFAULT_DIR})")
    backup.add_argument("--keep", type=int, help=f"Copies to keep (default: [backup] keep or {DEFAULT_KEEP})")


def run(args: argparse.Namespace) -> int:
    settings = _settings(args.config)
    out = args.out or (Path(settings["dir"]) if settings.get("dir") else DEFAULT_DIR)
    keep = args.keep if args.keep is not None else settings.get("keep", DEFAULT_KEEP)
    if keep < 1:
        print("--keep must be at least 1.")
        return 2
    if not args.db.exists():
        print(f"No ledger at {args.db}.")
        return 1
    try:
        path, version, removed = backup(args.db, out, now=datetime.now(timezone.utc), keep=keep)
    except BackupError as error:
        print(f"Backup failed: {error}. The ledger itself was not changed.")
        return 1
    kept = ", with client memory" if path.with_suffix(".memory").is_dir() else ""
    print(f"Backup written: {path} (schema v{version}, integrity ok, {path.stat().st_size:,} bytes{kept})"
          + (f"; removed {removed} older cop{'y' if removed == 1 else 'ies'}" if removed else ""))
    return 0


def backup(db: Path, out_dir: Path, *, now: datetime, keep: int) -> tuple[Path, int, int]:
    """Copy `db` into `out_dir` and verify it. Returns (copy path, schema version, old copies removed).

    Raises:
        BackupError: the source could not be read, or the copy failed its integrity check.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"smith-{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    partial = target.with_name(target.name + ".partial")
    try:
        source = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        with closing(source), closing(sqlite3.connect(partial)) as copy:
            source.backup(copy)
            check = copy.execute("PRAGMA integrity_check").fetchone()[0]
            version = copy.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as error:
        partial.unlink(missing_ok=True)
        raise BackupError(f"copy failed ({type(error).__name__})") from None
    if check != "ok":
        partial.unlink(missing_ok=True)
        raise BackupError("the copy failed its integrity check")
    partial.replace(target)
    notes = memory.directory(db)
    if notes.is_dir():
        try:
            shutil.copytree(notes, target.with_suffix(".memory"))
        except OSError as error:
            raise BackupError(f"the ledger was copied but the client memory was not ({type(error).__name__})") from None
    return target, version, _prune(out_dir, keep)


def _prune(out_dir: Path, keep: int) -> int:
    """Remove the oldest copies made by this command beyond `keep`, with their memory copies. Names sort by
    time."""
    copies = sorted(p for p in out_dir.iterdir() if p.is_file() and _NAME.fullmatch(p.name))
    old = copies[:-keep]
    for path in old:
        path.unlink()
        if path.with_suffix(".memory").is_dir():
            shutil.rmtree(path.with_suffix(".memory"))
    return len(old)


def _settings(path: Path) -> dict[str, Any]:
    """[backup] from the local config; an absent or unreadable config means defaults."""
    from smith.config import load_config

    try:
        return load_config(path).get("backup", {})
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
