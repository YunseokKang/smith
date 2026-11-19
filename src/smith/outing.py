"""Weekend outing picks (FR-57, docs/daily-design.md §18): what is on this weekend in the household's range, fitted to
the couple.

Code does everything a number or a rule decides, as for restaurants (§17): the dates (this weekend, joined to an
adjacent public holiday), the range (the client's `[outing] default_areas`, or a 구·시 named in the question), what
is on (official sources, `outing_sources`), the weather fit (outdoor events need a dry day; on a day whose dust
forecast is 나쁨 or worse an airway-sensitive household gets indoor picks only), the taste (tags the client's own
words in memory name, weighted above the neutral ones), the distance from home (district precision) and the
variety of the three picks. Every line the reader sees is written here, so the CLI, the Thursday evening card and a
counsel letter say the same thing.

Phase 1 reads time-bound events only (festivals, exhibitions, performances, hands-on programmes). Trending places
(Naver Place, web research), the nearby restaurant, the Evernote and calendar taste and post-visit ratings are §18.5
phase 2.
"""
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smith import memory, weather
from smith.outing_sources import DayWeather, Event

WEEKDAYS_KO = "월화수목금토일"
NEUTRAL = 0.3          # A tag the client never mentioned.
LIKED = 1.0            # A tag the client's own words name.
EXTRA_LIKED = 0.1      # Each further liked tag on the same event.
KIDS = 0.4             # Children's programmes: kept, but well below (a couple's outing).
ENDING = 1.15          # A run of several days that ends within a week: now or never.
ENDING_DAYS = 7
DISTANCE_KM = 40.0     # Distance factor 1 / (1 + km / DISTANCE_KM): 10 km 0.8, 40 km 0.5.
UNKNOWN_DISTANCE = 0.75
WET_OUTDOOR = 0.35
UNKNOWN_SKY = 0.85
BAD_AIR_OUTDOOR = 0.5
BAD_AIR = 3            # weather.GRADES: 3 = 나쁨.
COLD_OUTDOOR = 0.8     # Outdoors on a day whose high stays under weather.COLD_MIN.
STANDING_DAYS = 180    # Runs this long are standing programmes, not this weekend's news.
STANDING = 0.6
REPEAT_DAYS = 60       # A place recommended (and sent) within this many days is not repeated.
HISTORY_KEEP_DAYS = 120

# Tags: reader label and the words that name them, in the client's memory or in an event's title.
TAGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "art": ("미술·전시", ("미술", "전시", "갤러리", "미술관", "사진전", "비엔날레", "아트페어", "작품전", "개인전", "기획전")),
    "history": ("역사·문화유산", ("역사", "박물관", "고궁", "궁궐", "유적", "문화유산", "국가유산", "전통", "한옥", "왕릉")),
    "books": ("인문·책", ("인문", "철학", "고전", "독서", "서점", "도서관", "문학", "북토크", "낭독", "에세이")),
    "music": ("음악 공연", ("클래식", "음악회", "콘서트", "오케스트라", "국악", "재즈", "리사이틀", "독주회", "독창회", "실내악")),
    "stage": ("무대 공연", ("뮤지컬", "연극", "오페라", "무용", "발레")),
    "nature": ("자연·산책", ("산책", "공원", "숲", "정원", "수목원", "자연", "호수", "단풍", "둘레길", "트레킹", "경관")),
    "drive": ("드라이브·근교", ("드라이브", "근교", "바다", "해안", "전망대", "여행지")),
    "food": ("미식", ("미식", "다이닝", "맛집", "푸드", "디저트", "와인", "커피", "티룸", "먹거리")),
    "wellness": ("운동·웰니스", ("테니스", "웰니스", "요가", "명상", "스파", "러닝", "마라톤", "자전거")),
    "festival": ("축제", ("축제", "페스티벌", "불꽃", "등불", "빛축제")),
    "craft": ("체험", ("체험", "공방", "원데이", "만들기", "클래스", "워크숍")),
    "shopping": ("쇼핑·마켓", ("쇼핑", "팝업", "마켓", "장터", "플리마켓", "아울렛")),
}
# A memory fact counts as taste only when it is about liking or leisure (finance principles also say 역사·전통).
TASTE_CUES = ("취향", "좋아", "즐기", "즐겨", "관람", "여가", "취미", "주말", "데이트", "경험 소비", "가보고")
SEOUL_TAGS = {"전시/미술": ("art",), "클래식": ("music",), "콘서트": ("music",), "국악": ("music", "history"),
              "독주/독창회": ("music",), "뮤지컬/오페라": ("stage",), "연극": ("stage",), "무용": ("stage",),
              "축제-문화/예술": ("festival", "art"), "축제-전통/역사": ("festival", "history"),
              "축제-자연/경관": ("festival", "nature"), "축제-시민화합": ("festival",), "축제-기타": ("festival",),
              "교육/체험": ("craft",)}
