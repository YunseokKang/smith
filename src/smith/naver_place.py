"""Naver Place restaurant pages read through the Aside browser, signed out (user decision 2026-11-15, FR-55).

Naver offers no API for place ratings, menus or reviews, and its terms forbid collecting posts by automated means;
the client chose to accept that risk for a narrow use (daily design §17). So the reading stays small and human-paced:
only when the client asks, one search list and at most `MAX_DETAILS` place pages per question, a pause between pages,
never while signed in to Naver (the script stops with `signed-in` if a Naver login cookie is visible), and nothing but
numbers and menu names leaves the page. Review text and reviewer names are not read. The star histogram is read only
where the owner shows the star rating (Naver's star ratings returned on 2026-04-06; the owner chooses whether they show).

The scripts read the data the page already embeds (`window.__APOLLO_STATE__`); they call no Naver endpoint themselves
and type or click nothing. When Naver changes the page, they report `no-data` and Smith says so rather than guessing.
"""
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

from smith import aside

logger = logging.getLogger(__name__)

MARK = "SMITH-NP "
LIST_URL = "https://pcmap.place.naver.com/restaurant/list?query="
DETAIL_URL = "https://pcmap.place.naver.com/restaurant/{id}/review/visitor"
MAP_LINK = "https://map.naver.com/p/entry/place/{id}"
MAX_DETAILS = 5                 # Place pages per question (human pace; also bounds one call to ~45 s).
PAUSE_MS = 1500                 # Between pages.
CALL_BUDGET_MS = 90_000         # The script stops itself before Aside's 120 s limit.
CALL_TIMEOUT = 118
MAX_LIST = 50
MAX_MENUS = 40
MAX_KEYWORDS = 12
MAX_BUCKETS = 12                # Naver's histogram has ten half-star buckets (0.5 .. 5.0).
_ID = re.compile(r"^\d{4,12}$")
_COUNT = re.compile(r"^\s*(\d[\d,]*)\s*$")
_PRICE = re.compile(r"(\d[\d,]*)\s*원")


@dataclass(frozen=True)
class Place:
    """One search-list entry (organic only: Naver's ads are a separate list the script never reads)."""
    id: str
    name: str
    category: str
    area: str | None              # "서울 성동구 성수동2가": the business's district, from the page.
    visitor_reviews: int | None
    rating: Decimal | None        # None when the owner hides the star rating.
    blog_reviews: int | None
    saves: str | None             # Naver shows a band ("~100", "26,000+"), kept as shown.
    status: str | None            # "영업 중", "영업 종료", "오늘 휴무", ...
    michelin: str | None
    broadcasts: int
    booking: bool
    options: tuple[str, ...]
    blurb: str | None             # Naver's one-line summary of the reviews ("성수 데이트에 딱 맞는 캐주얼 분위기").
    distance: str | None


@dataclass(frozen=True)
class Menu:
    name: str
    price: int | None             # Won; None for "변동" or a missing price.
    signature: bool               # Marked as a representative dish by the owner.


@dataclass(frozen=True)
class Stars:
    """The visitor star ratings as Naver shows them: the average and the half-star histogram."""
    average: Decimal | None
    buckets: tuple[tuple[Decimal, int], ...]   # (score 0.5..5.0, reviews), Naver's order.

    @property
    def rated(self) -> int:
        return sum(n for _, n in self.buckets)

    def at_most(self, score: Decimal) -> int:
        return sum(n for s, n in self.buckets if s <= score)


@dataclass(frozen=True)
class Detail:
    """One place page: the menu and the visitor-review keyword votes (counts only)."""
    id: str
    menus: tuple[Menu, ...]
    keywords: tuple[tuple[str, int], ...]   # ("음식이 맛있어요", 243), most voted first.
    voting_reviews: int | None              # Reviews that voted at least one keyword.
    visitor_reviews: int | None
    conveniences: tuple[str, ...]
    menus_complete: bool = True             # False when a menu entry on the page could not be read.
    keywords_complete: bool = True          # False when the vote list or one of its counts could not be read.
    stars: Stars | None = None              # None: hidden by the owner, or not readable.
    stars_hidden: bool = False              # The owner chose not to show star ratings.


