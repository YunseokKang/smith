"""Restaurant picks for the places in tomorrow's calendar, in the evening 내일 준비 mail (FR-55; client 2026-11-16:
"다음 날 일정이나 할 일에 특정 장소가 있으면 내일 일과 메일에 자동으로 맛집 추천").

A place is found in code, never by a model: an event's location or title, or a due task's title, naming a public
place (`places.find`: a known area or venue, or any 읍·면·동, 구·군, 시 or railway station in the country). Only that
name goes to Naver ("광화문 맛집"): a location's own words never do (a location may hold a name or a private
appointment; review 2026-11-16, P0), and the title stays in the mail. At most `MAX_OUTINGS` areas an evening, each read
at most once in `REUSE_WITHIN` under the same food rules and visit log (a commute area does not cost a read every night;
`[dining] skip_areas` removes it altogether).
"""
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from smith import agenda, aside, contacts, dining_command, dining_log, places

logger = logging.getLogger(__name__)

VERSION = "dining-v3"            # Part of the cache key: another shape of result is read again.
MAX_OUTINGS = 2
REUSE_WITHIN = timedelta(hours=48)


@dataclass(frozen=True)
class Outing:
    area: str          # What Naver is asked about: "<area> 맛집".
    when: str          # "19:00", "종일" or "할 일".
    title: str         # The calendar entry it came from (shown in the mail, never sent to Naver).


def settings(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("dining") or {}
    return {"evening": bool(section.get("evening", True)), "google": bool(section.get("google", True)),
            "skip_areas": tuple(str(a) for a in section.get("skip_areas", []))}


def outings(events: list[agenda.Event], due_tasks: list[Any], *, skip: tuple[str, ...] = (),
            people: frozenset[str] = frozenset()) -> list[Outing]:
    """Tomorrow's places in time order (all-day first, as the agenda lists them), tasks after; one per area. `people`:
    the address book's names, never read as a place ("서현 근처 카페" with a friend 김서현)."""
    found: list[Outing] = []
    for event in events:
        area = places.find(event.location or "", people=people) or places.find(event.title, people=people)
        when = "종일" if event.all_day else f"{event.start:%H:%M}"
        found += [Outing(area=area, when=when, title=event.title)] if area else []
    for task in due_tasks:
        area = places.find(task.title, people=people)
        found += [Outing(area=area, when="할 일", title=task.title)] if area else []
    unique: dict[str, Outing] = {}
    for outing in found:
        if outing.area not in unique and not any(s and s in outing.area for s in skip):
            unique[outing.area] = outing
    return list(unique.values())[:MAX_OUTINGS]


Searcher = Callable[[Path, str], dict[str, Any]]


def step(db: Path, config: dict[str, Any], data: dict[str, Any], now: datetime, *, searcher: Searcher | None = None) -> None:
    """Fill data["dining"]: per outing {"area", "when", "title", "result" (the dict form) or None, "error"}. A failed
    read is named in data["failures"]["dining"] and stops the reads after it (Aside or the household files are down for
    all). The picks are optional: a failure here never fails the mail (review 2026-11-16, P1)."""
    if not settings(config)["evening"]:
        return
    people = frozenset(w for c in contacts.load(contacts.path(db)) for w in contacts.blank_words(c))   # Never raises.
    wanted = outings(data["events"], data["tasks"]["due_today"], skip=settings(config)["skip_areas"], people=people)
    data["dining"] = []
    stopped: str | None = None
    for outing in wanted:
        entry = {"area": outing.area, "when": outing.when, "title": outing.title, "result": None, "error": stopped}
        if stopped is None:
            try:
                entry["result"], _ = cached_search(db, outing.area, now=now, use_google=settings(config)["google"],
                                                   searcher=searcher)
            except (aside.AsideError, dining_command.DiningUnavailable) as error:
                entry["error"] = stopped = error.code
            except (OSError, sqlite3.Error, ValueError) as error:   # A cache or log file that cannot be written.
                entry["error"] = stopped = f"dining-{type(error).__name__}"
        data["dining"].append(entry)
    if stopped:
        data["failures"]["dining"] = stopped[:40]


def cached_search(db: Path, area: str, *, now: datetime, use_google: bool = True,
                  searcher: Searcher | None = None) -> tuple[dict[str, Any], bool]:
    """The picks for "<area> 맛집": the stored ones when read within REUSE_WITHIN under the same food rules and visit
    log (the household is read first even on a hit, so a broken health ledger stops the picks; review 2026-11-16, P1),
    else a new read stored for later in `data/dining/` (not the shared research table). Returns (result, fresh)."""
    _, _, fingerprint = dining_command.household(db)
    where = dining_log.folder(db)
    key = json.dumps([VERSION, area, fingerprint], ensure_ascii=False)
    found = dining_log.cached(where, key, now=now, within=REUSE_WITHIN)
    if found is not None:
        return found, False
    result = (searcher(db, f"{area} 맛집") if searcher is not None
              else dining_command.search(db, f"{area} 맛집", now=now, use_google=use_google))
    dining_log.store(where, key, result, now=now)
    logger.info("evening dining read: %s picks", len(result.get("picks") or []))
    return result, True


def for_prose(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """What the reply answerer may read about tonight's picks: names, dishes, links (public shop data only)."""
    return [{"area": e["area"], "when": e["when"], "picks": [
        {"order": n, "name": p["name"], "category": p["category"], "signature": [m["name"] for m in p["signature"]],
         "avoid": p["avoid"], "link": p["link"]} for n, p in enumerate((e.get("result") or {}).get("picks") or [], 1)]}
        for e in entries or []]
