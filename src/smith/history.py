"""역사의 오늘 (user request 2026-10-25): the events listed for this calendar day on Korean Wikipedia, one of which the
writer turns into a one- or two-line aside at the top of the mail, just for fun.

The day page ("10월 25일") is fetched through the MediaWiki API as wikitext and its 사건 section parsed; the list is cached
per month-day for REFRESH_DAYS because it hardly changes. The writer may only choose a year from this list (code checks),
and the mail links the page as the source. Without a writer the code picks one itself, preferring a Korean event.
"""
import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smith import gmail

API = "https://ko.wikipedia.org/w/api.php"
USER_AGENT = "Smith/0.1 (personal daily mail)"
REFRESH_DAYS = 90
MAX_EVENTS = 40
KOREA = re.compile(r"한국|조선|대한|고려|신라|백제|고구려|서울|독도|한반도|임진|명량|이순신|세종|한글")
_REF = re.compile(r"<ref[^>]*?/>|<ref[^>]*>.*?</ref>", re.S)
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]")
_YEAR = re.compile(r"^\*\s*\[\[(\d{1,4})년\]\]\s*(?:[-–—:]\s*(.*))?$")


@dataclass(frozen=True)
class Event:
    year: int
    text: str


def path(db: Path) -> Path:
    return Path(db).parent / "history.json"


def page_url(day: date) -> str:
    return "https://ko.wikipedia.org/wiki/" + urllib.parse.quote(f"{day.month}월_{day.day}일")


def events_for(day: date, *, cache: Path, now: datetime, getter: gmail.Getter = gmail.get) -> list[Event]:
    """The day's events, from the cache when fresh. Raises gmail.MailError-free: a failed fetch raises OSError."""
    key = f"{day.month:02d}-{day.day:02d}"
    stored = _read(cache)
    entry = stored.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("fetched_at"), str) and \
            datetime.fromisoformat(entry["fetched_at"]) > now - timedelta(days=REFRESH_DAYS):
        return [Event(int(e["year"]), str(e["text"])) for e in entry.get("events", [])]
    query = urllib.parse.urlencode({"action": "parse", "page": f"{day.month}월 {day.day}일", "prop": "wikitext",
                                    "format": "json", "formatversion": "2"})
    status, body = getter(f"{API}?{query}", {"User-Agent": USER_AGENT})
    wikitext = (body.get("parse") or {}).get("wikitext") if status == 200 else None
    if not isinstance(wikitext, str):
        if isinstance(entry, dict):
            return [Event(int(e["year"]), str(e["text"])) for e in entry.get("events", [])]
        raise OSError(f"wikipedia-{status}")
    events = parse(wikitext)
    stored[key] = {"fetched_at": now.isoformat(), "events": [{"year": e.year, "text": e.text} for e in events]}
    _write(cache, stored)
    return events


def parse(wikitext: str) -> list[Event]:
    """Events of the 사건 section: `* [[1597년]] - 명량 해전 발발.` lines, and `**` sub-items under a bare year line."""
    match = re.search(r"==\s*사건\s*==\s*\n(.*?)(?=\n==[^=]|\Z)", wikitext, re.S)
    if not match:
        return []
    events: list[Event] = []
    year: int | None = None
    for line in match.group(1).splitlines():
        line = line.strip()
        if line.startswith("**") and year is not None:
            text = clean(line.lstrip("*").strip())
            if text:
                events.append(Event(year, text))
            continue
        found = _YEAR.match(line)
        if not found:
            continue
        year = int(found.group(1))
        text = clean(found.group(2) or "")
        if text:
            events.append(Event(year, text))
    return events[:MAX_EVENTS]


def clean(text: str) -> str:
    text = _REF.sub("", text)
    for _ in range(3):
        text = _TEMPLATE.sub("", text)
    text = _LINK.sub(r"\1", text).replace("'''", "").replace("''", "")
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.split()).strip(" .;") + ("." if text.strip() else "")


def pick(events: list[Event], day: date) -> Event | None:
    """The code's own choice when no writer runs: a Korean event when there is one, otherwise one by the year's seed."""
    if not events:
        return None
    korean = [e for e in events if KOREA.search(e.text)]
    pool = korean or events
    return pool[day.year % len(pool)]


def for_model(events: list[Event]) -> list[dict[str, Any]]:
    return [{"year": e.year, "event": e.text[:200]} for e in events]


def _read(cache: Path) -> dict[str, Any]:
    try:
        data = json.loads(cache.read_text(encoding="utf-8-sig")) if cache.exists() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(cache: Path, data: dict[str, Any]) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    temp = cache.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, cache)
