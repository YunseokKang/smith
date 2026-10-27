"""Something to bring to the partner tonight (daily design §16.3, user request 2026-10-27): the evening mail carries one
small thing the reader can say to or share with his wife — a warm word of support, a motivating line, or a light,
current topic (what people are talking about, an item that is selling, a word in fashion, a drama or show).

The kind rotates by date so the evenings differ (`kind_for`). The two warm kinds need no research; the four current
ones come from one web call that carries no personal data — only the date, the kind and the titles already used in the
last month — and code decides what may be shown: a listed outlet, a plain https article URL, a date within
FRESH_DAYS, a page that answers (`sources.verify`). Topics that would sour an evening (politics, crime, disaster,
deaths, scandal) are excluded in the prompt and by a word screen here. When research yields nothing, the evening falls
back to a warm kind and says so in the system notes; it never pads with an unverified trend.
"""
import json
import re
import subprocess
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from smith import daily_news, headless, ledger, research, sources
from smith.payload import check_outbound, mask_identifiers

MODEL = "fable"
BUDGET_USD = "3.00"
TIMEOUT_SECONDS = 600
PROMPT_VERSION = "evening-talk-v1"
FRESH_DAYS = 30            # Trends move slower than news; a month-old article on a drama still describes it.
REUSE_WITHIN = timedelta(hours=6)
REPEAT_DAYS = 30           # A topic or line shown within this many days is not offered again.
CANDIDATES = 4
MAX_TITLE, MAX_DETAIL = 40, 400
KINDS = {"cheer": "다정한 응원", "drama": "요즘 드라마·예능", "item": "요즘 뜨는 물건", "motivation": "힘이 되는 한마디",
         "slang": "요즘 유행어", "issue": "요즘 화제"}
ROTATION = ("cheer", "drama", "item", "motivation", "slang", "issue")   # Warm and current alternate.
RESEARCHED = ("drama", "item", "slang", "issue")
FALLBACK = "cheer"
# Entertainment, lifestyle and portal-hosted copies of outlet articles, on top of the news desk's outlets.
HOSTS = daily_news.HOSTS + (
    "osen.co.kr", "starnewskorea.com", "sportschosun.com", "xportsnews.com", "tenasia.co.kr", "dispatch.co.kr",
    "mydaily.co.kr", "isplus.com", "spotvnews.co.kr", "newsen.com", "sportsseoul.com", "mbn.co.kr", "tvchosun.com",
    "ichannela.com", "heraldcorp.com", "asiae.co.kr", "mt.co.kr", "fnnews.com", "nocutnews.co.kr", "inews24.com",
    "dt.co.kr", "ajunews.com", "vogue.co.kr", "elle.co.kr", "marieclairekorea.com", "harpersbazaar.co.kr",
    "allurekorea.com", "cosmopolitan.co.kr", "wkorea.com", "gqkorea.co.kr", "esquirekorea.co.kr", "fashionbiz.co.kr",
    "korea.kr", "n.news.naver.com", "news.naver.com", "entertain.naver.com", "m.entertain.naver.com", "v.daum.net")
# A topic for a pleasant evening: these words in a title or detail drop the item whatever the model said.
SOUR = ("사망", "숨져", "참사", "사고", "살인", "폭행", "범죄", "구속", "기소", "재판", "탄핵", "선거", "정당", "의원",
        "대통령실", "전쟁", "테러", "지진", "화재", "논란", "열애", "이혼", "학폭", "마약", "자살", "성범죄")
