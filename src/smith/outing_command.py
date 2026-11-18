"""`smith outing`: weekend outing picks from official sources fitted to the household (FR-57, daily design §18).

- `smith outing search [구·시]`: this weekend's three picks in the default range (or in the 구·시 named).
- `smith outing taste`: the tags Smith fits picks to and the remembered words behind each.
- `smith outing login | status | logout`: the Seoul Open Data Plaza key in the OS credential store.

The data.go.kr key the weather already uses (`smith realestate login`) serves TourAPI and the mid-term forecast.
"""
import argparse
import getpass
import json
import re
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from smith import datagokr, holidays, memory, outing, outing_sources, weather
from smith.config import load_config, local_time

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")
FAILURES = {
    "festival": "한국관광공사 행사 정보를 읽지 못했습니다",
    "seoul": "서울시 문화행사 정보를 일부 읽지 못했습니다",
    "forecast": "단기예보를 읽지 못했습니다",
    "mid": "중기예보를 읽지 못했습니다",
    "air": "미세먼지 예보를 읽지 못했습니다(공기 때문에 바깥 행사를 빼는 판단은 하지 못함)",
}


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("outing", help="Weekend outing picks from official sources, fitted to the household (FR-57)")
    command.add_argument("action", choices=("search", "taste", "login", "status", "logout"),
                         help="search [구·시]; taste; login|status|logout (Seoul Open Data Plaza key)")
    command.add_argument("words", nargs="*", help="search: a 구·시 to narrow the range (e.g. 종로구); default: [outing] default_areas")
    command.add_argument("--top", type=int, default=3, help="search: picks to show (default 3)")
    command.add_argument("--on", type=date.fromisoformat, help="search: treat this day as today (YYYY-MM-DD)")
    command.add_argument("--json", action="store_true", help="search: machine-readable output")
    command.add_argument("--config", type=Path, default=DEFAULT_LOCAL_CONFIG)
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    if args.action in ("login", "status", "logout"):
        return _key(args.action)
    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"Missing setup: local config missing or invalid: {args.config}")
        return 2
    if args.action == "taste":
        return _taste(args.db)
    now = local_time(datetime.now(timezone.utc), config["app"]["timezone"])
    if args.on:
        now = now.replace(year=args.on.year, month=args.on.month, day=args.on.day)
    try:
        outcome = search(args.db, config, now, words=" ".join(args.words), top=max(1, args.top))
    except outing.OutingUnavailable as error:
        print(f"나들이 추천을 만들지 못했습니다: {error.code}")
        return 2
    print(json.dumps(outing.to_dict(outcome), ensure_ascii=False, indent=2) if args.json else outing.render(outcome, now))
    return 0 if outcome.picks else 1


