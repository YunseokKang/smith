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
from html import escape
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
    # Translated idioms the client's style guide names (2026-11-01): "I hear you saying", "the scale tips", "give up".
    ("calque", re.compile(r"(?:말씀|얘기|이야기)(?:으로|로)\s*들(?:었|려|리)|저울이\s*(?:더\s*)?(?:무겁|기울)|내주는\s*것|내줘야\s*하는\s*것"),
     "a translated idiom: open on the matter itself; 기회비용(감수할 부분), 득실을 따지면"),
)
# What the records and the mail call each kind of correction.
LABELS = {"double-passive": "이중 피동", "range-dash": "범위 대시", "sign-dash": "부호 대시", "dash": "대시",
          "calque": "번역 칼크", "cliche": "상투구", "hedge": "겹친 완곡", "slang": "구어"}

# The prompt rule every writer carries (appended to its system prompt). English like the prompts, Korean examples.
_NATURAL = """Natural Korean (code fixes a double passive and a dash in place, asks one rewrite for the other marked
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
# The client's style and readability guide (user feedback 2026-11-01, after the first counsel letters), in Korean with
# its own examples, appended to every writer's prompt through RULES: tone and endings, no translated idioms, lists for
# three or more parallel items, limits folded into the sentence, dense sentences, and a before/after example.
STYLE = """한국어 문체·가독성 지침 (사용자 지침 2026-11-01). 다섯 원칙을 모든 글에 적용한다.

1. 어조와 어미: 글마다 정한 기본 문체(보고서·레터는 하십시오체, 상담은 해요체)를 따르되 어미를 하나로 통일하지 않는다. 모든
   문장이 ~예요/~해요/~거든요로 끝나면 가볍고 수다스럽다. 핵심 결론과 수치는 ~입니다/~바랍니다/~권장합니다로 무게를 주고,
   공감과 부드러운 제안은 ~해요/~보세요를 섞어 리듬을 만든다. 첫 문장을 "~라는 말씀으로 들었어요" 같은 번역투 수용 어미로
   시작하지 말고 질문의 핵심을 곧바로 짚는다.
2. 번역투와 억지 비유 금지: 영어식 관용구를 풀어쓰지 말고 한국어 화자가 쓰는 어휘(한자어 포함)로 쓴다.
   ✗ "이 선택에는 내주는 것이 있어요" → ✓ "이 선택에는 기회비용(감수할 부분)이 따릅니다."
   ✗ "몸 쪽 저울이 더 무겁다고 봐요" → ✓ "득실을 따져보면 근력 운동을 해야 할 이유가 훨씬 큽니다."
   ✗ "기울어 계신 쪽의 비용부터 짚을게요" → ✓ "고민 중이신 '외부 준비'의 리스크부터 짚어보겠습니다."
   ✗ "제 생각을 바꿀 한 가지는 회복 신호예요" → ✓ "단, 한 가지 예외 조건은 '회복 지표'입니다."
   ✗ "안에서 7, 밖에서 3이에요" → ✓ "제가 제안하는 에너지 배분 비율은 '사내 7 : 사외 3'입니다."
   ✗ "속 구성이 지방 쪽으로 기울고 있어요" → ✓ "체성분이 체지방 위주로 불균형해지고 있습니다."
3. 정보의 구조화: 수치 비교, 단계별 계획, 루틴, 조건별 시나리오가 셋 이상 나열되면 줄글로 잇지 말고 불릿("- ")이나 번호("1. ")로
   쓴다. 줄글은 핵심 결론·통찰·공감·전체 맥락에, 목록은 운동 루틴, 회차별 일정, 식단, 첫째·둘째·셋째 근거, 연령별(45·55·60세)
   시나리오에 쓴다. 한 칸이 한두 문장인 보고서 블록은 줄글을 유지한다.
4. 메타 발언 분리: 자료의 한계나 출처의 신뢰도는 본문 흐름을 끊는 별도 문장으로 끼워 넣지 말고 문장 안에 녹이거나 괄호로 쓴다.
   ✗ "이건 출처가 하나뿐인 근거예요. 참고로 모아 두신 글이라 본인 생각으로 읽지는 않았어요."
   ✓ "주 2일을 넘겨도 효과가 더 커지는지는 추가 근거가 필요하지만(단일 연구 [R1]), …"
   ✓ "지난 9월 스크랩해 두신 책 구절처럼 가벼운 운동으로 충분하다는 시각도 있습니다."
