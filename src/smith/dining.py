"""Restaurant picks from Naver Place numbers (FR-55, daily design §17): filter, rank and explain, all in code.

What the household cannot eat comes from the health ledger (the client's fish allergy). The partner likes fish
(client 2026-11-16), so a place stays when the client has enough dishes of their own there (a Japanese place with
sushi and tonkatsu); only fish kitchens (횟집, 참치, 복어, …) drop before their page is read.

Quality is the share of visitor reviews that voted "음식이 맛있어요", pulled toward a prior when few voted, so twelve
enthusiastic reviews cannot outrank twelve thousand. Bad ratings count against it (client 2026-11-16: "최악, 별점 1점이
얼마나 적은지도 봐야 해"): the share of 1~2 star ratings in Naver's histogram, pulled toward its own prior, takes the
score down twice over, so a place most love and many hate ranks below one liked evenly. Where the owner hides the stars
the prior stands in. Google's rating, when a key is stored, is a second opinion: a place Google's reviewers rate
clearly lower is demoted and says so.
"""
import re
import statistics
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Callable

from smith import dining_log, health_letter, naver_place
from smith.naver_place import Detail, Menu, Place

MIN_REVIEWS = 20                  # Fewer visitor reviews than this is too little evidence to recommend on.
PRIOR = 0.6                       # The taste share a place is assumed to have before its votes are counted.
PRIOR_WEIGHT = 30                 # How many votes the prior is worth.
LOW_STARS = Decimal("2")          # "최악": ratings of 0.5 to 2 stars.
LOW_PRIOR = 0.02                  # The 1~2 star share assumed before a place's own ratings are counted (live
                                  # 2026-11-16: 0.3~1.1% on popular places; a hidden histogram gets this).
LOW_WEIGHT = 30
LOW_PENALTY = 2                   # A point of 1~2 star share costs two points of taste share.
SPLIT_SHARE = 0.10                # A shown 1~2 star share this high, over SPLIT_MIN ratings, reads "평이 갈리는 편".
SPLIT_MIN = 20
MIN_EDIBLE = 2                    # Dishes the client can eat for a place to stay in.
CROSS_GAP = Decimal("0.5")        # Google this far below Naver's average, or below CROSS_FLOOR, is a warning
CROSS_FLOOR = Decimal("4.0")      # (over CROSS_MIN Google ratings).
CROSS_MIN = 30
CROSS_PENALTY = 0.9
CROSS_READS = 2                   # Places beyond `top` that get a Google look-up (a demotion may lift them in).
TASTE = "음식이 맛있어요"
# Dishes and shop kinds a fish allergy rules out beyond the health letter's word list (FOOD_GROUPS["생선"]):
# restaurant menus name the fish ("광어회", "명란 파스타", "어묵탕"). Conservative on purpose (해물 is mixed seafood;
# 타코야키·오코노미야키 come topped with 가쓰오부시).
FISH_DISHES = ("명란", "연어", "장어", "초밥", "스시", "사시미", "물회", "회덮밥", "광어", "우럭", "도미", "방어", "대구",
               "명태", "동태", "황태", "북어", "코다리", "생태", "앤초비", "안초비", "엔초비", "날치알", "어묵", "오뎅",
               "복어", "아귀", "아구", "민어", "홍어", "과메기", "굴비", "메로", "농어", "전어", "가자미", "옥돔", "해물",
               "창란", "가쓰오", "가츠오", "가다랑어", "까나리", "청어", "숭어", "빙어", "시샤모", "쥐포", "노가리", "먹태",
               "서더리", "알탕", "매운탕", "어죽", "추어", "회냉면", "회국수", "회무침", "피쉬", "피시", "물고기",
               "사케동", "카이센", "치라시", "니기리", "타코야키", "타코야끼", "오코노미야키", "오코노미야끼",
               "모둠회", "모듬회", "활어회", "숙성회", "선어회",
               # English menus (checked in lower case).
               "fish", "salmon", "tuna", "anchov", "sushi", "sashimi", "mackerel", "bonito")
