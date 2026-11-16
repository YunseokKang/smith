"""`smith dining`: restaurant picks from Naver Place read signed out (FR-55), the couple's visit log, and the
optional Google cross-check key.

- `smith dining search 성수 파스타`: three picks for a place and a craving.
- `smith dining visited <이름 또는 네이버 지도 링크> --rating 1..5 [--note …] [--on YYYY-MM-DD]`: how a visit went.
- `smith dining visits`: the log.
- `smith dining google-login | google-status | google-logout`: the Places API key in the OS credential store.
"""
import argparse
import getpass
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from smith import aside, dining, dining_log, dining_view, google_places, health, naver_place

FAILURES = {
    "aside-not-found": "Aside가 설치돼 있지 않거나 찾지 못했습니다.",
    "aside-failed": "Aside 호출이 실패했습니다(앱이 꺼져 있을 수 있습니다).",
    "aside-timeout": "Aside 호출이 시간 안에 끝나지 않았습니다.",
    "signed-in": "Aside 브라우저가 네이버에 로그인돼 있어 읽지 않았습니다. 로그아웃하거나 로그인하지 않은 프로필을 쓰세요.",
    "no-data": "네이버 화면에서 가게 정보를 찾지 못했습니다(화면 구조가 바뀌었거나 막혔을 수 있습니다).",
}


class DiningUnavailable(Exception):
    """Picks cannot be made safely: the health ledger or the visit log is unreadable (`code` says which)."""
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("dining", help="Restaurant picks from Naver Place (read signed out through Aside), visits, Google key")
    command.add_argument("action", choices=("search", "visited", "visits", "google-login", "google-status", "google-logout"),
                         help="search WORDS (e.g. 성수 파스타); visited NAME-OR-LINK --rating N; visits; google-login|status|logout")
    command.add_argument("words", nargs="*", help="search: area and food as typed into Naver Map; visited: the shop name or its Naver Map link")
    command.add_argument("--top", type=int, default=3, help="search: picks to show (default 3)")
    command.add_argument("--read", type=int, default=naver_place.MAX_DETAILS,
                         help=f"search: place pages to read (at most {naver_place.MAX_DETAILS})")
    command.add_argument("--rating", type=int, choices=range(1, 6), help="visited: the couple's verdict, 1 (다시 안 감) to 5")
    command.add_argument("--note", default="", help="visited: a short note (메뉴, 분위기)")
    command.add_argument("--on", type=date.fromisoformat, help="visited: the day (default today)")
    command.add_argument("--json", action="store_true", help="search: machine-readable output")
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    if args.action.startswith("google-"):
        return _google_key(args.action)
    if args.action == "visits":
        return _visits(args.db)
    if args.action == "visited":
        return _visited(args)
    query = " ".join(args.words).strip()
    if not query:
        print("검색어를 주세요: smith dining search 성수 파스타")
        return 2
    try:
        result = search(args.db, query, read_limit=args.read, top=args.top)
    except DiningUnavailable as error:
        print(f"음식 제약이나 방문 기록을 읽지 못해 멈춥니다: {error.code}")
        return 2
    except aside.AsideError as error:
        print(FAILURES.get(error.code, f"읽기 실패: {error.code}"))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result, datetime.now(timezone.utc)))
    return 0 if result["picks"] else 1


def household(db: Path) -> tuple[list[str], list[dining_log.Visit], str]:
    """The food constraints, the visit log and a fingerprint of both (a cached pick made under other rules or before
    a bad visit is not reused). Raises DiningUnavailable: no picks without the food rules (fail closed)."""
    try:
        constraints = [c.text for c in health.load(health.path(db)).constraints]
    except health.HealthError as error:
        raise DiningUnavailable(f"health-{error}") from None
    try:
        visits = dining_log.load_visits(dining_log.folder(db))
    except dining_log.DiningLogError as error:
        raise DiningUnavailable(f"visits-{error}") from None
    state = json.dumps({"constraints": sorted(constraints),
                        "visits": [[v.name, v.on.isoformat(), v.rating, v.place_id] for v in visits]}, ensure_ascii=False)
    return constraints, visits, hashlib.sha256(state.encode("utf-8")).hexdigest()[:16]


def search(db: Path, query: str, *, read_limit: int = naver_place.MAX_DETAILS, top: int = 3,
           now: datetime | None = None, use_google: bool = True,
           recommend: Callable[..., dining.Outcome] = dining.recommend) -> dict[str, Any]:
    """One question end to end: the household's rules and visits, the Naver read, Google's word when a key is stored
    and this month's allowance is left. Returns the dict form (dining_view.to_dict). Raises DiningUnavailable (fail
    closed: no picks without the food rules) and aside.AsideError."""
    constraints, visits, _ = household(db)
    where = dining_log.folder(db)
    rules = dining.Rules.from_constraints(constraints)
    cross, note = google_cross(where, now or datetime.now(timezone.utc)) if use_google else (None, None)
    outcome = recommend(query, rules, read_limit=max(1, min(read_limit, naver_place.MAX_DETAILS)), top=max(1, top),
                        cross=cross, cross_note=note, visits=visits)
    outcome.rules_known = bool(constraints)
    return dining_view.to_dict(outcome)


