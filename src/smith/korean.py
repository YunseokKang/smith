"""Natural Korean in the client's prose (report design §34; user instruction 2026-10-27).

The reports, letters and answers are read by one Korean reader, and a model writing Korean slips into translated
English: double passives, English word order, filler openers, a doubled hedge, developer slang. An editor strikes
these. Two kinds, by what code can do safely:

- `tidy`: forms with exactly one right spelling are fixed in place without changing meaning, like `narrative.polish`
  (a double passive, a dash used as punctuation). Applied to every prose field of every writer.
- `problem`: patterns that need rewording are named, and the writer rewrites the block once (the report's repair
  call); a block still carrying one after that is kept as it is, without a memo: it is readable, merely not as good as
  it could be. The daily mail and the health letter have no repair call, so for them the patterns are prompt rules.

What no regex can catch (a metaphor translated word for word, an abstract subject where the actor could be named, a
pronoun without a referent, a nominal ending where a verb would do) is a rule in the writers' prompts (`RULES`) and a
problem kind of the first reader (`reader.PROBLEMS`, "unnatural"). The source list has per-document allowances for
some patterns; a block here is a few sentences, so those patterns are prompt rules and the ones with no allowance are
checked.

Every correction is recorded (user request 2026-10-27), so the corrections can be read against the reports they were
made in: `tidy` appends what it changed to `found`, the report keeps what was flagged, what stayed and what the first
reader marked (`narrative.korean_*`), and `summary` writes the one line the mail's footer and `smith report korean` show.
"""
import re
from collections import Counter
from typing import Any

# A double passive (이중 피동): a passive verb with 지다 on top. The verb's passive prefix, its single stem and that
# stem with 어: 판단되어진다 -> 판단된다, 보여집니다 -> 보입니다, 되어졌습니다 -> 되었습니다. The syllable after the
# prefix is 지 or 져 with any final consonant (집니다, 진, 질, 졌); that consonant moves onto the single stem.
_PASSIVES = {"되어": ("되", "되어"), "보여": ("보이", "보여"), "쓰여": ("쓰이", "쓰여"), "나뉘어": ("나뉘", "나뉘어"),
             "모여": ("모이", "모여"), "잊혀": ("잊히", "잊혀")}
_DOUBLE_PASSIVE = re.compile("(" + "|".join(sorted(_PASSIVES, key=len, reverse=True)) + r")([가-힣])")
_HANGUL, _INITIALS, _FINALS = 0xAC00, 21 * 28, 28
_JIEUT, _I, _YEO = 12, 20, 6  # The initial ㅈ and the medials ㅣ and ㅕ of 지 and 져.
# A dash between two numbers is a range (3~5%); a dash right before a number that stands apart from the word before
# it (after a space, a bracket or a range mark) is the number's sign (-3도); glued to the word before it
# (같습니다—3.4%) it is a separator, like any other em dash, en dash or horizontal bar, and becomes a comma. The
# number check reads the text in this tidied form, so a sign is checked as a sign (Codex reviews 2026-10-27).
_DASH = "[–—―]"
_RANGE_DASH = re.compile(rf"(?<=\d)\s*{_DASH}\s*(?=\d)")
_SIGN_DASH = re.compile(rf"(?<![^\s(\[~,:/]){_DASH}(?=\d)")
_EDGE_DASH = re.compile(rf"^\s*{_DASH}\s*|\s*{_DASH}\s*$")
_INNER_DASH = re.compile(rf"\s*{_DASH}\s*")

# (kind, pattern, what to write instead) for passages that need rewording: a block with one is rewritten once.
PATTERNS = (
    ("calque", re.compile(r"[을를]\s*통해(?:서)?(?![가-힣])|[을를]\s*통한(?![가-힣])"), "~을 통해 -> ~로, ~해서"),
    ("calque", re.compile(r"에\s*대해(?:서)?(?![가-힣])|에\s*대한(?![가-힣])"), "~에 대해 -> the object particle (규제를 논의)"),
    ("calque", re.compile(r"에\s*있어(?:서)?(?![가-힣])"), "~에 있어서 -> ~에서"),
    ("calque", re.compile(r"[을를]\s*(?:가지|갖)고\s*있"), "~을 가지고 있다 -> an adjective (경쟁력이 강하다)"),
    ("calque", re.compile(r"(?:기|[을를])\s*위해서는"), "~하기 위해서는 -> ~하려면"),
    ("calque", re.compile(r"(?:으로부터|로부터|에서|으로)의(?![가-힣])"), "~로부터의, ~에서의 -> spell the relation out"),
    ("cliche", re.compile(r"결론적으로|요약하(?:면|자면)|크게\s*(?:[두세네]|\d+)\s*가지로|주목할\s*만한|시사하는\s*바|혁신적|폭발적"),
     "a filler opener or stock phrase: delete it, or write the fact and its size"),
    ("hedge", re.compile(r"(?:가능성이|수도?)\s*있을\s*수\s*있"), "two hedges in one clause: keep one (~할 수 있습니다)"),
    ("slang", re.compile(r"박아\s*(?:두|둔|둘|둠|둡|놓)|박혀\s*있|박힌|갈아\s*끼|때려\s*박|뒤집"),
     "colloquial: 저장하다/기입하다, 교체하다, 바꾸다/반대로 하다"),
)
# What the records and the mail call each kind of correction.
LABELS = {"double-passive": "이중 피동", "range-dash": "범위 대시", "sign-dash": "부호 대시", "dash": "대시",
          "calque": "번역 칼크", "cliche": "상투구", "hedge": "겹친 완곡", "slang": "구어"}

