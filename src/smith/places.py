"""The place a restaurant question or a calendar entry names (FR-55), found in code from public place names only.

Only such a name ever reaches Naver ("연신내역 맛집"); the text's other words never do, since a question or an event's
location may hold a person's name or a private matter (Codex review 2026-11-16, P0). Two sources of names:

- `KNOWN`: areas people meet in that are not administrative names (성수, 홍대, 가로수길), venues (블루스퀘어, DDP) and
  day-trip spots, written by hand and matched as a word of their own;
- `places_kr.txt`: every 읍·면·동, 구·군 and 시 in the country and every urban and metropolitan railway station, built
  by `scripts/build_places.py` from public data (BUG-2026-007: a hand list of about 200 names missed the station a
  question named, and the question was then searched without a place, which Naver answered around central Seoul).

A full name ("연신내역", "불광동", "은평구") counts as it stands. A bare stem ("연신내", "불광") and a two-letter name
("중동", "이동") also read as everyday words or given names, so they count only with a place word or a word of going
right after them ("연신내 맛집", "불광에서는", "중동 근처", "녹번 가서"), never across a sentence, never when the
address book holds that name, and never over a full name elsewhere in the text (Codex review 2026-11-16, P0: "친구
이름은 서현. 파스타 …" must not send 서현). A bare station name is otherwise offered back as a guess (`suggest`), never
searched. Matching is per word with up to three particles taken off, well under a millisecond a text; the dictionary
(about 4,900 names) loads once.
"""
import difflib
import re
from collections.abc import Collection
from dataclasses import dataclass
from functools import cache
from pathlib import Path

DICTIONARY = Path(__file__).with_name("places_kr.txt")
# Areas and venues a calendar entry may name. Longer names first where one contains another (홍대입구 before 홍대).
# Left out on purpose: names that are also everyday words or given names (미사, 수지, 서현, 교대, 전주, 수유, 상수);
# the dictionary still finds them in a full form (서현역, 수지구) or beside a place word.
KNOWN = (
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
    "김포", "부천", "평촌", "안양", "동탄", "강릉", "속초", "양양", "해운대", "광안리", "부산", "서귀포", "제주도", "제주",
    "강화도", "울릉도", "경주", "여수",
)
# KNOWN names that stand on their own (a venue, a landmark); the other bare ones ("성수", "광화문") give way to a full
# place name elsewhere in the text, as a dictionary stem does (Codex review 2026-11-16, round 4).
_STANDALONE = frozenset(("예술의전당", "세종문화회관", "블루스퀘어", "샤롯데씨어터", "디큐브아트센터", "충무아트센터", "국립극장",
                         "국립중앙박물관", "국립현대미술관", "리움", "고척스카이돔", "고척돔", "잠실야구장", "잠실종합운동장",
                         "서울월드컵경기장", "월드컵경기장", "장충체육관", "동대문디자인플라자", "DDP", "코엑스", "더현대 서울",
                         "더현대서울", "롯데월드몰", "롯데월드", "타임스퀘어", "킨텍스", "스타필드 하남", "스타필드 고양",
                         "스타필드 수원", "서울대공원", "서울랜드", "경복궁", "봉은사", "서울숲", "에버랜드", "캐리비안베이",
                         "한국민속촌", "남이섬", "헤이리"))
_WHOLE_ENDINGS = ("역", "동", "길", "로데오", "시장", "공원", "호수", "마을", "도", "입구", "공항", "터미널")
# A KNOWN name standing as a word of its own (a Hangul letter right before it or after its suffix means another
# word). At one position the alternatives are tried in KNOWN order, so 강남역 wins over 강남, and 잠실 backtracks to
# 잠실야구장 when a letter follows.
_KNOWN = re.compile(r"(?<![가-힣])(" + "|".join(re.escape(name) for name in KNOWN)
                    + r")(?:역|동|점|에서|에|쪽|앞|근처)?(?:은|는|도)?(?![가-힣])", re.IGNORECASE)
