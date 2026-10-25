"""Google Drive, read-only, for one purpose: the Takeout exports Google drops into Drive on a schedule (daily design §13).

Verified against Google's docs on 2026-10-25: listing is `GET drive/v3/files?q=...` and a download is
`GET drive/v3/files/{id}?alt=media`. The scope `drive.readonly` is broad by Google's design (it can read any file), so
the code narrows what Smith touches: only files named like a Takeout archive (`takeout-<stamp>-<part>.zip`), listed by
name, never browsed by folder, and downloaded to a size cap. Every call is a plain GET with the bearer token.
"""
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smith import gmail
from smith.net import open_url

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024   # A Keep-only Takeout is a few megabytes; anything near this is not it.
_TIMEOUT_SECONDS = 120
# Takeout names an export's index archive `takeout-<stamp>-001.zip` and its data archives `takeout-<stamp>-<batch>-<part>.zip`
# (seen 2026-10-25: `-1-001` for Keep); all of one export share the stamp.
_NAME = re.compile(r"^takeout-(?P<stamp>\d{8}T\d{6}Z)(?:-(?P<batch>\d+))?-(?P<part>\d{3})\.zip$")
Downloader = Callable[[str, str, Path], int]   # (token, file_id, target) -> bytes written


class DriveError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Archive:
    id: str
    name: str
    stamp: str      # The export's timestamp: parts of one export share it.
    part: int
    size: int | None
    created: str
    batch: int = 0  # 0 for the index archive, 1+ for the data archives.


def has_scope(granted: set[str]) -> bool:
    return SCOPE in granted


def takeout_archives(token: str, *, getter: gmail.Getter = gmail.get, now: datetime | None = None) -> list[Archive]:
    """Every Takeout zip in Drive that Google itself put there, newest export first (parts in order).

    Provenance is checked, not assumed (review 2026-10-25): the file is owned by the account, sits in a `Takeout`
    folder the account owns, is a zip by MIME type, and its name stamp lies within two days of its creation time
    and not in the future — so a shared or renamed file cannot pose as the newest export.
    """
    now = now or datetime.now(timezone.utc)
    folders = _list(token, "name = 'Takeout' and mimeType = 'application/vnd.google-apps.folder' and 'me' in owners "
                           "and trashed = false", "files(id)", getter)
    folder_ids = {str(f.get("id")) for f in folders if isinstance(f, dict) and f.get("id")}
    if not folder_ids:
        return []
    files = _list(token, "name contains 'takeout-' and name contains '.zip' and trashed = false and 'me' in owners",
                  "files(id,name,size,createdTime,mimeType,parents,ownedByMe)", getter)
    found = []
    for item in files:
        if not isinstance(item, dict):
            continue
        match = _NAME.match(str(item.get("name", "")))
        file_id = str(item.get("id", ""))
        if not match or not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", file_id):
            continue
        if item.get("ownedByMe") is not True or "zip" not in str(item.get("mimeType", "")).lower():
            continue
        if not folder_ids & {str(p) for p in item.get("parents", []) if p}:
            continue
        created = _time(str(item.get("createdTime", "")))
        stamped = _time(match.group("stamp"))
        if created is None or stamped is None or created > now + timedelta(days=1) or abs((created - stamped).days) > 2:
            continue
        size = item.get("size")
        found.append(Archive(file_id, str(item["name"]), match.group("stamp"), int(match.group("part")),
                             int(size) if str(size).isdigit() else None, str(item.get("createdTime", "")),
                             int(match.group("batch") or 0)))
    return sorted(found, key=lambda a: (a.stamp, -a.batch, -a.part), reverse=True)


def _list(token: str, query: str, fields: str, getter: gmail.Getter) -> list[Any]:
    params = {"q": query, "orderBy": "createdTime desc", "pageSize": "100", "spaces": "drive", "fields": fields}
    try:
        status, body = getter(f"{FILES_URL}?{urllib.parse.urlencode(params)}", {"Authorization": f"Bearer {token}"})
    except OSError:
        raise DriveError("network-error") from None
    if status == 401:
        raise DriveError("login-required")
    if status == 403:
        reasons = {str(e.get("reason", "")) for e in (body.get("error") or {}).get("errors", []) if isinstance(e, dict)}
        raise DriveError("api-not-enabled" if "accessNotConfigured" in reasons else "no-scope")
    if status != 200:
        raise DriveError(f"http-{status}")
    files = body.get("files", [])
    return files if isinstance(files, list) else []


def _time(text: str) -> datetime | None:
    """RFC 3339 ("2026-10-25T06:48:54.430Z") or a Takeout stamp ("20261025T064834Z") as an aware datetime."""
    try:
        if re.fullmatch(r"\d{8}T\d{6}Z", text):
            return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def newest_export(archives: list[Archive]) -> list[Archive]:
    """All archives of the most recent export, index first, then data batches in order."""
    if not archives:
        return []
    stamp = max(a.stamp for a in archives)
    return sorted((a for a in archives if a.stamp == stamp), key=lambda a: (a.batch, a.part))


def download(token: str, file_id: str, target: Path) -> int:
    """Stream one file to `target`. Raises DriveError (too-large, http-<status>, network-error)."""
    request = urllib.request.Request(f"{FILES_URL}/{urllib.parse.quote(file_id)}?alt=media",
                                     headers={"Authorization": f"Bearer {token}"}, method="GET")
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with open_url(request, timeout=_TIMEOUT_SECONDS) as response, target.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise DriveError("too-large")
                handle.write(chunk)
    except urllib.error.HTTPError as error:
        target.unlink(missing_ok=True)
        raise DriveError(f"http-{error.code}") from None
    except OSError:
        target.unlink(missing_ok=True)
        raise DriveError("network-error") from None
    except DriveError:
        target.unlink(missing_ok=True)
        raise
    return written