# Kitchens where the client would find almost nothing: dropped before their page is read.
FISH_ONLY = ("생선", "횟집", "회센터", "참치", "복어", "장어", "아구", "아귀", "매운탕", "해물탕", "물회", "과메기", "홍어")
# Kitchens that serve fish and often something else (sushi with tonkatsu): read, and judged by their menu. There a dish
# counts as the client's only when it names a meat or another clearly non-fish food (a "런치 코스" may well be fish).
FISH_LEANING = ("초밥", "스시", "일식", "해물", "해산물", "오마카세", "이자카야")
NON_FISH = ("돈카츠", "돈까스", "돈가스", "카츠", "가츠", "까스", "규동", "오야코", "부타동", "치킨", "닭", "가라아게",
            "카라아게", "소고기", "쇠고기", "우삼겹", "돼지", "삼겹", "차슈", "한우", "와규", "스테이크", "야키토리",
            "야끼토리", "꼬치", "함박", "카레", "새우", "에비", "고기", "규카츠", "텐더")
# Words with 회 or a fish word that are not fish: beef dishes, 회오리 감자, shop and count words ("상회", "2회 리필").
NOT_FISH = ("육회", "육사시미", "회오리", "회전", "상회", "회관", "회식", "회원", "회차", "회사")
# Drinks, extras and sides: not a meal the client can order.
NOT_MAIN = ("맥주", "생맥", "소주", "하이볼", "사케", "와인", "막걸리", "위스키", "음료", "콜라", "사이다", "에이드",
            "주스", "커피", "아메리카노", "라떼", "녹차", "우롱", "탄산", "공기밥", "추가", "사리", "토핑", "곱빼기",
            "소스", "반찬")
MAIN_PRICE = 5000                 # Below this a priced item is a side.
_TIMES = re.compile(r"\d+\s*회")
# A word of free text that is raw fish itself: "회", "회를", "회부터" (a letter's "기회를", "회의", "회사" are not:
# the word must start with 회 and go on with a particle only; 의 is left out, "회의" being a meeting).
_RAW_FISH = re.compile(r"회(?:를|는|은|이|가|도|랑|이랑|와|과|나|만|로|으로|부터|까지|에|에는|라도|든|처럼|보다|하고|만큼|"
                       r"뿐|밖에)?(?:요)?[.,!?~]*")
# Quotes, brackets, emphasis and other marks around or inside a word ("‘회’를", "**회**를").
_MARKS = re.compile(r"[^\w\s]|_")
# Telling the client to keep off a food ("빼고", "피하세요", "고르지 마세요") and eating it negated ("못 드시는").
_AVOIDING = re.compile(r"피하|피해|빼고|빼세|빼시|말고|마세요|마십시오|지\s*마|제외")
_NEGATED = re.compile(r"(?:못|안)\s*(?:드시|드실|먹)\S*|드시면\s*안\S*|드시지\s*(?:마|않)\S*|먹으면\s*안\S*|먹지\s*(?:마|않)\S*")
# Any word that offers a food, directly or not ("드셔 보세요", "좋겠습니다", "어울려요", "시키세요").
_SUGGESTING = re.compile(r"드십시오|드세요|드셔|드시|드실|먹어|먹는|먹을|먹기|추천|권합|권해|권장|좋겠|좋아요|좋습니다|좋을|"
                         r"괜찮|어울|시키|주문|챙기|챙겨|곁들|맛보|맛있")
# The words a Naver query may carry beyond a known area (dining_evening.AREAS): what people eat, nothing else, so a
# question's names or plans never reach Naver.
FOOD_WORDS = ("한식", "양식", "일식", "중식", "아시안", "파스타", "피자", "버거", "스테이크", "브런치", "카페", "디저트",
              "베이커리", "빵", "와인바", "와인", "이자카야", "오마카세", "라멘", "우동", "소바", "돈카츠", "돈까스", "돈가스",
              "규카츠", "카레", "덮밥", "쌀국수", "태국", "베트남", "인도", "멕시칸", "타코", "딤섬", "만두", "마라탕", "훠궈",
              "샤브샤브", "고기", "고깃집", "삼겹살", "갈비", "불고기", "한우", "곱창", "막창", "족발", "보쌈", "치킨", "국밥",
              "냉면", "칼국수", "비빔밥", "백반", "한정식", "분식", "떡볶이", "중국집", "짜장면", "짬뽕", "오리", "닭갈비",
              "순대", "해장국", "감자탕", "코스", "데이트", "룸", "뷔페", "초밥", "스시", "맛집")