_CANONICAL = {name.lower(): name for name in KNOWN}
_SIDO = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "제주")   # Metropolitan cities, said bare.
# Specificity: the most specific place in a text wins ("서울 은평구 불광동" -> 불광동), then the earliest.
_RANK = {"station": 1, "dong": 1, "district": 2, "city": 3, "sido": 4}
_WORD = re.compile(r"[0-9A-Za-z가-힣]+")
# What makes a bare stem a place: a place word or a word of going right after it ("연신내 맛집", "녹번 가서"), or a
# place particle joined to it ("연신내에서", "불광근처"). A food word does not ("서현 파스타" may be about a person),
# nor does a word of coming ("서현 와서" is a person coming).
PLACE_WORDS = ("맛집", "근처", "주변", "부근", "인근", "쪽")
_GOING = ("가서", "가면", "가는", "가자", "가려", "갈까", "갈래")
_LOCATIVE = ("에서", "에선", "에", "엔", "으로", "로", "까지", "쯤", *PLACE_WORDS)
# Particles that also follow a person's name ("서현이랑", "서현에게", "서현으로부터"): taken off, but they make nothing
# a place.
_OTHER = ("으로부터", "로부터", "에게서", "한테서", "이랑", "랑", "하고", "에게", "한테", "께서", "께", "부터", "이나", "나", "은",
          "는", "이", "가", "을", "를", "도", "만", "의", "과", "와", "보다", "처럼", "인데", "이야", "야")
_SUFFIXES = sorted(set(_LOCATIVE + _OTHER), key=len, reverse=True)
_ABOUT = ("대해", "대한", "관해", "관한", "따르면", "의하면", "비해", "비하면")   # "서현에 대해" is about a person.
_NOT_PLACE = ("내역", "지역", "영역", "구역", "전역", "무역", "번역", "통역")   # Words ending like a station name.
MAX_SUGGESTIONS = 3
SUGGEST_CUTOFF = 0.8        # Jamo similarity: 구파밸역 / 구파발역 0.89; a shared syllable or two stays below.


@dataclass(frozen=True)
class _Names:
    full: dict[str, str]                 # name as written in full -> its kind: "연신내역" -> "station", "불광동" -> "dong"
    stems: dict[str, str]                # a bare stem -> its most specific kind: "연신내" -> "station", "불광" -> "station"


@cache
def _names() -> _Names:
    full: dict[str, str] = {}
    stems: dict[str, str] = {name: "sido" for name in _SIDO}
    for line in DICTIONARY.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        kind, name = line.split("\t")
        whole = name + "역" if kind == "station" else name
        stem = name if kind == "station" else name[:-1] if name[-1] in "동읍면구군시" and len(name) >= 3 else None
        for table, key in ((full, whole), (stems, stem)):
            if key and (key not in table or _RANK[kind] < _RANK[table[key]]):
                table[key] = kind
    return _Names(full=full, stems=stems)


@dataclass(frozen=True)
class _Hit:
    name: str
    bare: bool          # A stem or a two-letter name: any full name in the text wins over it.
    rank: int
    at: int


def find(text: str, *, people: Collection[str] = ()) -> str | None:
    """The most specific place `text` names, as it goes to Naver ("연신내역", "불광동", "성수"), or None. `people`:
    the address book's names (`contacts.blank_words`); the text they cover is never read as a place."""
    hits = _hits(without(text, people))[0]
    return min(hits, key=lambda h: (h.bare, h.rank, h.at)).name if hits else None


def mentions_place(text: str, *, people: Collection[str] = ()) -> bool:
    """Whether the text names a place at all, read or not: one `find` gives, one `suggest` can guess, or a word shaped
    like a station or 동 the dictionary does not hold ("김천구미역", "정동진역"). A thread follow-up with none of these
    ("고기집은 없어?") may keep the earlier place; one with any of them asked about a place of its own."""
    names = _names()
    hits, near = _hits(without(text, people))
    unknown = any(word not in names.full and word not in names.stems for word, _ in near)
    return bool(hits) or unknown or bool(suggest(text, people=people))


def without(text: str, people: Collection[str]) -> str:
    """The text with every address-book name blanked out before any place is looked for, so neither the hand list nor
    the dictionary can read a place inside a person's name ("성수동은 파스타 좋아해" with a contact 성수동, "서현 근처"
    with 김서현; Codex review 2026-11-16, rounds 1-3). One exception: the name followed by 역·동·구·시·군·읍·면 that
    makes a different, full place name ("서현역" with a friend 서현) is the place."""
    full = _names().full
    chars = list(text)
    for name in sorted((p for p in people if len(p) >= 2), key=len, reverse=True):
        for match in re.finditer(re.escape(name), text, re.IGNORECASE):
            longer = text[match.start():match.end() + 1]
            if len(longer) > len(name) and longer[-1] in "역동구시군읍면" and longer in full and longer not in people:
                continue
            chars[match.start():match.end()] = "□" * len(name)
    return "".join(chars)


