"""Readable Korean money and rate formats shared by the report and its proposals."""
from decimal import Decimal


def short_won(amount: Decimal | None) -> str:
    """Readable won: 27.3억 원, 4,120만 원, 85,000원. Unknown amounts read 미상, never 0."""
    if amount is None:
        return "미상"
    value = abs(amount)
    sign = "-" if amount < 0 else ""
    if value >= 100_000_000:
        return f"{sign}{value / 100_000_000:,.1f}억 원"
    if value >= 10_000:
        return f"{sign}{value / 10_000:,.0f}만 원"
    return f"{sign}{value:,.0f}원"


def percent(rate: Decimal | None, places: int = 2) -> str:
    """A fraction as a percentage: 0.0361 -> 3.61%."""
    return "미상" if rate is None else f"{rate * 100:.{places}f}%"


# The particle after a word depends on whether its last syllable closes with a consonant: (after a consonant, after a
# vowel). 로 also follows ㄹ (서울로, 1로). The letters of 2026-11 read "삼성전자은" and "150만 원로" from fixed particles.
_PARTICLES = {"은는": ("은", "는"), "이가": ("이", "가"), "을를": ("을", "를"), "과와": ("과", "와"), "로": ("으로", "로")}
_RIEUL = 8
# How a trailing digit or Latin letter is read: 영 일 삼 육 칠 팔 close with a consonant (일 칠 팔 with ㄹ), and so do the
# letter names 엘 엠 엔 알.
_DIGIT_FINALS = {"0": 21, "1": _RIEUL, "3": 16, "6": 1, "7": _RIEUL, "8": _RIEUL}
_LETTER_FINALS = {"l": _RIEUL, "r": _RIEUL, "m": 16, "n": 4}


def particle(word: str, pair: str) -> str:
    """The form of the particle `pair` ("은는", "이가", "을를", "과와", "로") that follows `word`, read by its last
    sound; closing brackets and quotes are skipped ("대출(본인)" ends in 인). A word with no readable end takes the form
    after a vowel."""
    after_consonant, after_vowel = _PARTICLES[pair]
    last = next((ch for ch in reversed(word) if ch.isalnum()), "")
    if "가" <= last <= "힣":
        final = (ord(last) - 0xAC00) % 28
    else:
        final = _DIGIT_FINALS.get(last, 0) if last.isdigit() else _LETTER_FINALS.get(last.lower(), 0)
    return after_vowel if final == 0 or (pair == "로" and final == _RIEUL) else after_consonant


def josa(word: str, pair: str) -> str:
    """`word` followed by the right form of the particle: josa("삼성전자", "은는") -> "삼성전자는"."""
    return word + particle(word, pair)
