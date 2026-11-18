"""Overnight news for the daily mail (FR-31 ③): one web call, then code decides what is shown: an outlet allowlist, a
plain https article URL, a page dated yesterday or today, and a reachability check of the page.

The call carries the date, the topics and the reader's own short interest words (`[daily] news_interests`, local
config), nothing else personal. The model proposes about twice the asked count and scores each for importance, urgency
and relevance; code keeps the `count` with the highest sum among those that pass. Fewer is fine, padding is not, and a
link is never the model's word alone (`sources.verify` fetches the page). The brief is stored like the finance research
(`research_briefs`, topic `daily-news`) so a morning run reuses the one prepared shortly before.
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

from smith import headless, ledger, research, sources
from smith.payload import check_outbound, mask_identifiers

MODEL = headless.MODEL
BUDGET_USD = "5.00"
TIMEOUT_SECONDS = 900
PROMPT_VERSION = "daily-news-v3"
SINCE_HOUR = 18   # "Overnight" starts at 18:00 the day before, Korea time (user 2026-11-18: 간밤에 일어난 일).
FRESH_DAYS = 1    # The page date must be yesterday or today: an overnight event in the Americas is dated yesterday.
REUSE_WITHIN = timedelta(hours=6)
MAX_HEADLINE, MAX_DETAIL = 90, 400
UNLISTED_KEPT = 20   # Rejected outlet hosts kept in the brief, so a thin morning says which outlets the model chose.
# Outlets whose article pages may be linked (host suffixes). National wires, dailies, business papers and broadcasters;
# the big international wires, papers and broadcasters; technology and science titles. The model receives this list
# (BUG-2026-008: unaware of it, it chose unlisted outlets and the 2026-11-18 mail had no news). The client may trim it.
HOSTS = ("yna.co.kr", "yonhapnews.co.kr", "news.kbs.co.kr", "kbs.co.kr", "imnews.imbc.com", "mbc.co.kr", "news.sbs.co.kr",
         "sbs.co.kr", "ytn.co.kr", "jtbc.co.kr", "chosun.com", "joongang.co.kr", "joins.com", "donga.com", "hani.co.kr",
         "khan.co.kr", "mk.co.kr", "hankyung.com", "sedaily.com", "edaily.co.kr", "newsis.com", "news1.kr", "kmib.co.kr",
         "hankookilbo.com", "munhwa.com", "segye.com", "seoul.co.kr", "etnews.com", "zdnet.co.kr", "bloter.net",
         "yonhapnewstv.co.kr", "mt.co.kr", "heraldcorp.com", "asiae.co.kr", "fnnews.com", "ajunews.com", "dt.co.kr",
         "inews24.com", "nocutnews.co.kr", "mbn.co.kr", "tvchosun.com", "ichannela.com", "koreaherald.com",
         "koreatimes.co.kr", "aitimes.com", "thelec.kr", "ddaily.co.kr",
         "reuters.com", "apnews.com", "afp.com", "bbc.com", "bbc.co.uk", "nytimes.com", "ft.com", "bloomberg.com", "wsj.com",
         "theguardian.com", "economist.com", "cnbc.com", "cnn.com", "nbcnews.com", "abcnews.go.com", "cbsnews.com",
         "npr.org", "washingtonpost.com", "latimes.com", "politico.com", "axios.com", "semafor.com", "time.com",
         "fortune.com", "marketwatch.com", "barrons.com", "aljazeera.com", "dw.com", "france24.com", "nikkei.com",
         "asia.nikkei.com", "nhk.or.jp", "kyodonews.net", "japantimes.co.jp", "scmp.com", "straitstimes.com",
         "techcrunch.com", "theverge.com", "arstechnica.com", "wired.com", "technologyreview.com", "venturebeat.com",
         "nature.com", "science.org")
SYSTEM_PROMPT = """You are the morning news desk for one reader in Korea who reads at 06:00 before the commute. You have
WebSearch and WebFetch; use them. Input JSON: {"today", "since", "topics", "interests", "count", "language", "outlets"}.

Report what happened overnight: events that took place or first broke after `since` (Korea time; the daytime of the
Americas and Europe). An older story counts only for a new development inside that window, told as that development.
Propose about twice `count` candidates across `topics` and the reader's `interests`, Korea and the world, one item per
story (developments of the same story are one item), no opinion columns, no sports or celebrity news unless of
national significance. Prefer the outlet that reported it first or an established wire.

