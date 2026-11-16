"""Restaurant picks for the places in tomorrow's calendar, in the evening 내일 준비 mail (FR-55; client 2026-11-16:
"다음 날 일정이나 할 일에 특정 장소가 있으면 내일 일과 메일에 자동으로 맛집 추천").

A place is found in code, never by a model: an event's location or title, or a due task's title, naming a known area
or venue (`AREAS`: Seoul's neighbourhoods, concert halls, stadiums, day-trip parks). Only that listed name goes to
Naver ("광화문 맛집"): a location's own words never do (a location may hold a name or a private appointment; review
2026-11-16, P0), and the title stays in the mail. At most `MAX_OUTINGS` areas an evening, each read at most once in
`REUSE_WITHIN` under the same food rules and visit log (a commute area does not cost a read every night;
`[dining] skip_areas` removes it altogether).
"""
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from smith import agenda, aside, dining_command, dining_log

logger = logging.getLogger(__name__)

VERSION = "dining-v3"            # Part of the cache key: another shape of result is read again.
MAX_OUTINGS = 2
REUSE_WITHIN = timedelta(hours=48)
# Areas and venues a calendar entry may name. Longer names first where one contains another (홍대입구 before 홍대).
# Left out on purpose: names that are also everyday words or given names (미사, 수지, 서현, 교대, 전주, 수유, 상수).
AREAS = (
    # Seoul: neighbourhoods and streets people meet in.
    "광화문", "시청", "종로", "종각", "을지로", "명동", "남대문", "동대문", "충무로", "서촌", "북촌", "삼청동", "인사동",
    "익선동", "안국", "경복궁", "대학로", "혜화", "성북동", "서울숲", "성수", "뚝섬", "건대입구", "건대", "왕십리", "금호동",
    "옥수", "한남동", "한남", "이태원", "해방촌", "경리단길", "용리단길", "삼각지", "신용산", "용산", "남산", "후암동",
    "여의도", "영등포", "문래", "당산", "목동", "공덕", "마포", "홍대입구", "홍대", "합정", "망원", "연남동", "연남",
    "신촌", "이대", "서대문", "강남역", "강남", "역삼", "선릉", "삼성동", "봉은사", "가로수길", "신사", "압구정로데오",
    "압구정", "청담", "신논현", "논현", "서초", "방배", "서래마을", "반포", "고속터미널", "사당", "송리단길",
    "석촌호수", "석촌", "잠실", "방이동", "올림픽공원", "천호", "노원", "창동", "상암", "마곡", "발산", "김포공항",
    "구로", "가산", "신림", "샤로수길", "서울대입구", "낙성대", "노량진", "흑석", "이촌", "청량리", "회기",
    "광장시장", "통인시장", "망원시장", "경동시장",
    # Venues.
    "예술의전당", "세종문화회관", "블루스퀘어", "샤롯데씨어터", "디큐브아트센터", "충무아트센터", "국립극장", "국립중앙박물관",
    "국립현대미술관", "리움", "고척스카이돔", "고척돔", "잠실야구장", "잠실종합운동장", "서울월드컵경기장", "월드컵경기장",
    "장충체육관", "동대문디자인플라자", "DDP", "코엑스", "더현대 서울", "더현대서울", "롯데월드몰", "롯데월드", "타임스퀘어",
    "킨텍스", "스타필드 하남", "스타필드 고양", "스타필드 수원", "서울대공원", "서울랜드",
    # Near Seoul and day trips.
    "판교", "분당", "정자동", "광교", "행궁동", "수원", "일산", "하남", "과천", "백운호수", "송도",
    "영종도", "인천공항", "에버랜드", "캐리비안베이", "한국민속촌", "남이섬", "가평", "춘천", "양평", "헤이리", "파주",
    "김포", "부천", "평촌", "안양", "동탄", "강릉", "속초", "양양", "해운대", "광안리", "부산", "서귀포", "제주", "경주",
    "여수",
)
# One pattern for all areas, a name standing as a word of its own (a Hangul letter right before it or after its
# suffix means another word). At one position the alternatives are tried in AREAS order, so 강남역 wins over 강남, and
# 잠실 backtracks to 잠실야구장 when a letter follows.
_AREA = re.compile(r"(?<![가-힣])(" + "|".join(re.escape(name) for name in AREAS) + r")(?:역|동|점|에서|에|쪽|앞|근처)?(?![가-힣])",
                   re.IGNORECASE)
_CANONICAL = {name.lower(): name for name in AREAS}


@dataclass(frozen=True)
class Outing:
    area: str          # What Naver is asked about: "<area> 맛집".
    when: str          # "19:00", "종일" or "할 일".
    title: str         # The calendar entry it came from (shown in the mail, never sent to Naver).


def settings(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("dining") or {}
    return {"evening": bool(section.get("evening", True)), "google": bool(section.get("google", True)),
            "skip_areas": tuple(str(a) for a in section.get("skip_areas", []))}


def area_in(text: str) -> str | None:
    """The first known area named in `text` as a word of its own: "광화문 보라쇼" and "성수동 약속" match, "신사업
    회의" does not."""
    match = _AREA.search(text)
    return None if match is None else _CANONICAL.get(match.group(1).lower(), match.group(1))


def outings(events: list[agenda.Event], due_tasks: list[Any], *, skip: tuple[str, ...] = ()) -> list[Outing]:
    """Tomorrow's places in time order (all-day first, as the agenda lists them), tasks after; one per area."""
    found: list[Outing] = []
    for event in events:
        area = area_in(event.location or "") or area_in(event.title)
        when = "종일" if event.all_day else f"{event.start:%H:%M}"
        found += [Outing(area=area, when=when, title=event.title)] if area else []
    for task in due_tasks:
        area = area_in(task.title)
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
    wanted = outings(data["events"], data["tasks"]["due_today"], skip=settings(config)["skip_areas"])
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