_KIND_PROMPTS = {
    "drama": "a Korean drama, variety show or streaming series many people are watching right now (what it is about, "
             "why it is talked about, where to watch)",
    "item": "a product many people are buying or talking about right now in Korea (food, beauty, home, gadget, "
            "fashion; what it is and why it is popular). No shopping or affiliate pages, no prices unless the article says them",
    "slang": "a word or expression in fashion in Korea right now (its meaning, where it comes from, an example of use)",
    "issue": "a light, pleasant topic people in Korea are talking about right now (culture, travel, food, sport, a "
             "festival, a viral moment) — never politics, crime, disasters or someone's private life",
}
SYSTEM_PROMPT = """You find one kind of light conversation topic for a married couple's evening in Korea. You have
WebSearch and WebFetch; use them. Input JSON: {"today", "kind", "looking_for", "count", "avoid"}.

Find about `count` candidates of what `looking_for` describes, current within the last few weeks, each one different,
none whose title is in `avoid`. Pleasant to talk about at home: no politics, crime, accidents, disasters, deaths,
scandals, dating rumours or anything that would sour an evening.

Each item: topic (the kind as given), headline (Korean, the topic's own name, e.g. the drama title or the word, under
30 characters), detail (Korean, two or three sentences: what it is and why people talk about it now; for a word, its
meaning and one example sentence), published_on (YYYY-MM-DD of the article), publisher (outlet name), source_title (the
page title), source_url (the article page itself, https, on an established Korean or international outlet or a portal
copy of one), kind ("fact"). Only URLs you actually opened; never compose one. Output JSON that matches the schema;
`gaps` says what you could not find."""


def kind_for(day: date, kinds: tuple[str, ...] = ROTATION) -> str:
    """The kind of the evening before `day` (the mail's subject day), rotating through `kinds` by date."""
    return kinds[day.toordinal() % len(kinds)]


def recent(db: Path, *, now: datetime, product: str) -> list[dict[str, str]]:
    """What the evening mails of the last REPEAT_DAYS offered: [{"kind", "title", "line"}], newest first."""
    with closing(ledger.connect(db)) as conn:
        runs = [r for r in ledger.daily_runs(conn, limit=REPEAT_DAYS + 10, product=product)
                if r["status"] in ("sent", "unknown") and r["created_at"] >= now - timedelta(days=REPEAT_DAYS)]
        shown = []
        for run in runs:
            stored = ledger.daily_snapshot(conn, run["run_key"]) or {}
            item = stored.get("talk") or {}
            if item.get("title") or item.get("line"):
                shown.append({"kind": str(item.get("kind", "")), "title": str(item.get("title", "")),
                              "line": str(item.get("line", ""))})
    return shown