def link(place_id: str) -> str:
    return MAP_LINK.format(id=place_id)


def place_id(text: str) -> str | None:
    """The place ID in a Naver Map link ("…/place/1234567") or a bare ID; None otherwise."""
    match = re.search(r"place/(\d{4,12})", text) or re.fullmatch(r"\s*(\d{4,12})\s*", text)
    return match.group(1) if match else None


def name_key(name: str) -> str:
    """A shop name for matching across sources and the visit log: lower case, letters and digits only."""
    return re.sub(r"[^0-9a-z가-힣]", "", name.lower())


def same_name(a: str, b: str) -> bool:
    """Whether two shop names name the same shop: one contains the other ("성수온실" / "성수온실 성수본점")."""
    short, long = sorted((name_key(a), name_key(b)), key=len)
    return len(short) >= 2 and short in long


# --- the scripts (run inside Aside; read-only) ---------------------------------------------------------------------

_PRELUDE = r"""
const ARGS = __ARGS__;
const T0 = Date.now();
const emit = (kind, body) => console.log('SMITH-NP ' + JSON.stringify(Object.assign({ kind }, body || {})));
const SIGNED_IN = ['NID_AUT', 'NID_SES', 'NID_JKL', 'nid_inf'];
const tab = await openTab(ARGS.url);
async function waitFor(test, arg, tries = 30) {
  for (let i = 0; i < tries; i++) {
    if (await tab.evaluate(test, arg)) return true;
    await sleep(500);
  }
  return false;
}
async function signedIn() {
  const names = await tab.evaluate(() => document.cookie.split(';').map(c => c.split('=')[0].trim()));
  return SIGNED_IN.some(n => names.includes(n));
}
"""

_LIST_SCRIPT = _PRELUDE + r"""
try {
  const loaded = await waitFor(() => !!window.__APOLLO_STATE__ &&
      Object.keys(window.__APOLLO_STATE__.ROOT_QUERY || {}).some(k => k.startsWith('placeList')), null);
  if (await signedIn()) { emit('error', { code: 'signed-in' }); }
  else if (!loaded) { emit('error', { code: 'no-data' }); }
  else {
    const out = await tab.evaluate((max) => {
      const st = window.__APOLLO_STATE__;
      const rq = st.ROOT_QUERY || {};
      const keys = Object.keys(rq).filter(k => k.startsWith('placeList') && rq[k] && rq[k].businesses);
      keys.sort((a, b) => ((rq[b].businesses.items || []).length) - ((rq[a].businesses.items || []).length));
      if (!keys.length) return null;
      const biz = rq[keys[0]].businesses;
      const items = (biz.items || []).map(r => st[r.__ref]).filter(Boolean).slice(0, max).map(o => ({
        id: o.id, name: o.name, category: o.category, area: o.commonAddress,
        visitor_reviews: o.visitorReviewCount, rating: o.visitorReviewScore, blog_reviews: o.blogCafeReviewCount,
        saves: o.saveCount, status: o.newBusinessHours && o.newBusinessHours.status,
        michelin: o.michelinGuide ? JSON.stringify(o.michelinGuide).slice(0, 200) : null,
        broadcasts: Array.isArray(o.broadcasts) ? o.broadcasts.length : 0, booking: !!o.hasBooking,
        options: o.options, blurb: o.microReview, distance: o.distance }));
      return { total: biz.total, items };
    }, ARGS.max);
    if (!out) emit('error', { code: 'no-data' });
    else { emit('list', { total: out.total }); out.items.forEach(item => emit('place', item)); }
  }
} finally {
  await closeTab(tab);
}
"""

