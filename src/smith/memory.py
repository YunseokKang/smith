"""Client memory (FR-19): what the client told Smith, kept as Markdown files and retrieved for every advice call.

The client mentions facts the ledger cannot hold (which home a nickname means, how long they lived where,
a subscription they applied for, a spouse's consent, how they like advice). Smith keeps them in
`data/memory/<topic>.md`, next to the ledger and ignored by Git, as plain bullets the client can read,
correct or delete in any editor:

    # 부동산·주거

    - 2026-10-05 · 메일 · 솔방울은 푸른마을아파트를 말한다.

Facts come only from the client: an answered e-mail question (the answer names what it will remember)
or `smith memory add`. Web research never writes memory and the research model never reads it.

Retrieval is a small RAG step without embeddings, so nothing leaves the machine to index it: the profile
and preference topics always go with a call; the other facts are ranked by shared character bigrams
with the call's text (Korean particles still match: "푸른마을을" shares "푸른마을") and newest first, within a
character budget. When everything fits, everything is sent.

Facts are the client's statements, not ledger facts: prompts say so, and a figure taken from one is
flagged like a figure from the client's question. Identifiers (phone, e-mail, account numbers) and
building or unit numbers are removed when a fact is written and again when it is read.
"""
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from smith.payload import mask_identifiers

TOPICS = {"profile": "가구·인적 사항", "property": "부동산·주거", "plan": "계획·일정", "preference": "선호·원칙",
          "other": "기타"}
PINNED = ("profile", "preference")   # Always sent: they shape every answer.
MAX_FACT_CHARS = 300
BUDGET_CHARS = 4000                  # About 25 facts; beyond this only the most relevant are sent.
_ENTRY = re.compile(r"^- (?:(\d{4}-\d{2}-\d{2}) · ([^·]{1,20}) · )?(.+)$")
# A building or unit number ("303동", "1203호") narrows a complex to one home: never kept.
_UNIT = re.compile(r"(?<!\d)\d{1,4}\s*(?:동|호)(?!\s*(?:이상|이하|이내))")


@dataclass(frozen=True)
class Fact:
    topic: str
    text: str
    recorded_on: date | None   # None for a line the client wrote by hand without a date.
    source: str


def directory(db: Path) -> Path:
    """Memory lives next to the ledger (data/memory for data/smith.db), so it is private by the same rule."""
    return Path(db).parent / "memory"


def clean(text: str) -> str:
    """One line, identifiers and building or unit numbers removed, at most MAX_FACT_CHARS."""
    text = _UNIT.sub("(동·호 생략)", mask_identifiers(" ".join(str(text).split())))
    return text.replace("((동·호 생략))", "(동·호 생략)")[:MAX_FACT_CHARS].strip()


def load(folder: Path) -> list[Fact]:
    """Every bullet of every Markdown file in `folder`; a file whose name is not a topic counts as "other"."""
    facts = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        topic = path.stem if path.stem in TOPICS else "other"
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            match = _ENTRY.match(line.strip()) if not line.startswith((" ", "\t")) else None
            if not match or not clean(match.group(3)):
                continue
            try:
                recorded = date.fromisoformat(match.group(1)) if match.group(1) else None
            except ValueError:
                recorded = None
            facts.append(Fact(topic, clean(match.group(3)), recorded, (match.group(2) or "직접 작성").strip()))
    return facts


def new_facts(existing: list[Fact], candidates: list[dict[str, str]]) -> list[tuple[str, str]]:
    """(topic, text) of the candidates not already remembered, cleaned; unknown topics become "other"."""
    known = {_key(f.text) for f in existing}
    found: dict[str, tuple[str, str]] = {}
    for item in candidates:
        text = clean(item.get("fact", ""))
        if text and _key(text) not in known:
            found.setdefault(_key(text), (item.get("topic") if item.get("topic") in TOPICS else "other", text))
    return list(found.values())


def add(folder: Path, topic: str, text: str, *, on: date, source: str) -> Fact | None:
    """Append one fact to `<topic>.md`; None when it is empty or already remembered."""
    topic = topic if topic in TOPICS else "other"
    fresh = new_facts(load(folder), [{"topic": topic, "fact": text}])
    if not fresh:
        return None
    _, text = fresh[0]
    source = clean(source).replace("·", " ")[:20] or "직접 작성"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{topic}.md"
    header = "" if path.exists() else f"# {TOPICS[topic]}\n\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{header}- {on.isoformat()} · {source} · {text}\n")
    return Fact(topic, text, on, source)


def retrieve(facts: list[Fact], query: str, *, budget: int = BUDGET_CHARS) -> list[Fact]:
    """The facts to send with one call: pinned topics first, then the most relevant, within `budget`."""
    if sum(len(f.text) for f in facts) <= budget:
        return sorted(facts, key=_recency, reverse=True)
    pinned = sorted((f for f in facts if f.topic in PINNED), key=_recency, reverse=True)
    others = [f for f in facts if f.topic not in PINNED]
    weights = _idf(others)
    wanted = _bigrams(query)
    others.sort(key=lambda f: (sum(weights[g] for g in _bigrams(f.text) & wanted), _recency(f)), reverse=True)
    chosen, used = [], 0
    for fact in pinned + others:
        if used + len(fact.text) <= budget:
            chosen.append(fact)
            used += len(fact.text)
    return chosen


def for_model(facts: list[Fact]) -> list[dict[str, Any]]:
    """The payload form: what the client said, under which topic, when and through which channel."""
    return [{"topic": f.topic, "fact": f.text, "recorded_on": None if f.recorded_on is None else f.recorded_on.isoformat(),
             "source": f.source} for f in facts]


def _key(text: str) -> str:
    return re.sub(r"[\s.,·!?~]", "", text)


def _recency(fact: Fact) -> date:
    return fact.recorded_on or date.min


def _bigrams(text: str) -> set[str]:
    """Character bigrams of each word: Korean has no spaces between a noun and its particle."""
    grams = set()
    for word in re.findall(r"[0-9A-Za-z가-힣]+", text.lower()):
        grams |= {word[i:i + 2] for i in range(len(word) - 1)} or {word}
    return grams


def _idf(facts: list[Fact]) -> dict[str, float]:
    """Rare bigrams weigh more than common ones ("아파트" appears everywhere, "푸른마을" in one fact)."""
    counts: dict[str, int] = {}
    for fact in facts:
        for gram in _bigrams(fact.text):
            counts[gram] = counts.get(gram, 0) + 1
    total = len(facts) or 1
    return {gram: math.log(1 + total / count) for gram, count in counts.items()}
