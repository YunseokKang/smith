"""The Aside browser CLI: find `aside.exe` and run one fixed script with `aside repl` (Evernote, Naver Place).

Smith never uses Aside's own agent. Each caller writes a fixed read-only script whose results come back as one JSON
record per printed line behind a caller-chosen marker, so page text can never be mistaken for a record.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

Runner = Callable[[list[str], float], tuple[int, str]]


class AsideError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def find_executable() -> Path:
    """The Aside CLI executable (a native .exe, never a .cmd wrapper: TS-010). Raises AsideError."""
    local = os.environ.get("LOCALAPPDATA")
    candidates = [Path(local) / "Aside" / "CLI" / "current" / "aside.exe"] if local else []
    found = shutil.which("aside")
    candidates += [Path(found)] if found else []
    for path in candidates:
        if path.is_file() and path.suffix.lower() == ".exe":
            return path
    raise AsideError("aside-not-found")


def run(args: list[str], timeout: float) -> tuple[int, str]:
    """Run the Aside CLI; returns (exit code, stdout). A timeout returns -1 with what was printed so far."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags, check=False)
        return done.returncode, done.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as expired:
        out = expired.stdout or b""
        return -1, out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)


def records(script: str, args: dict[str, Any], *, mark: str, runner: Runner, executable: Path,
            timeout: float) -> list[dict[str, Any]]:
    """Run one script (its `__ARGS__` replaced by `args` as JSON); returns the records printed behind `mark`.

    Raises AsideError with the script's own error code when it reported one, `aside-timeout` or `aside-failed` when
    nothing usable came back.
    """
    code = script.replace("__ARGS__", json.dumps(args, ensure_ascii=False))
    status, out = runner([str(executable), "repl", code], timeout)
    found = []
    for line in out.splitlines():
        if line.startswith(mark):
            try:
                record = json.loads(line[len(mark):])
            except ValueError:
                continue
            if isinstance(record, dict):
                found.append(record)
    errors = [r for r in found if r.get("kind") == "error"]
    if errors:
        raise AsideError(str(errors[0].get("code", "script-error"))[:40])
    if not found:
        raise AsideError("aside-timeout" if status == -1 else "aside-failed")
    return found
