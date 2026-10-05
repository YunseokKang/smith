"""`smith doctor`: one read-only health check of everything the unattended runs depend on.

Ledger file and schema, local config, stored credentials (presence only, never values), the Windows
scheduled task, the last data syncs, research, report deliveries and e-mail answers, and recent errors in
the run log. Nothing is sent, synced or written. Exit code 1 when anything needs fixing.
"""
import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smith import ledger

TASK = r"Smith\ReportDue"
STALE_SYNC = timedelta(days=7)
_LEVELS = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}


def add_parser(sub: Any, default_db: Path) -> None:
    doctor = sub.add_parser("doctor", help="Check ledger, config, credentials, scheduler, syncs and deliveries")
    doctor.add_argument("--db", type=Path, default=default_db)
    doctor.add_argument("--config", type=Path, default=Path("config/smith.local.toml"))


def run(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    results: list[tuple[str, str, str]] = []
    config = _config(args.config, results)
    _credentials(results)
    _scheduler(results, Path(__file__).resolve().parents[2])
    _ledger(args.db, config, now, results)
    _log(args.db, now, results)
    for level, area, message in results:
        print(f"{_LEVELS[level]}  {area:<12} {message}")
    failed = sum(1 for level, _, _ in results if level == "fail")
    print(f"\n{failed} problem(s), {sum(1 for r in results if r[0] == 'warn')} warning(s).")
    return 1 if failed else 0


def _config(path: Path, results: list) -> dict[str, Any] | None:
    from smith.config import load_config

    try:
        config = load_config(path)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        results.append(("fail", "config", f"{path}: {error}"))
        return None
    mail = config.get("mail", {})
    recipient = mail.get("recipient")
    masked = None if not recipient else recipient[:2] + "***@" + recipient.split("@")[1]
    results.append(("ok", "config", f"timezone {config['app']['timezone']}, reports "
                    f"{'enabled' if config['reports']['enabled'] else 'disabled'} "
                    f"({', '.join(config['reports']['weekdays'])} {config['reports']['time']})"))
    results.append(("ok" if recipient else "fail", "mail", f"recipient {masked or 'missing'}, answer replies "
                    f"{'on' if mail.get('answer_replies') else 'off'}"))
    return config


def _credentials(results: list) -> None:
    from smith import credentials

    try:
        stored = {"gmail": credentials.load_gmail() is not None, "toss": credentials.load_toss_client() is not None,
                  **{p: credentials.load_api_key(p) is not None
                     for p in credentials.EVIDENCE_PROVIDERS + credentials.REAL_ESTATE_PROVIDERS}}
    except Exception as error:  # noqa: BLE001 - a broken credential store is itself the finding.
        results.append(("fail", "credentials", f"credential store unavailable ({type(error).__name__})"))
        return
    missing = sorted(name for name, present in stored.items() if not present)
    level = "fail" if not stored["gmail"] else ("warn" if missing else "ok")
    results.append((level, "credentials", "stored: " + ", ".join(sorted(n for n, p in stored.items() if p))
                    + (f"; missing: {', '.join(missing)}" if missing else "")))


def _scheduler(results: list, root: Path) -> None:
    if sys.platform != "win32":
        results.append(("warn", "scheduler", "not Windows: check your own scheduler runs `smith report run-due`"))
        return
    try:
        # schtasks prints localized field names (Korean, in cp949, when there is no console); the
        # ScheduledTasks module gives stable names as JSON.
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TASK_QUERY],
                             capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        results.append(("warn", "scheduler", f"could not query ({type(error).__name__})"))
        return
    if out.returncode != 0 or not out.stdout.strip():
        results.append(("fail", "scheduler", f"{TASK} not registered: run scripts\\install-schedule.ps1"))
        return
    try:
        task = json.loads(out.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        results.append(("warn", "scheduler", "unreadable task query output"))
        return
    level, notes = "ok", [f"state {task.get('State')}", f"last run {task.get('Last') or 'never'} "
                                                          f"(result {task.get('Result')})", f"next {task.get('Next') or '-'}"]
    if str(root).lower() not in str(task.get("Command", "")).lower():
        level = "warn"
        notes.append("runs code from another folder")
    if task.get("Result") not in (0, 267009, 267011):  # 0x41301 running, 0x41303 not yet run.
        level = "warn"
    if task.get("State") == "Disabled":
        level = "fail"
    results.append((level, "scheduler", ", ".join(notes)))


_TASK_QUERY = (
    "$t = Get-ScheduledTask -TaskPath '\\Smith\\' -TaskName 'ReportDue' -ErrorAction Stop; "
    "$i = $t | Get-ScheduledTaskInfo; "
    "[pscustomobject]@{State = [string]$t.State; Result = $i.LastTaskResult; "
    "Last = $(if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $i.LastRunTime.ToString('yyyy-MM-dd HH:mm') }); "
    "Next = $(if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') }); "
    "Command = (($t.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }) -join '; ')} | ConvertTo-Json -Compress"
)


def _ledger(db: Path, config: dict[str, Any] | None, now: datetime, results: list) -> None:
    if not db.exists():
        results.append(("fail", "ledger", f"no ledger at {db}"))
        return
    try:
        raw = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        with closing(raw):
            version = raw.execute("PRAGMA user_version").fetchone()[0]
            check = raw.execute("PRAGMA quick_check").fetchone()[0]
        with closing(ledger.connect_read_only(db)) as conn:
            _ledger_state(conn, config, now, results)
    except (sqlite3.Error, ledger.LedgerError) as error:
        results.append(("fail", "ledger", f"not readable ({type(error).__name__}: {error})"))
        return
    note = "" if version == ledger.SCHEMA_VERSION else f" (file is v{version}; migrated on the next write)"
    results.insert(0, ("ok" if check == "ok" else "fail", "ledger", f"schema v{ledger.SCHEMA_VERSION}{note}, "
                                                                     f"integrity {check}"))


def _ledger_state(conn: sqlite3.Connection, config: dict[str, Any] | None, now: datetime, results: list) -> None:
    for source, entry in sorted(ledger.sync_status(conn, known_at=now).items()):
        success, failure = entry["last_success"], entry["last_failure"]
        failing = failure is not None and (success is None or failure > success)
        stale = success is None or now - success > STALE_SYNC
        results.append(("warn" if failing or stale else "ok", f"sync:{source}",
                        f"last success {_ago(now, success)}" + (f", last failure {_ago(now, failure)} "
                                                                f"({entry['last_error']})" if failing else "")))
    research = ledger.latest_research(conn, since=now - timedelta(days=14))
    results.append(("ok" if research else "warn", "research",
                    f"last brief {_ago(now, research['created_at'])}, {len(research['brief']['items'])} item(s)"
                    if research else "no successful brief in 14 days"))
    runs = ledger.report_runs(conn, limit=50)
    sent = [r for r in runs if r["status"] == "sent"]
    last = sent[0] if sent else None
    after = [r for r in runs if last is None or r["created_at"] > last["created_at"]]
    trouble = Counter(r["status"] for r in after if r["status"] in ("failed", "unknown", "building", "sending"))
    message = f"last sent {_ago(now, datetime.fromisoformat(last['created_at'])) if last else 'never'}"
    if trouble:
        message += ", since then " + ", ".join(f"{n} {s}" for s, n in sorted(trouble.items()))
    if config is not None and config["reports"]["enabled"]:
        from smith import delivery
        upcoming = delivery.slots_between(config["reports"], config["app"]["timezone"], now, now + timedelta(days=8))
        if upcoming:
            message += f", next slot {upcoming[0]:%m-%d %H:%M}"
    results.append(("warn" if trouble else "ok", "reports", message))
    rows = conn.execute("SELECT status FROM mail_questions WHERE claimed_at >= ?",
                        ((now - timedelta(days=30)).isoformat(),)).fetchall()
    counts = Counter(row[0] for row in rows)
    results.append(("warn" if counts.get("unknown") or counts.get("failed") else "ok", "mail answers",
                    "30 days: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no questions")))


def _log(db: Path, now: datetime, results: list) -> None:
    path = db.parent / "report-runs.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
    except OSError:
        results.append(("warn", "run log", f"no log at {path} (the scheduler has not run yet?)"))
        return
    since = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    errors = [line for line in lines if line[:20] >= since and ("crashed" in line or "failed" in line)]
    results.append(("warn" if errors else "ok", "run log",
                    f"{len(errors)} error line(s) in 3 days" + (f"; latest: {errors[-1][21:120]}" if errors else "")))


def _ago(now: datetime, moment: datetime | None) -> str:
    if moment is None:
        return "never"
    minutes = int((now - moment).total_seconds() // 60)
    return f"{minutes} min ago" if minutes < 120 else (f"{minutes // 60} h ago" if minutes < 2880 else f"{minutes // 1440} d ago")