def search(db: Path, config: dict[str, Any], now: datetime, *, words: str = "", top: int = 3,
           key_loader: Callable[[str], str | None] | None = None, get: outing_sources.Getter | None = None,
           opener: Callable[..., Any] | None = None, record: bool = False) -> outing.Outcome:
    """One weekend end to end. `record` writes the picks to the history (only for what is actually sent).
    Raises OutingUnavailable: no data.go.kr key, the memory or history unreadable, or no source answered."""
    from keyring.errors import KeyringError

    from smith import credentials

    loader = key_loader or credentials.load_api_key
    try:
        key, seoul_key = loader("datagokr"), loader(outing_sources.SEOUL_PROVIDER)
    except KeyringError:
        raise outing.OutingUnavailable("keyring") from None
    if not key:
        raise outing.OutingUnavailable("no-datagokr-key")
    settings = config.get("outing", {})
    asked = district_named(words)
    scope = outing.area_range(list(settings.get("default_areas", ["서울"])), asked)
    rest_days, _ = holidays.cached(holidays.path(db))
    days = outing.weekend(now.date(), rest_days)
    try:
        facts = memory.load(memory.directory(db))
    except (OSError, UnicodeDecodeError):
        raise outing.OutingUnavailable("memory-unreadable") from None
    taste = outing.taste_from_memory(facts)
    recent = outing.recent_keys(outing.history_path(db), now.date())

    daily = config.get("daily", {})
    location = home = None
    if "weather" in daily:
        location = weather.Location(**{**daily["weather"], "warning_keywords": tuple(daily["weather"]["warning_keywords"])})
        home = (location.lat, location.lon)
    failures: dict[str, str] = {}
    forecast = outing_sources.weekend_weather(key, location, days, now=now, get=get) if location else None
    if forecast:
        failures.update(forecast.failures)

    events: list[outing_sources.Event] = []
    for province in outing.provinces_to_read(scope):
        try:
            events += outing_sources.festivals(key, days[0], days[-1], province, get=get)
        except datagokr.Error as error:
            failures["festival"] = error.code
    notes: list[str] = []
    reads_seoul = "서울" in scope.provinces or any(c.endswith("구") for c in scope.cities)
    if reads_seoul and seoul_key:
        for category in outing_sources.SEOUL_CATEGORIES:
            try:
                events += outing_sources.seoul_events(seoul_key, category, opener=opener)
            except outing_sources.SourceError as error:
                failures["seoul"] = error.code
                if error.code == "network-error" or error.code.startswith(("http-", "seoul-INFO-100", "seoul-ERROR")):
                    break       # Down or the key refused: the other categories would only wait out the same timeout.
    elif reads_seoul:
        notes.append("서울시 문화행사(전시·공연)는 키가 없어 읽지 않았습니다(smith outing login).")
    if not events and failures:
        raise outing.OutingUnavailable("sources-failed: " + ", ".join(f"{k}={v}" for k, v in failures.items()))
    if words.strip() and not asked:
        notes.append("말씀하신 곳에서 구·시 이름을 찾지 못해 기본 범위로 찾았습니다(동·역 단위로는 아직 거르지 못합니다).")

    result = outing.rank(events, days=days, weather=forecast.days if forecast else [], taste=taste, scope=scope,
                         home=home, airways_sensitive=bool(daily.get("airways_sensitive", False)), top=top,
                         recent=recent)
    for index, pick in enumerate(result.picks):     # Only the chosen few cost a detail call each.
        if pick.event.source == "tour" and not pick.event.link:
            link = outing_sources.homepage(key, pick.event.id, get=get)
            if link:
                result.picks[index] = replace(pick, event=replace(pick.event, link=link))
    result.failures = failures
    result.notes = notes + [FAILURES[k] + f"({v})." for k, v in failures.items() if k in FAILURES]
    if record and result.picks:
        outing.remember(outing.history_path(db), outing.sent_entries(result.picks), now.date())
    return result


def district_named(words: str) -> str | None:
    """The first Seoul 구 or Gyeonggi 시·군 named in the words ("종로구", "과천시에서" → "과천시"), from a fixed list so
    that everyday words ending alike ("친구", "입구", "역시") never narrow the range; only these narrow it for now."""
    for word in words.split():
        match = re.match(r"([가-힣]{2,5}[구시군])(?:에서|에|으로|로|쪽|근처|주변)?$", word)
        if match and match.group(1) in outing.CAPITAL_DISTRICTS:
            return match.group(1)
    return None


def _taste(db: Path) -> int:
    try:
        taste = outing.taste_from_memory(memory.load(memory.directory(db)))
    except (OSError, UnicodeDecodeError):
        print("기억 파일을 읽지 못했습니다.")
        return 2
    if not taste.weights:
        print("아직 기억된 나들이 취향이 없습니다. 모든 분류를 고르게 봅니다.")
        return 0
    for tag in taste.liked():
        print(f"좋아함 · {outing.TAGS[tag][0]}: \"{taste.basis[tag]}\"")
    for tag in taste.disliked():
        print(f"덜 고름 · {outing.TAGS[tag][0]}: \"{taste.basis[tag]}\"")
    print(f"그 밖의 분류는 중립({outing.NEUTRAL})으로 봅니다.")
    return 0


def _key(action: str) -> int:
    from smith import credentials

    provider = outing_sources.SEOUL_PROVIDER
    if action == "status":
        print(f"seoul (서울 열린데이터광장): {'stored' if credentials.load_api_key(provider) else 'missing'}")
        return 0
    if action == "logout":
        credentials.delete_api_key(provider)
        print("Removed the Seoul Open Data Plaza key from the OS credential store.")
        return 0
    if not sys.stdin.isatty():
        print("Run `smith outing login` in an interactive terminal; keys are never passed as arguments.")
        return 2
    key = getpass.getpass("Seoul Open Data Plaza key (hidden, Enter to keep): ").strip()
    if not key:
        print("Nothing entered; the stored key (if any) is unchanged. Check with: smith outing status")
        return 0
    credentials.save_api_key(provider, key)
    print("Saved to the OS credential store. Check with: smith outing status")
    return 0