def research_and_store(db: Path, *, now: datetime, day: date, kind: str, avoid: list[str], secrets: list[str],
                       executable: Path | None = None, runner: headless.Runner = subprocess.run,
                       researcher: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Tonight's candidates for a researched kind, reused when made within REUSE_WITHIN; never raises for a research
    failure. Returns {"outcome": success|failure|reused|none, "items", "error_code"}; "none" for a warm kind."""
    if kind not in RESEARCHED:
        return {"outcome": "none", "items": [], "error_code": None}
    topic_list = [{"ref": "T1", "title": "evening-talk", "question": f"{day.isoformat()} {kind}"}]
    with closing(ledger.connect(db)) as conn:
        found = ledger.latest_research(conn, since=now - REUSE_WITHIN, topics=topic_list)
        if found is not None:
            return {"outcome": "reused", "items": found["brief"]["items"], "error_code": None}
    brief_id = f"talk-{day.isoformat()}-{uuid.uuid4().hex[:8]}"
    try:
        result = (researcher or research_talk)(today=now.date(), kind=kind, avoid=avoid, secrets=secrets,
                                               executable=executable or headless.find_claude(), runner=runner)
    except headless.HeadlessError as error:
        with closing(ledger.connect(db)) as conn:
            ledger.record_research(conn, brief_id=brief_id, created_at=now, topics=topic_list, outcome="failure",
                                   error_code=str(error)[:80], model=MODEL)
        return {"outcome": "failure", "items": [], "error_code": str(error)}
    with closing(ledger.connect(db)) as conn:
        ledger.record_research(conn, brief_id=brief_id, created_at=now, topics=topic_list, outcome="success", model=MODEL,
                               cost_usd=result.get("cost_usd"), brief={"items": result["items"], "gaps": result["gaps"],
                                                                         "dropped": result["dropped"],
                                                                         "prompt_version": PROMPT_VERSION})
    return {"outcome": "success", "items": result["items"], "error_code": None}


def research_talk(*, today: date, kind: str, avoid: list[str], secrets: list[str], executable: Path,
                  runner: headless.Runner = subprocess.run, model: str = MODEL,
                  verifier: Callable[..., tuple[list[dict[str, Any]], dict[str, int]]] | None = None) -> dict[str, Any]:
    """One web call, then selection and page checks. Raises headless.HeadlessError when the call fails."""
    prompt = json.dumps({"today": today.isoformat(), "kind": kind, "looking_for": _KIND_PROMPTS[kind],
                         "count": CANDIDATES * 2, "avoid": avoid[:REPEAT_DAYS]}, ensure_ascii=False)
    check_outbound(prompt, secrets)
    output, cost = headless.run(prompt, system_prompt=SYSTEM_PROMPT, schema=research.brief_schema(CANDIDATES * 4, 10),
                                executable=executable, model=model, budget_usd=BUDGET_USD, timeout_seconds=TIMEOUT_SECONDS,
                                tools=headless.WEB_TOOLS, runner=runner)
    items, reasons = select(output.get("items", []), today=today, kind=kind, avoid=avoid)
    items, unreachable = (verifier or sources.verify)(items)
    for reason, number in unreachable.items():
        reasons[reason] = reasons.get(reason, 0) + number
    confirmed = [i for i in items if confirmed_page(i)]
    if len(confirmed) < len(items):
        reasons["unconfirmed-page"] = reasons.get("unconfirmed-page", 0) + len(items) - len(confirmed)
    items = [dict(item, ref=f"T{n}") for n, item in enumerate(confirmed[:CANDIDATES], start=1)]
    gaps = [mask_identifiers(str(g))[:200] for g in output.get("gaps", []) if str(g).strip()]
    return {"items": items, "gaps": gaps, "dropped": reasons, "cost_usd": cost}


def select(raw: list[dict[str, Any]], *, today: date, kind: str, avoid: list[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep items from listed outlets with a plain https URL, a date within FRESH_DAYS, nothing sour and nothing
    offered in the last month; one per title and per URL."""
    kept: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    seen: set[str] = {_key(a) for a in avoid}
    urls: set[str] = set()

    def drop(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for item in raw:
        if not isinstance(item, dict):
            drop("not-an-item")
            continue
        title, detail = _clean(item.get("headline"), MAX_TITLE), _clean(item.get("detail"), MAX_DETAIL)
        url = str(item.get("source_url", "")).strip()
        published = _date(item.get("published_on"))
        if not title or not detail:
            drop("empty-text")
        elif not research._plain_https(url) or not allowed_host(url):
            drop("bad-or-unlisted-url")
        elif published is None or not (today - timedelta(days=FRESH_DAYS) <= published <= today + timedelta(days=1)):
            drop("stale-or-undated")
        elif sour(title + detail):
            drop("sour-topic")
        elif _key(title) in seen or url in urls:
            drop("repeat")
        else:
            seen.add(_key(title))
            urls.add(url)
            kept.append({"topic": kind, "headline": title, "detail": detail, "published_on": published.isoformat(),
                         "publisher": _clean(item.get("publisher"), 60), "source_title": _clean(item.get("source_title"), 200),
                         "source_url": url, "kind": "fact"})
    return kept, reasons


def confirmed_page(item: dict[str, Any]) -> bool:
    """Whether the page check saw the article answer on a listed host (review 2026-10-27: an "unchecked" timeout or a
    "mismatch" is not a link the mail may call confirmed; a redirect off the list is not either)."""
    check = item.get("check") or {}
    return check.get("status") in ("reachable", "matched") and allowed_host(str(check.get("final_url") or item.get("source_url", "")))


def sour(text: str) -> bool:
    """A word that would sour the evening (SOUR) anywhere in `text`."""
    return any(word in text for word in SOUR)


def allowed_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in HOSTS)


def _key(title: str) -> str:
    return re.sub(r"[\s'\"“”‘’<>《》「」]+", "", title).lower()


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = mask_identifiers(re.sub(r"(?:https?://|www\.)\S+", "", text))
    return text[:limit].strip()


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