def google_cross(where: Path, now: datetime, *, key_loader: Callable[[], str | None] | None = None,
                 lookup: Callable[..., google_places.Rating | None] = google_places.lookup) -> tuple[Callable[[naver_place.Place], dining.Cross | None] | None, str | None]:
    """The Google look-up for `dining.recommend`, counted against this month's allowance; (None, reason) when there is
    no key ("google-no-key", silent in the output) or the month's cap is reached."""
    from keyring.errors import KeyringError

    from smith import credentials

    try:
        key = (key_loader or (lambda: credentials.load_api_key(google_places.PROVIDER)))()
    except KeyringError:
        return None, "google-keyring"
    if not key:
        return None, "google-no-key"
    month = now.strftime("%Y-%m")
    try:
        if dining_log.google_used(where, month) >= google_places.MONTHLY_CAP:
            return None, "google-cap"
    except dining_log.DiningLogError:
        return None, "google-log-unreadable"

    def check(place: naver_place.Place) -> dining.Cross | None:
        if not google_places.searchable(place.area):
            return None                              # No district to match on: no call, nothing counted.
        try:
            # Counted before the call, under a lock: a lost response still spent a call, and two runs never both
            # take the last one (review 2026-11-16, P1).
            if not dining_log.reserve_google(where, month, google_places.MONTHLY_CAP):
                raise dining.CrossFailed("google-cap")
            found = lookup(place.name, place.area, key=key)
        except google_places.GoogleError as error:
            raise dining.CrossFailed(error.code) from None
        except (dining_log.DiningLogError, OSError):
            raise dining.CrossFailed("google-log-unreadable") from None
        return None if found is None else dining.Cross(source="구글", rating=found.rating, count=found.count, url=found.url)
    return check, None


def render(result: dict[str, Any], now: datetime) -> str:
    total = f"{result['total']:,}곳 중 " if result.get("total") else ""
    lines = [f"\"{result['query']}\" 네이버 플레이스 {total}앞쪽 {result['listed']}곳을 보고 추천합니다."]
    if result["ruled_out"]:
        lines.append("뺀 곳: " + "; ".join(f"{why} {len(names)}곳({', '.join(names[:3])}{' 외' if len(names) > 3 else ''})"
                                         for why, names in result["ruled_out"].items()))
    if result["unread"]:
        lines.append(f"상세를 읽지 못한 곳: {', '.join(result['unread'])}")
    if not result["picks"]:
        lines.append("추천할 곳을 찾지 못했습니다. 검색어를 바꿔 보세요(예: 동네 이름 + 음식).")
    for n, pick in enumerate(result["picks"], 1):
        lines += [""] + dining_view.pick_lines(n, pick)
    lines += ["", f"출처: 네이버 플레이스(로그인하지 않은 상태로 읽음), {now.astimezone().strftime('%Y-%m-%d %H:%M')}. "
                  "숫자는 네이버 화면 그대로이고 광고 자리는 뺐습니다. 순위는 \"음식이 맛있어요\" 비율에서 별점 1~2점 비율을 깎아 정합니다."]
    note = result.get("cross_note")
    if note in dining_view.CROSS_NOTES:
        lines.append(dining_view.CROSS_NOTES[note])
    elif note == "google-no-key":
        lines.append("구글 교차 확인은 꺼져 있습니다(키 등록: smith dining google-login).")
    lines += dining_view.notes(result)
    return "\n".join(lines)


def _visited(args: argparse.Namespace) -> int:
    # A Naver Map link among the words gives the place ID; the other words are the name.
    links = {w: naver_place.place_id(w) for w in args.words if "place/" in w}
    found_id = next((i for i in links.values() if i), None)
    name = " ".join(w for w in args.words if w not in links).strip() or (f"네이버 장소 {found_id}" if found_id else "")
    if not name or args.rating is None:
        print("가게 이름(또는 네이버 지도 링크)과 --rating 1~5를 주세요: smith dining visited 성수온실 --rating 4 --note \"파스타 좋음\"")
        return 2
    visit = dining_log.Visit(name=name[:80], on=args.on or date.today(), rating=args.rating, place_id=found_id,
                             note=" ".join(args.note.split())[:dining_log.MAX_NOTE])
    try:
        dining_log.add_visit(dining_log.folder(args.db), visit)
    except (dining_log.DiningLogError, OSError) as error:
        print(f"방문 기록 파일을 읽지 못해 쓰지 않았습니다: {error}")
        return 2
    verdict = "다음 추천에서 뺍니다" if visit.rating <= 2 else "다음 추천에 함께 적습니다"
    print(f"기록했습니다: {visit.on} {visit.name} {visit.rating}점. {verdict}.")
    return 0


def _visits(db: Path) -> int:
    try:
        visits = dining_log.load_visits(dining_log.folder(db))
    except dining_log.DiningLogError as error:
        print(f"방문 기록 파일을 읽지 못했습니다: {error}")
        return 2
    for v in reversed(visits[-30:]):
        print(f"{v.on} · {v.rating}점 · {v.name}" + (f" · {v.note}" if v.note else "") + (f" · {naver_place.link(v.place_id)}" if v.place_id else ""))
    if not visits:
        print("(아직 기록이 없습니다)")
    return 0


def _google_key(action: str) -> int:
    from smith import credentials

    if action == "google-status":
        print(f"google-places: {'stored' if credentials.load_api_key(google_places.PROVIDER) else 'missing'}")
        return 0
    if action == "google-logout":
        credentials.delete_api_key(google_places.PROVIDER)
        print("Removed the Google Places key from the OS credential store.")
        return 0
    if not sys.stdin.isatty():
        print("Run `smith dining google-login` in an interactive terminal; keys are never passed as arguments.")
        return 2
    key = getpass.getpass("Google Places API key (hidden, Enter to keep): ").strip()
    if not key:
        print("Nothing entered; the stored key (if any) is unchanged. Check with: smith dining google-status")
        return 0
    credentials.save_api_key(google_places.PROVIDER, key)
    print("Saved to the OS credential store. Check with: smith dining google-status")
    return 0