_DETAIL_SCRIPT = _PRELUDE + r"""
try {
  for (let i = 0; i < ARGS.ids.length; i++) {
    const id = ARGS.ids[i];
    if (Date.now() - T0 > ARGS.budget) { emit('stopped', { at: i }); break; }
    // A pause before every page, the first one too (it follows the list call), and the login check after every load.
    await sleep(ARGS.pause);
    await tab.goto(ARGS.detail.replace('{id}', id));
    const loaded = await waitFor((pid) => !!(window.__APOLLO_STATE__ && window.__APOLLO_STATE__['PlaceDetailBase:' + pid]), id);
    if (await signedIn()) { emit('error', { code: 'signed-in' }); break; }
    if (!loaded) { emit('miss', { id, code: 'no-data' }); continue; }
    await waitFor((pid) => !!window.__APOLLO_STATE__['VisitorReviewStatsResult:' + pid], id, 6);
    // Shapes are checked here too: a field of the wrong type travels as null (Smith reads it as unread), and one
    // page that cannot be read never stops the pages after it.
    let d = null;
    try {
      d = await tab.evaluate(([pid, maxMenus, maxKeywords, maxBuckets]) => {
        const st = window.__APOLLO_STATE__ || {};
        const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v)) ? v : {};
        const base = obj(st['PlaceDetailBase:' + pid]);
        const stats = obj(st['VisitorReviewStatsResult:' + pid]);
        const voted = obj(obj(stats.analysis).votedKeyword);
        const review = obj(stats.review);
        const shown = obj(base.reviewSettings).showVisitorReviewScore;
        const menus = Object.keys(st).filter(k => k.startsWith('PlaceMenuItem')).slice(0, maxMenus).map(k => {
          const m = st[k];
          if (!m || typeof m !== 'object') return null;
          return { name: m.name, price: obj(m.price).displayText, signature: Array.isArray(m.badges) && m.badges.includes('repr') };
        });
        const keywords = Array.isArray(voted.details)
          ? voted.details.slice(0, maxKeywords).map(k => (k && typeof k === 'object') ? [k.displayName, k.count] : null)
          : null;
        // The histogram only where the owner shows stars: a hidden rating stays hidden.
        const buckets = shown === true && Array.isArray(review.starDistribution)
          ? review.starDistribution.slice(0, maxBuckets).map(b => (b && typeof b === 'object') ? [b.score, b.count] : null)
          : null;
        return { id: pid, menus, keywords, voting_reviews: voted.reviewCount, visitor_reviews: base.visitorReviewsTotal,
                 conveniences: Array.isArray(base.conveniences) ? base.conveniences : [],
                 stars_shown: typeof shown === 'boolean' ? shown : null,
                 star_average: shown === true ? review.avgRating : null, star_buckets: buckets };
      }, [id, ARGS.max_menus, ARGS.max_keywords, ARGS.max_buckets]);
    } catch (e) {
      emit('miss', { id, code: 'parse-error' });
      continue;
    }
    emit('detail', d);
  }
} finally {
  await closeTab(tab);
}
"""


# --- calls ---------------------------------------------------------------------------------------------------------

def search(query: str, *, runner: aside.Runner = aside.run, executable: Path | None = None) -> tuple[list[Place], int | None]:
    """The organic list for `query` in Naver's order, and Naver's total. Raises aside.AsideError."""
    exe = executable or aside.find_executable()
    found = aside.records(_LIST_SCRIPT, {"url": LIST_URL + quote(query), "max": MAX_LIST}, mark=MARK, runner=runner,
                          executable=exe, timeout=CALL_TIMEOUT)
    total = next((_count(r.get("total")) for r in found if r.get("kind") == "list"), None)
    places = [p for p in (_place(r) for r in found if r.get("kind") == "place") if p is not None]
    logger.info("naver place list: %s of %s organic entries", len(places), total)
    return places, total


def details(ids: list[str], *, runner: aside.Runner = aside.run,
            executable: Path | None = None) -> tuple[dict[str, Detail], list[str]]:
    """The place pages for up to MAX_DETAILS ids; returns (details by id, ids not read). Raises aside.AsideError."""
    wanted = [i for i in ids if _ID.match(i)][:MAX_DETAILS]
    if not wanted:
        return {}, []
    exe = executable or aside.find_executable()
    args = {"url": "about:blank", "detail": DETAIL_URL, "ids": wanted, "pause": PAUSE_MS,
            "budget": CALL_BUDGET_MS, "max_menus": MAX_MENUS, "max_keywords": MAX_KEYWORDS, "max_buckets": MAX_BUCKETS}
    found = aside.records(_DETAIL_SCRIPT, args, mark=MARK, runner=runner, executable=exe, timeout=CALL_TIMEOUT)
    read = {d.id: d for d in (_detail(r) for r in found if r.get("kind") == "detail") if d is not None and d.id in wanted}
    missing = [i for i in wanted if i not in read]
    logger.info("naver place details: %s read, %s missing", len(read), len(missing))
    return read, missing


