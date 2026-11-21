"""Google's rating for one Naver place, through the official Places API (New) Text Search (FR-55 cross-check).

Naver shows its own reviewers' stars; Google's reviewers are another crowd (fewer review events, more visitors from
abroad). A place Google rates clearly lower is worth a warning. Google gives no star histogram, only the average and
the count (checked 2026-11-16), so it is a second opinion, not the 1~2 star share.

Facts from Google's documentation (2026-11-16): POST places:searchText with `X-Goog-Api-Key` and `X-Goog-FieldMask`;
`rating` and `userRatingCount` bill as Text Search Enterprise, free for 1,000 calls a month. The key lives in the OS
credential store (`smith dining google-login`), never in files or logs; the request carries the shop's name and
district only.
"""
import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from smith import naver_place, net, places

logger = logging.getLogger(__name__)

PROVIDER = "google-places"
ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.displayName,places.formattedAddress,places.rating,places.userRatingCount,places.googleMapsUri"
MONTHLY_CAP = 900     # Free allowance 1,000 a month; the margin covers two runs counting at once.
TIMEOUT = 10
RESULTS = 3

Poster = Callable[[str, bytes, dict[str, str], float], Any]


class GoogleError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Rating:
    name: str
    rating: Decimal | None
    count: int | None
    url: str | None


def lookup(name: str, area: str | None, *, key: str, poster: Poster | None = None) -> Rating | None:
    """Google's entry for the Naver shop `name` in `area` ("서울 성동구 성수동2가"): the first result whose name matches
    and whose address carries the same 구/시; None when no result does. Raises GoogleError on a failed call."""
    district = _district(area)
    if district is None:
        return None
    body = json.dumps({"textQuery": f"{name} {area}", "languageCode": "ko", "regionCode": "KR", "pageSize": RESULTS},
                      ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK}
    data = (poster or _post)(ENDPOINT, body, headers, TIMEOUT)
    places = data.get("places") if isinstance(data, dict) else None
    for item in places[:RESULTS] if isinstance(places, list) else []:
        found = _rating(item)
        address = item.get("formattedAddress") if isinstance(item, dict) else None
        if found is None or not isinstance(address, str) or district not in address:
            continue
        if naver_place.same_name(name, found.name) or _same_branch(name, found.name, area or "", address):
            return found
    return None


KIND_MASK = "places.displayName,places.formattedAddress,places.primaryType,places.types"


def kind(name: str, *, key: str, poster: Poster | None = None) -> tuple[str, list[str], str] | None:
    """(Google's name, its place types, address) for a card merchant's name (FR-60): the first result whose name
    matches; None when none does. The request carries the merchant's name only. Raises GoogleError on a failed call.

    A card statement names no district, so a name shared by shops in several towns may match another branch; the type
    (bar, cafe, restaurant) is what the caller wants and is usually the same."""
    body = json.dumps({"textQuery": name, "languageCode": "ko", "regionCode": "KR", "pageSize": RESULTS},
                      ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": KIND_MASK}
    data = (poster or _post)(ENDPOINT, body, headers, TIMEOUT)
    found = data.get("places") if isinstance(data, dict) else None
    for item in found[:RESULTS] if isinstance(found, list) else []:
        shown = item.get("displayName") if isinstance(item, dict) else None
        title = shown.get("text") if isinstance(shown, dict) else None
        if not isinstance(title, str) or not (naver_place.same_name(name, title) or _contains(name, title)):
            continue
        types = [t for t in [item.get("primaryType"), *(item.get("types") or [])] if isinstance(t, str)]
        address = item.get("formattedAddress")
        return title.strip()[:80], types[:12], address[:120] if isinstance(address, str) else ""
    return None


def _contains(merchant: str, google: str) -> bool:
    """A statement writes "새벽식당 역삼점" or "주식회사 새벽식당" where Google writes the shop's name alone, or the reverse."""
    a, b = naver_place.name_key(merchant), naver_place.name_key(google)
    return len(a) >= 2 and len(b) >= 2 and (a in b or b in a)


def _same_branch(naver: str, google: str, area: str, address: str) -> bool:
    """The two sources spell a branch differently ("요쇼쿠 서울숲 본점" / "요쇼쿠 성수", live 2026-11-16): the names agree
    once the branch words are gone, and Google's address carries the Naver area's 동, not just its 구."""
    words = area.split()
    neighbourhood = words[-1] if words and words[-1].endswith(("동", "가", "읍", "면", "리")) else None
    if neighbourhood is None or neighbourhood not in address:
        return False
    shop = _without_branch(naver, words)
    return len(shop) >= 2 and shop == _without_branch(google, words)


def _without_branch(name: str, area_words: list[str]) -> str:
    """A shop name without its branch words: after the first word, those ending in 점 ("본점", "성수2호점"), listed
    meeting areas ("서울숲") and the start of an area word ("성수" for "성수동1가")."""
    areas = {naver_place.name_key(a) for a in places.KNOWN}
    spots = [naver_place.name_key(w) for w in area_words]
    words = [w for w in (naver_place.name_key(w) for w in name.split()) if w]
    return "".join(words[:1] + [w for w in words[1:]
                                if not (w.endswith("점") or w in areas or any(s.startswith(w) for s in spots))])


def searchable(area: str | None) -> bool:
    """Whether a Naver area carries a district to match Google's address on (else no call is made)."""
    return _district(area) is not None


def _district(area: str | None) -> str | None:
    """The 구 (or else 시/군) of a Naver area, the part both sources spell the same way."""
    words = (area or "").split()
    return next((w for w in words if w.endswith("구")), None) or next((w for w in words if w.endswith(("시", "군"))), None)


def _rating(item: Any) -> Rating | None:
    if not isinstance(item, dict):
        return None
    shown = item.get("displayName")
    name = shown.get("text") if isinstance(shown, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        rating = Decimal(str(item["rating"])) if isinstance(item.get("rating"), (int, float)) else None
    except InvalidOperation:
        rating = None
    rating = rating if rating is not None and rating.is_finite() and 1 <= rating <= 5 else None
    count = item.get("userRatingCount")
    url = item.get("googleMapsUri")
    return Rating(name=name.strip()[:80], rating=rating,
                  count=count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None,
                  url=url if isinstance(url, str) and url.startswith("https://") else None)


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with net.open_url(request, timeout=timeout) as response:
            return json.loads(response.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as error:
        code = {400: "google-bad-request", 401: "google-key-rejected", 403: "google-key-rejected",
                429: "google-quota"}.get(error.code, f"google-http-{error.code}")
        raise GoogleError(code) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        raise GoogleError("google-unreachable") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GoogleError("google-bad-response") from None