Score every candidate "1" to "5" on three separate axes. Code ranks by the sum and keeps the top `count`, so score
honestly and do not inflate:
- importance: how many people it affects and how deeply (5: a national or global turning point; 1: a minor item).
- urgency: whether the reader should know or act on it today (5: changes today's commute, money, health or safety, or
  takes effect today; 1: background that can wait).
- relevance: how directly it touches the reader (5: squarely on one of `interests`; 3: on one of `topics` only; 1: neither).

Each item: topic (one of the given topics), headline (Korean, factual, under 60 characters), detail (Korean, two
sentences: what happened, and what it means for this reader, naming the interest it touches when it does),
published_on (YYYY-MM-DD), publisher (outlet name), source_title (the page title), source_url (the article page itself,
https), kind ("fact"), importance, urgency, relevance. Only URLs you actually opened; never compose one.

The source_url host must be one of `outlets` or a subdomain of one. Code drops every other link, portal copies on Naver
or Daum included, so when a story turns up elsewhere, find the same story on a listed outlet and link that page.
Write every text in Korean. Output JSON that matches the schema; `gaps` lists topics with nothing worth reporting."""
SCORES = ("importance", "urgency", "relevance")
MAX_INTERESTS, MAX_INTEREST = 8, 30


def schema(count: int) -> dict[str, Any]:
    """The research brief schema plus the three scores (strings "1".."5": the validator knows strings, lists, objects)."""
    shape = research.brief_schema(count * 4, 10)
    item = shape["properties"]["items"]["items"]
    item["properties"] = {**item["properties"], **{name: {"type": "string", "maxLength": 4} for name in SCORES}}
    item["required"] = [*item["required"], *SCORES]
    return shape


def clean_interests(raw: Any) -> list[str]:
    """The reader's interests from `[daily] news_interests`: short public words only, identifiers masked."""
    words = [mask_identifiers(" ".join(str(w).split()))[:MAX_INTEREST] for w in (raw if isinstance(raw, list) else [])]
    return [w for w in words if w][:MAX_INTERESTS]