INDOOR_SEOUL = {"전시/미술", "클래식", "콘서트", "국악", "독주/독창회", "뮤지컬/오페라", "연극", "무용", "교육/체험"}
TOUR_TAGS = {"EV01": ("festival",), "EV02": ("stage",), "EV03": ("festival",)}   # lclsSystm2 축제 / 공연 / 행사
INDOOR_WORDS = ("전시", "미술관", "박물관", "갤러리", "실내", "공연장", "아트센터", "극장")   # In a title or address.
# A venue that says it is outside, and the words that make a venue a building even inside a park ("올림픽공원 올림픽홀").
OUTDOOR_PLACE = ("야외", "광장", "마당", "거리", "한강", "숲", "정원", "운동장")
INDOOR_PLACE = ("홀", "극장", "센터", "관", "로비", "층", "B1", "실")
# Opening hours that leave the weekend out ("월-금 10:30~18:30", "평일"), unless they name a weekend day too.
WEEKDAYS_ONLY = re.compile(r"평일|월(?:요일)?\s*[-~]\s*금")
WEEKEND_OPEN = re.compile(r"토|주말|매일|연중|공휴일|일요일|[-~]\s*일(?![가-힣])")
# Taste sentences: a clause with one of these is a dislike (or unclear), never a liking (Codex review 2026-11-18).
NEGATIVE = ("싫", "않", "별로", "피하", "꺼리", "질색", "관심 없", "흥미 없", "안 좋아", "안 가", "못 가")
DISLIKED = 0.15
_CLAUSE = re.compile(r"[,.;!?\n]|(?<=[가-힣])(?:고|지만|는데|며|면서)\s")
KID_TITLE = ("어린이", "유아", "키즈", "아동", "초등", "청소년", "아기", "그림책", "동요")
KID_AUDIENCE = re.compile(r"^(?:만?\s*\d+\s*(?:개월|세)\s*(?:~|-)\s*\d+\s*세|미취학|유아|어린이|아동|초등학생(?!\s*이상)|청소년(?!\s*이상))")
SKIP_TITLE = ("모집", "공모", "공고", "참가자 접수", "수강생", "오디션", "채용")


# The names a question may narrow the range to (public administrative names): Seoul's 25 구 and Gyeonggi's 시·군.
CAPITAL_DISTRICTS = frozenset((
    "종로구 중구 용산구 성동구 광진구 동대문구 중랑구 성북구 강북구 도봉구 노원구 은평구 서대문구 마포구 양천구 강서구 구로구 "
    "금천구 영등포구 동작구 관악구 서초구 강남구 송파구 강동구 "
    "수원시 성남시 의정부시 안양시 부천시 광명시 평택시 동두천시 안산시 고양시 과천시 구리시 남양주시 오산시 시흥시 군포시 의왕시 "
    "하남시 용인시 파주시 이천시 안성시 김포시 화성시 광주시 양주시 포천시 여주시 연천군 가평군 양평군").split())