# --- parsing (page data is untrusted: every field is checked and bounded) --------------------------------------------

def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:limit]
    return cleaned or None


def _count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str):
        match = _COUNT.match(value)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def _rating(value: Any) -> Decimal | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        rating = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return rating if rating.is_finite() and Decimal(0) < rating <= Decimal(5) else None


def _list(value: Any, limit: int) -> list[Any]:
    return value[:limit] if isinstance(value, list) else []


def _place(record: dict[str, Any]) -> Place | None:
    pid, name = str(record.get("id") or ""), _text(record.get("name"), 80)
    if not _ID.match(pid) or not name:
        return None
    options = tuple(o.strip() for o in (_text(record.get("options"), 400) or "").split(",") if o.strip())
    broadcasts = record.get("broadcasts")
    return Place(id=pid, name=name, category=_text(record.get("category"), 40) or "", area=_text(record.get("area"), 60),
                 visitor_reviews=_count(record.get("visitor_reviews")), rating=_rating(record.get("rating")),
                 blog_reviews=_count(record.get("blog_reviews")), saves=_text(record.get("saves"), 20),
                 status=_text(record.get("status"), 30), michelin=_text(record.get("michelin"), 200),
                 broadcasts=broadcasts if isinstance(broadcasts, int) and broadcasts >= 0 else 0,
                 booking=record.get("booking") is True, options=options[:20], blurb=_text(record.get("blurb"), 80),
                 distance=_text(record.get("distance"), 12))


def _menu(item: Any) -> Menu | None:
    if not isinstance(item, dict):
        return None
    name = _text(item.get("name"), 60)
    if not name:
        return None
    match = _PRICE.search(_text(item.get("price"), 40) or "")
    return Menu(name=name, price=int(match.group(1).replace(",", "")) if match else None,
                signature=item.get("signature") is True)


def _detail(record: dict[str, Any]) -> Detail | None:
    pid = str(record.get("id") or "")
    if not _ID.match(pid):
        return None
    raw_menus, raw_keywords = record.get("menus"), record.get("keywords")
    menu_items, keyword_items = _list(raw_menus, MAX_MENUS), _list(raw_keywords, MAX_KEYWORDS)
    menus = tuple(m for m in (_menu(i) for i in menu_items) if m is not None)
    keywords = []
    for pair in keyword_items:
        if isinstance(pair, list) and len(pair) == 2 and _text(pair[0], 30) and _count(pair[1]) is not None:
            keywords.append((_text(pair[0], 30) or "", _count(pair[1]) or 0))
    conveniences = tuple(c for c in (_text(c, 30) for c in _list(record.get("conveniences"), 20)) if c)
    return Detail(id=pid, menus=menus, keywords=tuple(keywords), voting_reviews=_count(record.get("voting_reviews")),
                  visitor_reviews=_count(record.get("visitor_reviews")), conveniences=conveniences,
                  menus_complete=isinstance(raw_menus, list) and len(menus) == len(menu_items),
                  keywords_complete=isinstance(raw_keywords, list) and len(keywords) == len(keyword_items),
                  stars=_stars(record) if record.get("stars_shown") is True else None,
                  stars_hidden=record.get("stars_shown") is False)


def _stars(record: dict[str, Any]) -> Stars | None:
    """The histogram only when every bucket reads (a half-star score in 0.5..5 and a count); else unread."""
    raw = record.get("star_buckets")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_BUCKETS:
        return None
    buckets = []
    for pair in raw:
        score = _rating(pair[0]) if isinstance(pair, list) and len(pair) == 2 else None
        count = _count(pair[1]) if score is not None else None
        if score is None or count is None or (score * 2) % 1:
            return None
        buckets.append((score, count))
    if len({s for s, _ in buckets}) != len(buckets):
        return None
    return Stars(average=_rating(record.get("star_average")), buckets=tuple(buckets))