def rank(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """The top `count` by the sum of the scores (ties: urgency, then importance, then the model's order), renumbered."""
    ordered = sorted(enumerate(items), key=lambda pair: (-sum(pair[1][s] for s in SCORES), -pair[1]["urgency"],
                                                         -pair[1]["importance"], pair[0]))
    return [dict(item, ref=f"R{n}") for n, (_, item) in enumerate(ordered[:count], start=1)]


def research_and_store(db: Path, *, now: datetime, today: date, topics: list[str], interests: list[str], count: int,
                       secrets: list[str], executable: Path | None = None, runner: headless.Runner = subprocess.run,
                       researcher: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Today's news brief, reused when one was made within REUSE_WITHIN; never raises for a research failure.

    Returns {"outcome": success|failure|reused, "items", "brief_id", "error_code"}.
    """
    question = f"{today.isoformat()} {' '.join(topics)} | {' '.join(interests)} x{count} {PROMPT_VERSION}"
    topic_list = [{"ref": "N1", "title": "daily-news", "question": question}]
    with closing(ledger.connect(db)) as conn:
        found = ledger.latest_research(conn, since=now - REUSE_WITHIN, topics=topic_list)
        if found is not None:
            return {"outcome": "reused", "items": found["brief"]["items"], "brief_id": found["brief_id"], "error_code": None}
    brief_id = f"news-{today.isoformat()}-{uuid.uuid4().hex[:8]}"
    try:
        result = (researcher or research_news)(today=today, topics=topics, interests=interests, count=count,
                                               secrets=secrets, executable=executable or headless.find_claude(),
                                               runner=runner)
    except headless.HeadlessError as error:
        with closing(ledger.connect(db)) as conn:
            ledger.record_research(conn, brief_id=brief_id, created_at=now, topics=topic_list, outcome="failure",
                                   error_code=error.code if hasattr(error, "code") else str(error), model=MODEL)
        return {"outcome": "failure", "items": [], "brief_id": brief_id, "error_code": str(error)}
    with closing(ledger.connect(db)) as conn:
        ledger.record_research(conn, brief_id=brief_id, created_at=now, topics=topic_list, outcome="success", model=MODEL,
                               cost_usd=result.get("cost_usd"), brief={"items": result["items"], "gaps": result["gaps"],
                                                                         "dropped": result["dropped"],
                                                                         "unlisted_hosts": result.get("unlisted_hosts", {}),
                                                                         "prompt_version": PROMPT_VERSION})
    return {"outcome": "success", "items": result["items"], "brief_id": brief_id, "error_code": None}


def research_news(*, today: date, topics: list[str], interests: list[str], count: int, secrets: list[str],
                  executable: Path, runner: headless.Runner = subprocess.run, model: str = MODEL,
                  verifier: Callable[..., tuple[list[dict[str, Any]], dict[str, int]]] | None = None) -> dict[str, Any]:
    """One web call, then selection, page checks and ranking. Raises headless.HeadlessError when the call fails."""
    since = f"{(today - timedelta(days=1)).isoformat()} {SINCE_HOUR:02d}:00 KST"
    prompt = json.dumps({"today": today.isoformat(), "since": since, "topics": topics, "interests": interests,
                         "count": count, "language": "ko", "outlets": list(HOSTS)}, ensure_ascii=False)
    check_outbound(prompt, secrets)
    output, cost = headless.run(prompt, system_prompt=SYSTEM_PROMPT, schema=schema(count), executable=executable,
                                model=model, budget_usd=BUDGET_USD, timeout_seconds=TIMEOUT_SECONDS,
                                tools=headless.WEB_TOOLS, runner=runner)
    raw = output.get("items", [])
    items, reasons = select(raw, today=today, topics=topics)
    items, unreachable = (verifier or sources.verify)(items)
    for reason, number in unreachable.items():
        reasons[reason] = reasons.get(reason, 0) + number
    gaps = [mask_identifiers(str(g))[:200] for g in output.get("gaps", []) if str(g).strip()]
    return {"items": rank(items, count), "gaps": gaps, "dropped": reasons, "unlisted_hosts": unlisted_hosts(raw),
            "cost_usd": cost}


def select(raw: list[dict[str, Any]], *, today: date, topics: list[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep items from listed outlets with a plain https URL and a recent date; one per URL and per headline."""
    kept: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    seen_urls: set[str] = set()
    seen_heads: set[str] = set()

    def drop(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for item in raw:
        if not isinstance(item, dict):
            drop("not-an-item")
            continue
        url = str(item.get("source_url", "")).strip()
        headline = _clean(item.get("headline"), MAX_HEADLINE)
        detail = _clean(item.get("detail"), MAX_DETAIL)
        if not headline or not detail:
            drop("empty-text")
            continue
        if not research._plain_https(url):
            drop("bad-url")
            continue
        if not allowed_host(url):
            drop("host-not-listed")
            continue
        published = _date(item.get("published_on"))
        if published is None or not (today - timedelta(days=FRESH_DAYS) <= published <= today):
            drop("stale-or-undated")
            continue
        scores = {name: _score(item.get(name)) for name in SCORES}
        if None in scores.values():
            drop("unscored")
            continue
        key = re.sub(r"\s+", "", headline)
        if url in seen_urls or key in seen_heads:
            drop("duplicate")
            continue
        seen_urls.add(url)
        seen_heads.add(key)
        topic = str(item.get("topic", "")).strip()
        kept.append({"topic": topic if topic in topics else "기타", "headline": headline, "detail": detail,
                     "published_on": published.isoformat(), "publisher": _clean(item.get("publisher"), 60),
                     "source_title": _clean(item.get("source_title"), 200), "source_url": url, "kind": "fact",
                     **scores})
    return kept, reasons


def allowed_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in HOSTS)


def unlisted_hosts(raw: list[Any]) -> dict[str, int]:
    """Hosts of proposed https links outside HOSTS, most frequent first: public outlet names only, never the URL."""
    counts: dict[str, int] = {}
    for item in raw:
        url = str(item.get("source_url", "")).strip() if isinstance(item, dict) else ""
        if research._plain_https(url) and not allowed_host(url):
            host = (urlsplit(url).hostname or "").lower()
            counts[host] = counts.get(host, 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:UNLISTED_KEPT])


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = mask_identifiers(re.sub(r"(?:https?://|www\.)\S+", "", text))
    return text[:limit].strip()


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _score(value: Any) -> int | None:
    """A score "1".."5" (a bare number is accepted too); anything else is None."""
    text = str(value).strip()
    return int(text) if text in ("1", "2", "3", "4", "5") else None
