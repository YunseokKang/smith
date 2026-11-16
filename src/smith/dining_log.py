"""The household's own restaurant record (FR-55): places the couple went to with their verdict, the Google look-ups
spent this month, and the evening mail's picks per area. Private by the ledger's rule: `data/dining/` next to
`data/smith.db`.

A verdict of 1 or 2 keeps the place out of later picks; 4 or 5 is shown beside it. The Google count keeps the
cross-check inside the free monthly allowance (`google_places.MONTHLY_CAP`): the check and the count happen under one
lock file, so two runs at once cannot both take the last call.
"""
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smith import naver_place

VISITS_FILE = "visits.json"
USAGE_FILE = "google_usage.json"
USAGE_LOCK = "google_usage.lock"
VISITS_LOCK = "visits.lock"
CACHE_FILE = "evening_cache.json"
CACHE_KEEP = timedelta(days=7)
LOCK_WAIT = 2.0                # Seconds to wait for another run's lock before giving up on the Google check.
STALE_LOCK = 60.0              # A lock older than this was left by a crashed run.
MAX_NOTE = 200
MAX_VISITS = 2000


class DiningLogError(Exception):
    """A record file that cannot be read as written: callers stop instead of treating it as empty."""


@dataclass(frozen=True)
class Visit:
    name: str
    on: date
    rating: int                   # 1..5, the couple's verdict.
    place_id: str | None = None   # The Naver place ID when the client gave a link.
    note: str = ""


def folder(db: Path) -> Path:
    return Path(db).parent / "dining"


def load_visits(where: Path) -> list[Visit]:
    """Every visit, oldest first; an absent file is none, a broken one raises DiningLogError."""
    raw = _read(where / VISITS_FILE, [])
    if not isinstance(raw, list):
        raise DiningLogError("visits-not-a-list")
    visits = []
    for item in raw:
        try:
            visit = Visit(name=str(item["name"])[:80], on=date.fromisoformat(item["on"]), rating=int(item["rating"]),
                          place_id=naver_place.place_id(str(item.get("place_id") or "")), note=str(item.get("note") or "")[:MAX_NOTE])
        except (KeyError, TypeError, ValueError) as error:
            raise DiningLogError(f"visit-unreadable: {type(error).__name__}") from None
        if not visit.name.strip() or not 1 <= visit.rating <= 5:
            raise DiningLogError("visit-out-of-range")
        visits.append(visit)
    return visits


def add_visit(where: Path, visit: Visit) -> None:
    """Append one visit: read, add and replace under the lock file, so two runs at once both keep theirs (a lost 1-point
    verdict would keep a place in the picks; review 2026-11-16, round 5). Raises DiningLogError or OSError."""
    if not visit.name.strip() or not 1 <= visit.rating <= 5:
        raise ValueError("a visit needs a name and a rating from 1 to 5")
    with _locked(where / VISITS_LOCK):
        visits = load_visits(where)[-(MAX_VISITS - 1):] + [visit]
        _write(where / VISITS_FILE, [{"name": v.name, "on": v.on.isoformat(), "rating": v.rating, "place_id": v.place_id,
                                      "note": v.note} for v in visits])


def latest_visit(visits: list[Visit], place: naver_place.Place) -> Visit | None:
    """The newest visit to `place`: by Naver ID when both have one, else by name (a branch suffix may differ)."""
    found = [v for v in visits if (v.place_id == place.id) if v.place_id] or \
            [v for v in visits if v.place_id is None and naver_place.same_name(v.name, place.name)]
    return max(found, key=lambda v: v.on) if found else None


def google_used(where: Path, month: str) -> int:
    raw = _read(where / USAGE_FILE, {})
    value = raw.get(month, 0) if isinstance(raw, dict) else 0
    return value if isinstance(value, int) and value >= 0 else 0


def reserve_google(where: Path, month: str, cap: int) -> bool:
    """Take one Google call of this month's allowance: False when `cap` is reached. The check and the count happen
    under the lock file (earlier months are dropped). Raises DiningLogError (locked too long, unreadable) or OSError."""
    with _locked(where / USAGE_LOCK):
        used = google_used(where, month)
        if used >= cap:
            return False
        _write(where / USAGE_FILE, {month: used + 1})
        return True


def cached(where: Path, key: str, *, now: datetime, within: timedelta) -> dict[str, Any] | None:
    """The evening picks stored under `key` within `within` of `now`, or None. An unreadable cache is a miss."""
    try:
        entry = (_read(where / CACHE_FILE, {}) or {}).get(key)
        at = datetime.fromisoformat(entry["at"]) if isinstance(entry, dict) else None
    except (DiningLogError, AttributeError, KeyError, TypeError, ValueError):
        return None
    return entry["result"] if at is not None and now - at <= within and isinstance(entry.get("result"), dict) else None


def store(where: Path, key: str, result: dict[str, Any], *, now: datetime) -> None:
    """Keep `result` under `key`; entries older than CACHE_KEEP go. Raises OSError when the file cannot be written."""
    try:
        raw = _read(where / CACHE_FILE, {})
    except DiningLogError:
        raw = {}
    kept = {k: v for k, v in (raw.items() if isinstance(raw, dict) else [])
            if isinstance(v, dict) and _recent(v.get("at"), now)}
    _write(where / CACHE_FILE, {**kept, key: {"at": now.isoformat(), "result": result}})


def _recent(stamp: Any, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(str(stamp)) <= CACHE_KEEP
    except (TypeError, ValueError):
        return False


@contextmanager
def _locked(lock: Path) -> Iterator[None]:
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise DiningLogError(f"{lock.stem}-locked") from None
            time.sleep(0.01)
    try:
        yield
    finally:
        os.close(handle)
        lock.unlink(missing_ok=True)


def _read(file: Path, empty: Any) -> Any:
    if not file.exists():
        return empty
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiningLogError(f"unreadable {file.name}: {type(error).__name__}") from None


def _write(file: Path, value: Any) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=file.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=1)
        os.replace(temp, file)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