# The prompt rule every writer carries (appended to its system prompt). English like the prompts, Korean examples.
RULES = """Natural Korean (code fixes a double passive and a dash in place, asks one rewrite for the other marked
patterns, and a reader marks the rest). Write as a Korean editor would, not as a translation: verb and adjective
roots, a concrete actor as the subject, one hedge per clause. Never: a double passive (되어진다, 보여집니다 -> 됩니다,
보입니다); English-order calques (~을 통해 -> ~로, ~해서; ~에 대해 논의 -> ~를 논의; ~에 있어서 -> ~에서; ~을 가지고
있다 -> an adjective, 경쟁력이 강하다; ~하기 위해서는 -> ~하려면; ~로부터의, ~에서의, ~와의 -> spell the relation out);
filler openers and stock phrases (결론적으로, 요약하면, 크게 세 가지로, 주목할 만한, 시사하는 바가 큽니다, 혁신적,
폭발적 -> delete, or the fact and its size); a doubled hedge (가능성이 있을 수 있습니다 -> 있을 수 있습니다); colloquial
verbs (박아두다, 갈아끼우다, 뒤집다 -> 저장하다, 교체하다, 바꾸다); an em dash as punctuation (a comma, a colon or a new
sentence). Sparingly, once or twice in a whole text at most: ~에 의해 (name the actor instead), ~는 점에서 (~라서),
더 이상, 즉, 매우, and the nominal endings ~것입니다 and ~뜻입니다 where a plain verb ending says the same (계속
일하게 하는 것입니다 -> 계속 일하게 합니다)."""


def _single_passive(match: re.Match[str]) -> str:
    """The single form of a double passive, or the match unchanged when the syllable after the prefix is not 지/져."""
    prefix, syllable = match.group(1), ord(match.group(2)) - _HANGUL
    initial, medial, final = syllable // _INITIALS, syllable % _INITIALS // _FINALS, syllable % _FINALS
    if initial != _JIEUT or medial not in (_I, _YEO):
        return match.group(0)
    stem = _PASSIVES[prefix][0 if medial == _I else 1]
    last = ord(stem[-1]) - _HANGUL  # Open syllables (되, 이, 뉘, 히, 어, 여): the final consonant of 지/져 moves onto it.
    return stem[:-1] + chr(_HANGUL + last - last % _FINALS + final)


# The steps of `tidy`, in order: (kind, pattern, replacement).
_STEPS = (("double-passive", _DOUBLE_PASSIVE, _single_passive), ("range-dash", _RANGE_DASH, "~"),
          ("sign-dash", _SIGN_DASH, "-"), ("dash", _EDGE_DASH, ""), ("dash", _INNER_DASH, ", "))


def tidy(text: str, found: list[dict[str, str]] | None = None) -> str:
    """`text` with each double passive in its single form and dashes turned into range marks, signs or commas. With
    `found`, every change is appended to it as {"kind", "before", "after"}: the record of the corrections."""
    for kind, pattern, replacement in _STEPS:
        def substitute(match: re.Match[str], kind: str = kind, replacement: Any = replacement) -> str:
            new = replacement(match) if callable(replacement) else replacement
            if found is not None and new != match.group(0):
                found.append({"kind": kind, "before": match.group(0).strip(), "after": new.strip()})
            return new
        text = pattern.sub(substitute, text)
    return text


def fixes(text: str) -> list[dict[str, str]]:
    """What `tidy` would change in `text`, as [{"kind", "before", "after"}]."""
    found: list[dict[str, str]] = []
    tidy(text, found)
    return found


def problems(text: str) -> list[tuple[str, str]]:
    """Every passage of `text` that needs rewording, in text order, as (kind, the matched words)."""
    found = [(match.start(), kind, " ".join(match.group(0).split()))
             for kind, pattern, _ in PATTERNS for match in pattern.finditer(text)]
    return [(kind, words) for _, kind, words in sorted(found)]


def problem(text: str) -> tuple[str, str] | None:
    """The first passage of `text` that needs rewording, as (kind, the matched words), or None."""
    return next(iter(problems(text)), None)


def advice(kind: str) -> str:
    """What to write instead of a pattern of `kind`, for the writer's repair brief."""
    return "; ".join(dict.fromkeys(hint for k, _, hint in PATTERNS if k == kind))


def summary(stats: dict[str, Any] | None) -> str | None:
    """One Korean line on the corrections of one text, for the mail's footer and `smith report korean`: the parts
    present in `stats` among tidied (fixes), flagged and remaining ({"kind", "words", ...}, what the report's check
    sent back and what stayed) and reader_notes (what the first reader marked). None without stats."""
    if stats is None:
        return None
    parts = []
    if "tidied" in stats:
        parts.append(f"고정 수정 {len(stats['tidied'])}건" + _detail(_kinds(stats["tidied"])))
    if "flagged" in stats:
        parts.append(f"재작성 요청 {len(stats['flagged'])}건" + _detail(_flags(stats["flagged"])))
    if "remaining" in stats:
        parts.append(f"남은 것 {len(stats['remaining'])}건" + _detail(_flags(stats["remaining"])))
    if "reader_notes" in stats:
        parts.append(f"첫 독자 지적 {len(stats['reader_notes'])}건")
    return "한국어 교정: " + ", ".join(parts)


def _kinds(items: list[dict[str, Any]]) -> str:
    counts = Counter(LABELS.get(item.get("kind", ""), item.get("kind", "")) for item in items)
    return ", ".join(f"{label} {count}" for label, count in counts.items() if label)


def _flags(items: list[dict[str, Any]]) -> str:
    words = list(dict.fromkeys(item["words"] for item in items if item.get("words")))[:5]
    kinds = _kinds(items)
    return f"{kinds}: {', '.join(words)}" if kinds and words else kinds or ", ".join(words)


def _detail(text: str) -> str:
    return f"({text})" if text else ""