def suggest(text: str, *, people: Collection[str] = ()) -> list[str]:
    """Places the text may have meant, for a reply that found none: a station's name written bare ("구파발 다녀와서"
    -> 구파발역), or the names nearest to a station- or dong-like word that is not one ("구파밸역" -> 구파발역). Other
    bare names stay unguessed: "이동 중에" or "운동 끝나고" are not about 이동면 or 운동동."""
    names = _names()
    found: list[str] = []
    for word, suffix in _hits(without(text, people))[1]:
        if names.full.get(word + "역") == "station":
            found += [word + "역"] if not suffix else []
        elif word not in names.full and word not in names.stems:
            pool = [w for w in names.full if w[-1] == word[-1] and abs(len(w) - len(word)) <= 1]
            scored = sorted(((_alike(word, w), w) for w in pool), reverse=True)
            found += [w for score, w in scored[:2] if score >= SUGGEST_CUTOFF]
    return list(dict.fromkeys(found))[:MAX_SUGGESTIONS]


def _hits(text: str) -> tuple[list[_Hit], list[tuple[str, str]]]:
    """(places named, (word, suffix) readings that look like a place without counting as one)."""
    names = _names()
    hits = []
    for match in _KNOWN.finditer(text):
        name = _CANONICAL.get(match.group(1).lower(), match.group(1))
        bare = not (name in _STANDALONE or name in names.full or name.endswith(_WHOLE_ENDINGS))
        hits.append(_Hit(name, bare, _known_rank(name, names.full), match.start()))
    near: list[tuple[str, str]] = []
    words = list(_WORD.finditer(text))
    for i, match in enumerate(words):
        # Only the word right after counts, across spaces and nothing else: "서현. 파스타" is two sentences.
        following = words[i + 1].group() if i + 1 < len(words) and not text[match.end():words[i + 1].start()].strip() else ""
        for word, suffix in _readings(match.group()):
            if word in names.full and len(word) >= 3:
                hits.append(_Hit(word, False, _RANK[names.full[word]], match.start()))
                break
            if word in names.full or (word in names.stems and len(word) >= 2):
                if (suffix in _LOCATIVE and not following.startswith(_ABOUT)) \
                        or (not suffix and following.startswith((*PLACE_WORDS, *_GOING))):
                    hits.append(_Hit(word, True, _RANK[names.full.get(word) or names.stems[word]], match.start()))
                else:
                    near.append((word, suffix))
                break
            # A station- or 동-shaped word the dictionary lacks, whatever particle follows ("김천구미역은"; Codex round 5).
            if len(word) >= 3 and word.endswith(("역", "동")) and not word.endswith(_NOT_PLACE):
                near.append((word, suffix))
    return hits, near


def _known_rank(name: str, full: dict[str, str]) -> int:
    """A hand-listed name ranks as the administrative area it also is (Codex review 2026-11-16, P2): 분당 as 분당구,
    수원 as 수원시, 부산 as a metropolitan city, so "부천 송내역" picks the station; neighbourhoods and venues as a 동."""
    if name in _SIDO:
        return _RANK["sido"]
    if full.get(name + "시") == "city":
        return _RANK["city"]
    if full.get(name + "구") == "district" or full.get(name + "군") == "district":
        return _RANK["district"]
    return _RANK["dong"]


def _readings(word: str) -> list[tuple[str, str]]:
    """The word as written, then with one, two and three particles or place words taken off, each as (stem, the
    suffix right after the stem): "연신내역에서는" -> ("연신내역에서", "는"), then ("연신내역", "에서")."""
    out = level = [(word, "")]
    for _ in range(3):
        level = [(rest[:-len(s)], s) for rest, _ in level for s in _SUFFIXES if rest.endswith(s) and len(rest) > len(s)]
        out = out + level
    return out


def _alike(a: str, b: str) -> float:
    """Similarity letter by letter (jamo), so a typo inside a syllable counts as a near miss: 구파밸역 / 구파발역."""
    return difflib.SequenceMatcher(None, _jamo(a), _jamo(b)).ratio()


def _jamo(text: str) -> str:
    out = []
    for char in text:
        code = ord(char) - 0xAC00
        if 0 <= code < 11172:
            out += [chr(0x1100 + code // 588), chr(0x1161 + code % 588 // 28)] + ([chr(0x11A7 + code % 28)] if code % 28 else [])
        else:
            out.append(char)
    return "".join(out)