@dataclass(frozen=True)
class Rules:
    """What the client does not eat: `words` flag dishes; `fish` also judges fish kitchens."""
    words: frozenset[str]
    fish: bool

    @classmethod
    def from_constraints(cls, constraints: list[str]) -> "Rules":
        words = set(health_letter.banned_words(constraints))
        fish = bool(words & set(health_letter.FOOD_GROUPS["생선"]))
        return cls(words=frozenset(words | set(FISH_DISHES) if fish else words), fish=fish)

    @classmethod
    def household(cls, load: Callable[[], list[str]]) -> "Rules":
        """The rules from the health ledger (`load` returns the constraint texts); when the ledger cannot be read,
        the strict fish rules stand in, so a check never runs without rules (the household's recorded allergy;
        reviews 2026-11-16)."""
        from smith import health

        try:
            return cls.from_constraints(load())
        except health.HealthError:
            return cls.from_constraints(["생선 알레르기"])

    @property
    def active(self) -> bool:
        return bool(self.words)

    def dish(self, name: str) -> bool:
        """Whether a dish name carries a ruled-out food; with fish ruled out any 회 counts (활어회정식, 모둠회 한상)."""
        text = _not_fish_removed(name.lower())
        return any(w in text for w in self.words) or (self.fish and "회" in text)

    def mentions(self, text: str) -> bool:
        """Whether free text (a sentence of a letter) names a ruled-out food: its words, a fish dish, or 회 itself.
        Word by word, so "기회를", "회의" and "사회" do not count while "활어회정식을" and "회를" do. Quotes and
        emphasis marks go first: "‘회’를" and "**회**를" read as "회를" (review 2026-11-16, round 5)."""
        for word in _MARKS.sub("", text.lower()).split():
            cleaned = _not_fish_removed(word)
            if any(w in cleaned for w in self.words) or (self.fish and _RAW_FISH.fullmatch(cleaned.strip())):
                return True
        return False

    def unsafe(self, sentence: str, avoid: set[str] | frozenset[str] = frozenset()) -> bool:
        """Whether a model's sentence may lead the client to a food they cannot eat (review 2026-11-16, P1): it names
        one (the rules, or a dish flagged on a pick) and either offers anything at all or does not tell them to keep
        off it. Only "생선 메뉴는 빼고 고르세요"-like sentences survive; mentions for the partner drop too (safe side)."""
        if not (self.mentions(sentence) or any(name and name in sentence for name in avoid)):
            return False
        return bool(_SUGGESTING.search(_NEGATED.sub(" ", sentence))) or not _AVOIDING.search(sentence)

    def scrub(self, text: str, avoid: set[str] | frozenset[str] = frozenset()) -> tuple[str, int]:
        """`text` without its unsafe sentences (line breaks kept); returns (text, sentences dropped)."""
        lines, dropped = [], 0
        for line in text.split("\n"):
            kept = []
            for sentence in re.split(r"(?<=[.!?])\s+", line):
                if sentence.strip() and self.unsafe(sentence, avoid):
                    dropped += 1
                else:
                    kept.append(sentence)
            lines.append(" ".join(kept).rstrip())
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip(), dropped

    def fish_only(self, place: Place) -> bool:
        """A fish kitchen by its category or name (only when fish is ruled out)."""
        text = _not_fish_removed(f"{place.category} {place.name}")
        return self.fish and (any(k in text for k in FISH_ONLY) or "회" in _not_fish_removed(place.category))

    def fish_leaning(self, place: Place) -> bool:
        return self.fish and any(k in f"{place.category} {place.name}" for k in FISH_LEANING)

    def edible(self, menu: Menu, leaning: bool) -> bool:
        """A dish the client can order: no ruled-out food, and in a fish-leaning kitchen a named non-fish food."""
        if self.dish(menu.name):
            return False
        return not leaning or any(word in menu.name for word in NON_FISH)


@dataclass(frozen=True)
class Cross:
    """Another platform's rating of the same shop."""
    source: str                   # "구글"
    rating: Decimal | None
    count: int | None
    url: str | None

    def lower_than(self, naver: Decimal | None) -> bool:
        """Clearly lower than Naver (or plainly low), over enough ratings to mean something."""
        if self.rating is None or (self.count or 0) < CROSS_MIN:
            return False
        return self.rating < CROSS_FLOOR or (naver is not None and naver - self.rating >= CROSS_GAP)