class OutingUnavailable(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Taste:
    weights: dict[str, float]
    basis: dict[str, str]            # tag -> the memory sentence (first 80 chars) that named it

    def weight(self, tag: str) -> float:
        return self.weights.get(tag, NEUTRAL)

    def liked(self) -> list[str]:
        return [t for t, w in sorted(self.weights.items(), key=lambda kv: -kv[1]) if w >= LIKED]

    def disliked(self) -> list[str]:
        return [t for t, w in self.weights.items() if w < NEUTRAL]


@dataclass(frozen=True)
class Pick:
    event: Event
    score: float
    day: date                        # The weekend day it fits best.
    tags: tuple[str, ...]
    outdoor: bool
    km: float | None
    reasons: tuple[str, ...]
    kids: bool = False


@dataclass
class Outcome:
    days: list[date]
    weather: list[DayWeather]
    picks: list[Pick] = field(default_factory=list)
    considered: int = 0
    ruled_out: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    range_label: str = ""
    taste: Taste | None = None


# ------------------------------------------------------------------------------------------------------------- dates

def weekend(today: date, holidays: set[date] = frozenset()) -> list[date]:
    """This weekend: today if it is a Saturday or Sunday (and the rest of it), else the coming Saturday and Sunday;
    joined with any public holiday right before or after (a Friday or Monday holiday makes a long weekend)."""
    if today.weekday() == 6:
        days = [today]
    else:
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        days = [saturday, saturday + timedelta(days=1)]
    while days[0] - timedelta(days=1) in holidays and days[0] - timedelta(days=1) >= today:
        days.insert(0, days[0] - timedelta(days=1))
    while days[-1] + timedelta(days=1) in holidays:
        days.append(days[-1] + timedelta(days=1))
    return days


def days_label(days: list[date]) -> str:
    first, last = days[0], days[-1]
    head = f"{first.month}/{first.day} {WEEKDAYS_KO[first.weekday()]}"
    return head if first == last else f"{head}~{last.month}/{last.day} {WEEKDAYS_KO[last.weekday()]}"


# ------------------------------------------------------------------------------------------------------------- taste

def taste_from_memory(facts: list[memory.Fact]) -> Taste:
    """Tags from the client's own preference facts that talk about liking or leisure, read clause by clause: a tag in
    a clause with a negative word ("미술 전시는 싫고", "좋아하지 않아요") is a dislike, never a liking; a tag both
    liked and disliked is unclear and stays neutral. Every tag never mentioned is neutral."""
    liked: dict[str, str] = {}
    disliked: dict[str, str] = {}
    for fact in facts:
        if fact.topic != "preference" or not any(cue in fact.text for cue in TASTE_CUES):
            continue
        for clause in _CLAUSE.split(fact.text):
            if not clause or not clause.strip():
                continue
            side = disliked if any(n in clause for n in NEGATIVE) else liked
            for tag, (_, words) in TAGS.items():
                if any(w in clause for w in words):
                    side.setdefault(tag, fact.text[:80])
    weights = {t: LIKED for t in liked if t not in disliked} | {t: DISLIKED for t in disliked if t not in liked}
    return Taste(weights, {t: (liked.get(t) or disliked[t]) for t in weights})


def event_tags(event: Event) -> tuple[str, ...]:
    tags = list(SEOUL_TAGS.get(event.category, ())) if event.source == "seoul" else \
        list(TOUR_TAGS.get(event.category[:4], ()))
    for tag, (_, words) in TAGS.items():
        if tag not in tags and any(w in event.title for w in words):
            tags.append(tag)
    return tuple(tags)


def outdoor(event: Event) -> bool:
    """Whether the weather matters. TourAPI events are taken as outdoor unless the title or address names an indoor
    venue (a 공연 there is often a street ritual such as 남산봉수의식); Seoul's categories say it better, except
    that a venue saying it is outside wins over the category ("○○공원 야외무대" for a concert, Codex review 2026-11-18:
    otherwise a dusty day's airway rule would not apply)."""
    if any(w in event.title for w in ("야외", "불꽃")) or "야외" in event.place:
        return True
    if any(w in event.title for w in INDOOR_WORDS):
        return False
    if event.source == "seoul":
        outside = any(w in event.place for w in OUTDOOR_PLACE) and not any(w in event.place for w in INDOOR_PLACE)
        return outside or event.category not in INDOOR_SEOUL
    return not any(w in event.place for w in INDOOR_WORDS)


def open_days(event: Event, days: list[date]) -> list[date]:
    """The weekend days the event runs and, by its stated hours, opens: a weekday-only venue is not a Saturday, and a
    day the hours name as closed is not offered (the card of 2026-11-19 recommended a Sunday for a venue whose hours
    read "일, 월 및 한글날 휴무")."""
    weekdays_only = bool(WEEKDAYS_ONLY.search(event.hours)) and not WEEKEND_OPEN.search(event.hours)
    closed, listed = read_hours(event.hours)
    return [d for d in days if event.start <= d <= event.end and not (weekdays_only and d.weekday() >= 5)
            and d.weekday() not in closed and (not listed or d.weekday() in listed)]


# The hours text ("화-금 10:30~20:00 / 토 10:30~19:00", "9:00~18:00 / 일, 월 및 한글날 휴무") read token by token in one
# pass: every alternative matches a fixed short piece, so the time is linear in the text (Codex review 2026-11-19: the
# first version's nested pattern took a second to reject eighteen repeated day names). A day name stands alone: the 일
# of 공휴일, 매일 or "1일" is not Sunday.
_HOURS_TOKEN = re.compile(
    # A week of the month, in words or figures ("첫째", "2, 4주", "매월"): what follows holds on some weeks only.
    r"(?P<nth>첫|둘째|셋째|넷째|다섯째|마지막|격주|짝수|홀수|매월|격월|\d{1,2}\s?(?:째|번째)?\s?주(?!말))"
    r"|(?P<range>[-~])"
    r"|(?P<time>\d{1,2}\s?(?::|시))"
    r"|(?P<close>휴무|휴관|휴장|휴원|쉽니다|쉼)"
    r"|(?P<negated>없음|없이|없습니다|없어|없는|아님)"
    r"|(?P<open>운영|개관|오픈|관람|공연|상영|개방)"
    r"|(?P<every>매일|연중|상시|무휴)"
    r"|(?P<weekend>(?<![가-힣])주말)"
    r"|(?P<day>(?<![\d가-힣])(?:[월화수목금토일]요일(?:은|는|이|가|에|도|만)?|[월화수목금토일](?![가-힣])))"
    r"|(?P<sep>[,·]|및|과|와|그리고)"
    r"|(?P<half>오전|오후|낮|저녁|밤)"
    r"|(?P<stop>[./|;\n])"
    r"|(?P<space>[\s()\[\]]+)"
    r"|(?P<digit>\d+)"
    r"|(?P<word>.)", re.S)
MAX_HOURS_CHARS = 200   # More than any real opening-hours line; the rest is not read.
CLOSURE_REACH = 12      # Plain characters allowed between a list of days and its closure word ("및 한글날").


def read_hours(hours: str) -> tuple[set[int], set[int]]:
    """(closed, listed): the weekdays (0 = Monday) the hours text closes, and those it gives opening times for.

    A list or a range of days ("일, 월", "토요일과 일요일", "토~일", "주말") right before a closure word, with at most a
    few plain words between, is closed. A list or a range right before a time, or a range before an opening word, is
    listed. `listed` is empty, limiting nothing, when the text names no day, says every day, or names a day this
    reader does not understand: a wrongly removed open day is worse than a missed closure, so a part-read list never
    decides (Codex review). For the same reason a clause with a week of the month ("매월 첫째 일요일 휴관", "2, 4주
    일요일 휴관") and a negated closure ("일요일 휴무 없음") close nothing."""
    tokens = [(m.lastgroup, m.group()) for m in _HOURS_TOKEN.finditer(hours[:MAX_HOURS_CHARS]) if m.lastgroup != "space"]
    closed: set[int] = set()
    listed: set[int] = set()
    loose = qualified = False
    i = 0
    while i < len(tokens):
        kind, word = tokens[i]
        if kind in ("stop", "nth", "every"):
            qualified = kind == "nth" or (qualified and kind != "stop")
            loose = loose or kind == "every"
            i += 1
            continue
        if kind not in ("day", "weekend"):
            i += 1
            continue
        run: set[int] = set()
        ranged, last, j = False, None, i
        while j < len(tokens):
            kind, word = tokens[j]
            following = tokens[j + 1][0] if j + 1 < len(tokens) else ""
            if kind == "day":
                last = WEEKDAYS_KO.index(word[0])
                run.add(last)
            elif kind == "weekend":
                last = None
                run |= {5, 6}
            elif kind == "sep" and following in ("day", "weekend"):
                pass
            elif kind == "range" and last is not None and following == "day":
                until = WEEKDAYS_KO.index(tokens[j + 1][1][0])
                run |= {(last + step) % 7 for step in range((until - last) % 7 + 1)}
                ranged, last, j = True, until, j + 1
            else:
                break
            j += 1
        after = j + 1 if j < len(tokens) and tokens[j][0] == "half" else j
        reach = j
        while reach < len(tokens) and reach - j < CLOSURE_REACH and tokens[reach][0] in ("word", "sep"):
            reach += 1
        follows = tokens[after][0] if after < len(tokens) else ""
        closes = reach < len(tokens) and tokens[reach][0] == "close"
        negated = closes and reach + 1 < len(tokens) and tokens[reach + 1][0] == "negated"
        if qualified or negated:
            loose = True
        elif follows == "time":
            listed |= run
        elif closes:
            closed |= run
            j = reach + 1
        elif ranged and follows in ("open", "stop", ""):
            listed |= run
        else:
            loose = True
        i = max(j, i + 1)
    return closed, set() if loose else listed


def closed_weekdays(hours: str) -> set[int]:
    """The weekdays (0 = Monday) the hours text names as closed."""
    return read_hours(hours)[0]


def listed_weekdays(hours: str) -> set[int]:
    """The weekdays (0 = Monday) the hours text gives opening times for; empty limits nothing (see `read_hours`)."""
    return read_hours(hours)[1]


def for_kids(event: Event) -> bool:
    title = event.title
    return any(w in title for w in KID_TITLE) or bool(KID_AUDIENCE.search(event.audience.strip()))


# ------------------------------------------------------------------------------------------------------------- range

@dataclass(frozen=True)
class Range:
    provinces: tuple[str, ...]     # "서울", "경기", "인천" read whole
    cities: tuple[str, ...]        # "고양시", "구리시", "강남구": an event's district must start with one

    @property
    def label(self) -> str:
        return ", ".join([*(f"{p} 전역" for p in self.provinces), *self.cities])

    def contains(self, event: Event) -> bool:
        if event.source == "seoul":
            return "서울" in self.provinces or event.district in self.cities
        province = "서울" if event.place.startswith("서울") else "경기" if event.place.startswith("경기") else \
            "인천" if event.place.startswith("인천") else ""
        if province and province in self.provinces:
            return True
        return any(event.district == c or event.district.startswith(c + " ") for c in self.cities)


def area_range(default_areas: list[str], asked: str | None = None) -> Range:
    """The range: a 구·시·군 the question names, else the configured default (whole provinces and cities)."""
    names = [asked] if asked and asked[-1:] in ("구", "시", "군") else list(default_areas) or ["서울"]
    provinces = tuple(n for n in names if n in ("서울", "경기", "인천"))
    return Range(provinces, tuple(n for n in names if n not in provinces))


def provinces_to_read(scope: Range) -> list[str]:
    """TourAPI province codes worth asking: the whole provinces, and for named cities Seoul's (a 구) or the
    capital-area provinces around it."""
    from smith.outing_sources import PROVINCES
    wanted = {PROVINCES[p] for p in scope.provinces}
    for city in scope.cities:
        wanted |= {PROVINCES["서울"]} if city.endswith("구") else {PROVINCES["경기"], PROVINCES["인천"]}
    return sorted(wanted)


# ----------------------------------------------------------------------------------------------------------- scoring

def rank(events: list[Event], *, days: list[date], weather: list[DayWeather], taste: Taste, scope: Range,
         home: tuple[float, float] | None, airways_sensitive: bool, top: int = 3,
         recent: set[str] = frozenset()) -> Outcome:
    """Filter and score. `recent` holds pick keys sent within REPEAT_DAYS (never repeated)."""
    out = Outcome(days=days, weather=weather, range_label=scope.label, taste=taste)
    by_day = {w.day: w for w in weather}
    seen: dict[str, Event] = {}
    for event in events:
        key = dedupe_key(event.title)
        if key in seen and seen[key].source == "seoul":
            continue                                    # Seoul's row carries fee and hours; keep it.
        seen[key] = event
    out.considered = len(seen)
    scored: list[Pick] = []
    for event in seen.values():
        why = _ruled_out(event, days, scope, recent)
        if why:
            out.ruled_out[why] = out.ruled_out.get(why, 0) + 1
            continue
        pick = _score(event, days, by_day, taste, home, airways_sensitive)
        if pick is None:
            out.ruled_out["날씨가 맞지 않는 바깥 행사"] = out.ruled_out.get("날씨가 맞지 않는 바깥 행사", 0) + 1
            continue
        scored.append(pick)
    scored.sort(key=lambda p: (-p.score, p.km if p.km is not None else 999, p.event.title))
    out.picks = _varied(scored, top)
    return out


def dedupe_key(title: str) -> str:
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", title)
    return re.sub(r"[^0-9a-z가-힣]", "", text.lower())


def pick_key(event: Event) -> str:
    return f"{event.source}:{event.id}"


def _ruled_out(event: Event, days: list[date], scope: Range, recent: set[str]) -> str | None:
    if not scope.contains(event):
        return "범위 밖"
    if not any(event.start <= d <= event.end for d in days):
        return "이번 주말에 열리지 않음"
    if not open_days(event, days):
        return "운영 시간이 평일뿐"
    if any(w in event.title for w in SKIP_TITLE):
        return "모집·공모 공고"
    # The same event read from the other source next time is still the same event (Codex review 2026-11-18).
    if pick_key(event) in recent or f"title:{dedupe_key(event.title)}" in recent:
        return f"최근 {REPEAT_DAYS}일 안에 추천함"
    return None


def _score(event: Event, days: list[date], by_day: dict[date, DayWeather], taste: Taste,
           home: tuple[float, float] | None, airways_sensitive: bool) -> Pick | None:
    is_outdoor = outdoor(event)
    fits: list[tuple[float, date]] = []
    for day in open_days(event, days):
        w = by_day.get(day)
        if not is_outdoor:
            fits.append((1.0, day))
            continue
        grade = _grade(w.air if w else None)
        if grade is not None and grade >= BAD_AIR and airways_sensitive:
            continue                                     # Not outside on a 나쁨 day for weak airways.
        fit = UNKNOWN_SKY if w is None or w.pop is None else (WET_OUTDOOR if w.wet else 1.0)
        if grade is not None and grade >= BAD_AIR:
            fit *= BAD_AIR_OUTDOOR
        if w is not None and w.tmax is not None and w.tmax < weather.COLD_MIN:
            fit *= COLD_OUTDOOR
        fits.append((fit, day))
    if not fits:
        return None
    fit, day = max(fits, key=lambda f: (f[0], -days.index(f[1])))
    tags = event_tags(event)
    liked = [t for t in tags if taste.weight(t) >= LIKED]
    tags = tuple(liked + [t for t in tags if t not in liked])   # The lead tag (label, variety) is a liked one.
    taste_factor = max([taste.weight(t) for t in tags] or [NEUTRAL]) + EXTRA_LIKED * max(0, len(liked) - 1)
    km = _km(home, event)
    distance = UNKNOWN_DISTANCE if km is None else 1 / (1 + km / DISTANCE_KM)
    span = (event.end - event.start).days
    ending = 3 <= span < STANDING_DAYS and event.end <= days[-1] + timedelta(days=ENDING_DAYS)
    standing = span >= STANDING_DAYS
    kids = for_kids(event)
    score = (taste_factor * fit * distance * (ENDING if ending else 1.0) * (KIDS if kids else 1.0)
             * (STANDING if standing else 1.0))
    return Pick(event, round(score, 4), day, tags, is_outdoor, km,
                tuple(_reasons(event, liked, is_outdoor, day, by_day, days, km, ending, kids, standing)), kids)


def _reasons(event: Event, liked: list[str], is_outdoor: bool, day: date, by_day: dict[date, DayWeather],
             days: list[date], km: float | None, ending: bool, kids: bool, standing: bool = False) -> list[str]:
    lines = []
    if liked:
        lines.append("좋아하시는 " + "·".join(TAGS[t][0] for t in liked[:2]))
    w = by_day.get(day)
    weekday = WEEKDAYS_KO[day.weekday()]
    if is_outdoor:
        if w and w.pop is not None and not w.wet:
            cold = w.tmax is not None and w.tmax < weather.COLD_MIN
            lines.append(f"바깥 행사, {weekday}요일 {w.sky or '비 소식 없음'}"
                         + (f"이지만 쌀쌀함(낮 최고 {w.tmax:.0f}도)" if cold else ""))
        elif w and w.wet:
            lines.append(f"바깥 행사인데 {weekday}요일에도 비 소식(강수확률 {w.pop}%)")
        else:
            lines.append("바깥 행사, 날씨는 아직 예보 전")
    else:
        wet = [d for d in days if by_day.get(d) and by_day[d].wet]
        lines.append(f"실내라 {WEEKDAYS_KO[wet[0].weekday()]}요일 비와 상관없음" if wet else "실내")
    if km is not None:
        lines.append(f"집에서 약 {km:.0f}km")
    if ending:
        lines.append(f"{event.end.month}/{event.end.day}에 끝나 이번이 거의 마지막")
    if standing:
        lines.append("늘 하는 상설 행사")
    if kids:
        lines.append("어린이 대상일 수 있음")
    return lines


def _varied(scored: list[Pick], top: int) -> list[Pick]:
    """The best `top`, at most one per leading tag while there are enough others (an exhibition, a festival and a
    concert rather than three exhibitions)."""
    chosen: list[Pick] = []
    used: set[str] = set()
    for pick in scored:
        lead = pick.tags[0] if pick.tags else "other"
        if lead in used:
            continue
        chosen.append(pick)
        used.add(lead)
        if len(chosen) == top:
            return chosen
    for pick in scored:
        if pick not in chosen:
            chosen.append(pick)
            if len(chosen) == top:
                break
    return chosen


def _grade(word: str | None) -> int | None:
    return {"좋음": 1, "보통": 2, "나쁨": 3, "매우나쁨": 4}.get(word or "")


def _km(home: tuple[float, float] | None, event: Event) -> float | None:
    if home is None or event.lat is None or event.lon is None:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (home[0], home[1], event.lat, event.lon))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