5. 문장 밀도: 지시대명사(그것, 이것, 그 칸, 그 숫자) 대신 구체적인 명사를 쓴다. 한 문장에 생각 하나. 그리고·그런데·그래서·또
   하나로 문장을 이어 붙이지 않는다.

적용 예(전/후):
[전] A루틴은 레그프레스, 체스트프레스, 시티드 로우, 덤벨 숄더프레스, 플랭크 30초예요. B루틴은 고블릿 스쿼트, 랫풀다운, 덤벨
루마니안 데드리프트, 푸시업, 데드버그예요. 마무리 5분은 두 루틴이 같아요. 1회차는 A루틴을 가장 가벼운 무게로 1~2세트, 12번씩
해요. 자세를 익히는 날이에요. 2회차는 B루틴을 똑같이 가볍게 해요. 3회차는 A루틴 2세트, 열 번째쯤 힘들어지는 무게를 찾아요.
[후] 운동은 전신을 고루 자극하는 A/B 교차 루틴으로 진행합니다. 회당 35~45분(준비 5분 → 본 운동 25~35분 → 자세 교정 스트레칭
5분) 구성입니다.
- A루틴: 레그프레스, 체스트프레스, 시티드 로우, 덤벨 숄더프레스, 플랭크(30초)
- B루틴: 고블릿 스쿼트, 랫풀다운, 덤벨 루마니안 데드리프트, 푸시업, 데드버그
- 공통 마무리(5분): 벽에 등 대고 턱 당기기(10회), 문틀 가슴 스트레칭(양쪽 각 30초), 무릎 꿇고 장요근 늘리기(양쪽 각 30초)
11월 회차별 진행 가이드(주 2회, 월·목 또는 화·금 권장)
1. 1~2회차(자세 적응): A루틴(1회차), B루틴(2회차)을 가장 가벼운 무게로 1~2세트(12회) 진행하며 정확한 궤적을 익힙니다.
2. 3~4회차(적정 중량 탐색): 각 2세트씩 진행하되, 10번째 반복 무렵부터 힘에 부치는 무게를 찾습니다.
3. 5~6회차(볼륨 확대): 각 3세트로 늘리고, 12회가 수월해진 종목만 무게를 한 단계 올립니다.
4. 7회차(디로딩·점검): A·B 루틴 중 4종목만 골라 2세트로 가볍게 마친 뒤 인바디를 측정합니다. 이 결과가 12월 루틴의 기준점입니다.
   (테니스 레슨 전날에는 하체 종목을 1세트 줄여 피로도를 조절하세요.)"""
RULES = _NATURAL + "\n\n" + STYLE


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


_BULLET = re.compile(r"^\s*[-•·▪]\s+(.*\S)\s*$")
_NUMBERED = re.compile(r"^\s*\d{1,2}[.)]\s+(.*\S)\s*$")


def blocks(text: str) -> list[tuple[str, list[str]]]:
    """A letter's lines as blocks for rendering (style guide rule 3): ("p", [line]) for a prose line, consecutive
    "- " lines as one ("ul", items), consecutive "1. " lines as one ("ol", items). Blank lines are block breaks for
    prose only; a list stays one list across them."""
    found: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        bullet, numbered = _BULLET.match(line), _NUMBERED.match(line)
        kind, item = ("ul", bullet.group(1)) if bullet else ("ol", numbered.group(1)) if numbered else ("p", line.strip())
        if kind != "p" and found and found[-1][0] == kind:
            found[-1][1].append(item)
        else:
            found.append((kind, [item]))
    return found


def html_blocks(text: str, *, color: str, size: int = 15, line_height: float = 1.7) -> str:
    """The blocks of `text` as e-mail HTML: one <p> per prose line, <ul>/<ol> for its lists, inline styles only."""
    style = f"font-size:{size}px;line-height:{line_height};color:{color}"
    out = []
    for kind, items in blocks(text):
        if kind == "p":
            out.append(f'<p style="margin:0 0 12px;{style}">{escape(items[0])}</p>')
        else:
            out.append(f'<{kind} style="margin:0 0 12px;padding-left:22px;{style}">'
                       + "".join(f'<li style="margin:0 0 4px">{escape(item)}</li>' for item in items) + f"</{kind}>")
    return "".join(out)


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
    elif stats.get("reader_skipped"):
        parts.append("첫 독자 검토 못 함" + ("(시간 부족)" if stats["reader_skipped"] == "skipped-time" else ""))
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