@dataclass(frozen=True)
class Pick:
    place: Place
    detail: Detail
    voters: int
    tasty: int                    # Votes for "음식이 맛있어요" (0 when not among the most voted).
    score: float
    signature: tuple[Menu, ...]   # Representative dishes the client can eat (or the first ones).
    avoid: tuple[str, ...]        # Dishes the rules flag (the partner may still enjoy them).
    highlights: tuple[tuple[str, int], ...]
    rated: int = 0                # Star ratings in Naver's histogram (0: hidden or none).
    low: int = 0                  # Of those, 1~2 stars.
    broth: bool = False           # A fish-leaning kitchen: broths and toppings may carry fish.
    cross: Cross | None = None
    visit: dining_log.Visit | None = None

    @property
    def taste_share(self) -> float | None:
        return min(self.tasty, self.voters) / self.voters if self.voters else None

    @property
    def low_share(self) -> float | None:
        return self.low / self.rated if self.rated else None

    @property
    def split(self) -> bool:
        """Ratings gathered at both ends: many 1~2 stars on a histogram large enough to show it."""
        return self.rated >= SPLIT_MIN and self.low / self.rated >= SPLIT_SHARE

    @property
    def stars_average(self) -> Decimal | None:
        return (self.detail.stars.average if self.detail.stars else None) or self.place.rating

    @property
    def typical_price(self) -> int | None:
        prices = [m.price for m in self.signature if m.price]
        return int(statistics.median(prices)) if prices else None


@dataclass
class Outcome:
    query: str
    total: int | None
    listed: int
    picks: list[Pick] = field(default_factory=list)
    ruled_out: dict[str, list[str]] = field(default_factory=dict)
    unread: list[str] = field(default_factory=list)   # Names whose page could not be read.
    rules_known: bool = True
    cross_note: str | None = None                     # Why the Google check did not run or stopped, if it did not.


def shortlist(places: list[Place], rules: Rules, limit: int, ruled_out: dict[str, list[str]]) -> list[Place]:
    """The first `limit` places in Naver's order that the rules and the evidence floor allow."""
    chosen: list[Place] = []
    seen: set[str] = set()
    for place in places:
        if place.id in seen or len(chosen) >= limit:
            continue
        seen.add(place.id)
        if rules.fish_only(place):
            ruled_out.setdefault("생선 요리 전문점", []).append(place.name)
        elif (place.visitor_reviews or 0) < MIN_REVIEWS:
            ruled_out.setdefault(f"방문자 리뷰 {MIN_REVIEWS}개 미만", []).append(place.name)
        else:
            chosen.append(place)
    return chosen


def rank(chosen: list[Place], details: dict[str, Detail], rules: Rules, ruled_out: dict[str, list[str]],
         visits: list[dining_log.Visit] | None = None) -> list[Pick]:
    """Picks for the places whose page was read, best first. A place drops when its evidence or menu cannot be read,
    when the client has fewer than MIN_EDIBLE dishes there, or when the couple rated an earlier visit 1 or 2."""
    picks = []
    for place in chosen:
        detail = details.get(place.id)
        if detail is None:
            continue
        leaning = rules.fish_leaning(place)
        # Mains only: drinks, extras and sides never count as the client's dishes, even when nothing else was read.
        edible = [m for m in detail.menus if _main(m) and rules.edible(m, leaning)]
        visit = dining_log.latest_visit(visits or [], place)
        reason = _drop_reason(detail, rules, visit, len(edible))
        if reason:
            ruled_out.setdefault(reason, []).append(place.name)
            continue
        avoid = tuple(m.name for m in detail.menus if rules.dish(m.name))
        # Absent from a fully read list of the most-voted keywords means few votes: counted as none.
        tasty = next((n for name, n in detail.keywords if name == TASTE), 0)
        voters = detail.voting_reviews or 0
        rated = detail.stars.rated if detail.stars else 0
        low = detail.stars.at_most(LOW_STARS) if detail.stars else 0
        shown = edible or ([] if rules.active else list(detail.menus))
        picks.append(Pick(place=place, detail=detail, voters=voters, tasty=tasty, score=score(tasty, voters, low, rated),
                          signature=(tuple(m for m in shown if m.signature) or tuple(shown))[:3], avoid=avoid[:5],
                          highlights=tuple(k for k in detail.keywords if k[0] != TASTE)[:3], rated=rated, low=low,
                          broth=leaning, visit=visit))
    picks.sort(key=lambda p: (-p.score, -(p.place.visitor_reviews or 0)))
    return picks