# ----------------------------------------------------------------------------------------------------------- history

def history_path(db: Path) -> Path:
    return Path(db).parent / "outing" / "history.json"


def recent_keys(path: Path, today: date) -> set[str]:
    """Pick keys sent within REPEAT_DAYS, and "title:<dedupe key>" for each (the same event from the other source).
    Raises OutingUnavailable("history-unreadable") on a damaged file."""
    recent = [e for e in _read_history(path) if (today - date.fromisoformat(e["on"])).days < REPEAT_DAYS]
    return {e["key"] for e in recent} | {f"title:{e['match']}" for e in recent if isinstance(e.get("match"), str) and e["match"]}


def sent_entries(picks: list[Pick]) -> list[dict[str, str]]:
    return [{"key": pick_key(p.event), "title": p.event.title} for p in picks]


def remember(path: Path, sent: list[dict[str, str]], today: date) -> None:
    """Record sent picks ({"key", "title"}: the evening card, a counsel answer); entries older than HISTORY_KEEP_DAYS
    are dropped. Raises OutingUnavailable on a damaged file (it is not overwritten) and OSError."""
    entries = [e for e in _read_history(path) if (today - date.fromisoformat(e["on"])).days < HISTORY_KEEP_DAYS]
    entries += [{"key": s["key"], "title": s["title"][:60], "match": dedupe_key(s["title"]), "on": today.isoformat()}
                for s in sent]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


def _read_history(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = [e for e in data if isinstance(e, dict) and isinstance(e.get("key"), str)]
        for e in entries:
            date.fromisoformat(e["on"])
        return entries
    except (OSError, ValueError, TypeError, KeyError):
        raise OutingUnavailable("history-unreadable") from None


# ------------------------------------------------------------------------------------------------------------ output

# Every reader (CLI, Thursday's card, a counsel letter) gets these lines from the dict form (`to_dict`).
SOURCE_LINE = ("출처: 한국관광공사 TourAPI, 서울시 문화행사 정보(서울 열린데이터광장), 기상청·에어코리아 예보. "
               "일정 변경·휴관은 가기 전 확인하세요.")


def to_dict(outcome: Outcome) -> dict[str, Any]:
    return {
        "days": [d.isoformat() for d in outcome.days], "range": outcome.range_label,
        "weather": [{"day": w.day.isoformat(), "sky": w.sky, "pop": w.pop, "tmin": w.tmin, "tmax": w.tmax,
                     "air": w.air, "air_failed": "air" in outcome.failures, "source": w.source} for w in outcome.weather],
        "taste": [TAGS[t][0] for t in outcome.taste.liked()] if outcome.taste else [],
        "picks": [{"title": p.event.title, "source": p.event.source, "id": p.event.id, "start": p.event.start.isoformat(),
                   "end": p.event.end.isoformat(), "place": p.event.place, "district": p.event.district,
                   "fee": p.event.fee, "hours": p.event.hours, "link": p.event.link, "day": p.day.isoformat(),
                   "kind": TAGS[p.tags[0]][0] if p.tags else "행사", "tags": list(p.tags), "outdoor": p.outdoor,
                   "km": None if p.km is None else round(p.km, 1), "score": p.score, "reasons": list(p.reasons)}
                  for p in outcome.picks],
        "considered": outcome.considered, "ruled_out": outcome.ruled_out, "failures": outcome.failures,
        "notes": outcome.notes,
    }


def weather_line(w: dict[str, Any]) -> str:
    day = date.fromisoformat(w["day"])
    head = f"{WEEKDAYS_KO[day.weekday()]}({day.month}/{day.day})"
    if w.get("pop") is None and not w.get("sky"):
        return f"{head} 아직 예보 없음" + (", 미세먼지 못 읽음" if w.get("air_failed") else "")
    parts = [w.get("sky") or "하늘 미상"]
    if w.get("pop") is not None:
        parts.append(f"비 {w['pop']}%")
    if w.get("tmin") is not None and w.get("tmax") is not None:
        parts.append(f"{w['tmin']:.0f}~{w['tmax']:.0f}도")
    parts.append(f"미세먼지 {w['air']}" if w.get("air") else "미세먼지 못 읽음" if w.get("air_failed") else "미세먼지 예보 전")
    return f"{head} " + ", ".join(parts)


def summary_lines(found: dict[str, Any]) -> list[str]:
    """The weekend, its weather and the taste the picks were fitted to."""
    days = [date.fromisoformat(d) for d in found["days"]]
    lines = [f"{days_label(days)} · 범위 {found['range']}"]
    if found.get("weather"):
        lines.append("날씨: " + " / ".join(weather_line(w) for w in found["weather"]) + f" ({found['weather'][0]['source']})")
    lines.append("맞춘 취향: " + ", ".join(found["taste"]) + " (Smith가 기억하는 말에서)" if found.get("taste")
                 else "맞춘 취향: 아직 기억된 취향이 없어 고르게 봤습니다.")
    return lines


def pick_detail(pick: dict[str, Any]) -> list[str]:
    """The lines under a pick's title: where and when, why, hours, the page to check."""
    start, end, day = (date.fromisoformat(pick[k]) for k in ("start", "end", "day"))
    when = f"{start.month}/{start.day}" if start == end else f"{start.month}/{start.day}~{end.month}/{end.day}"
    where = " · ".join(x for x in (pick["place"] if pick["source"] == "seoul" else "", pick["district"]) if x)
    lines = [f"[{pick['kind']}] {where} · {when}" + (f" · {pick['fee']}" if pick.get("fee") else ""),
             f"추천 날: {WEEKDAYS_KO[day.weekday()]}요일 · " + " · ".join(pick["reasons"])]
    if pick.get("hours"):
        lines.append(f"시간: {pick['hours']}")
    return lines


def render(outcome: Outcome, now: datetime) -> str:
    found = to_dict(outcome)
    lines = [f"이번 주말 나들이 추천 · {summary_lines(found)[0]}", *summary_lines(found)[1:]]
    if not found["picks"]:
        lines.append("이번 주말에 범위 안에서 추천할 행사를 찾지 못했습니다.")
    for n, pick in enumerate(found["picks"], 1):
        lines += ["", f"{n}. {pick['title']}", *(f"   {line}" for line in pick_detail(pick))]
        if pick.get("link"):
            lines.append(f"   가기 전 확인: {pick['link']}")
    lines += ["", f"본 행사 {outcome.considered}건" + (", 뺀 것: " + "; ".join(f"{k} {v}건" for k, v in outcome.ruled_out.items())
                                                     if outcome.ruled_out else "")]
    lines += outcome.notes
    lines.append(f"{SOURCE_LINE} {now.astimezone().strftime('%Y-%m-%d %H:%M')}에 읽음.")
    return "\n".join(lines)