def score(tasty: int, voters: int, low: int, rated: int) -> float:
    """The taste share pulled toward PRIOR, times one less twice the 1~2 star share pulled toward LOW_PRIOR."""
    taste = (min(tasty, voters) + PRIOR * PRIOR_WEIGHT) / (voters + PRIOR_WEIGHT)
    low_rate = (min(low, rated) + LOW_PRIOR * LOW_WEIGHT) / (rated + LOW_WEIGHT)
    return taste * max(0.0, 1 - LOW_PENALTY * low_rate)


def _drop_reason(detail: Detail, rules: Rules, visit: dining_log.Visit | None, edible: int) -> str | None:
    if not detail.voting_reviews or not detail.keywords_complete:
        return "리뷰 키워드 투표를 읽지 못함"
    if rules.active and (not detail.menus or not detail.menus_complete):
        return "메뉴를 읽지 못해 못 드시는 음식 확인 불가"
    if visit is not None and visit.rating <= 2:
        return "지난번 별로였던 곳"
    if rules.active and edible < MIN_EDIBLE:
        return f"드실 수 있는 메뉴가 {MIN_EDIBLE}개 미만"
    return None


def query_words(text: str, area: str | None) -> str:
    """The Naver query for a question: the known area and up to two FOOD_WORDS found in it, nothing else (counsel
    review 2026-11-16, P0: a router-written query could carry a contact's name). "맛집" when it names no food."""
    foods = [w for w in FOOD_WORDS if w in text and w != "맛집"][:2]
    return " ".join([*([area] if area else []), *(foods or ["맛집"])])


def _main(menu: Menu) -> bool:
    return not any(word in menu.name for word in NOT_MAIN) and (menu.price is None or menu.price >= MAIN_PRICE)


def _not_fish_removed(text: str) -> str:
    for word in NOT_FISH:
        text = text.replace(word, " ")
    return _TIMES.sub(" ", text)


def cross_check(picks: list[Pick], top: int, lookup: Callable[[Place], Cross | None]) -> tuple[list[Pick], str | None]:
    """Google's rating for the first `top + CROSS_READS` picks; a clearly lower one costs CROSS_PENALTY. Stops at the
    first failed call (quota, key, network) and says why; the picks stand on Naver alone then."""
    checked, note = [], None
    for n, pick in enumerate(picks):
        if n < top + CROSS_READS and note is None:
            try:
                found = lookup(pick.place)
            except CrossFailed as failure:
                note, found = failure.code, None
            if found is not None:
                lower = found.lower_than(pick.stars_average)
                pick = replace(pick, cross=found, score=pick.score * (CROSS_PENALTY if lower else 1))
        checked.append(pick)
    checked.sort(key=lambda p: (-p.score, -(p.place.visitor_reviews or 0)))
    return checked, note


class CrossFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def recommend(query: str, rules: Rules, *, read_limit: int = naver_place.MAX_DETAILS, top: int = 3,
              search: Callable[[str], tuple[list[Place], int | None]] = naver_place.search,
              read: Callable[[list[str]], tuple[dict[str, Detail], list[str]]] = naver_place.details,
              cross: Callable[[Place], Cross | None] | None = None, cross_note: str | None = None,
              visits: list[dining_log.Visit] | None = None) -> Outcome:
    """One question: the Naver list, the shortlist's pages, the ranked picks, Google's word on the leaders when
    `cross` is given (else `cross_note` says why not). Raises aside.AsideError."""
    places, total = search(query)
    outcome = Outcome(query=query, total=total, listed=len(places), cross_note=cross_note)
    chosen = shortlist(places, rules, min(read_limit, naver_place.MAX_DETAILS), outcome.ruled_out)
    details, missing = read([p.id for p in chosen]) if chosen else ({}, [])
    outcome.unread = [p.name for p in chosen if p.id in missing]
    picks = rank(chosen, details, rules, outcome.ruled_out, visits)
    if cross is not None and picks:
        picks, outcome.cross_note = cross_check(picks, top, cross)
    outcome.picks = picks[:top]
    return outcome
