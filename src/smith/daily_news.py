"""Overnight news for the daily mail (FR-31 ③): one web call that carries no personal data, then code decides what is
shown: an outlet allowlist, a plain https article URL, a date within two days, and a reachability check of the page.

The model proposes twice the asked count; code keeps the first `count` that pass. Fewer is fine, padding is not, and a
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

MODEL = "fable"
BUDGET_USD = "5.00"
TIMEOUT_SECONDS = 900
PROMPT_VERSION = "daily-news-v1"
FRESH_DAYS = 2
REUSE_WITHIN = timedelta(hours=6)
MAX_HEADLINE, MAX_DETAIL = 90, 400
# Outlets whose article pages may be linked (host suffixes). National wires, dailies and broadcasters; the big
# international wires and papers; a few technology titles. The local config may extend this later.
HOSTS = ("yna.co.kr", "yonhapnews.co.kr", "news.kbs.co.kr", "kbs.co.kr", "imnews.imbc.com", "mbc.co.kr", "news.sbs.co.kr",
         "sbs.co.kr", "ytn.co.kr", "jtbc.co.kr", "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "khan.co.kr",
         "mk.co.kr", "hankyung.com", "sedaily.com", "edaily.co.kr", "newsis.com", "news1.kr", "kmib.co.kr", "hankookilbo.com",
         "munhwa.com", "segye.com", "seoul.co.kr", "etnews.com", "zdnet.co.kr", "bloter.net", "yonhapnewstv.co.kr",
         "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com", "ft.com", "bloomberg.com", "wsj.com",
         "theguardian.com", "economist.com", "cnbc.com", "cnn.com", "nikkei.com", "asia.nikkei.com", "techcrunch.com",
         "theverge.com", "arstechnica.com", "nature.com", "science.org", "washingtonpost.com", "politico.com", "axios.com")
SYSTEM_PROMPT = """You are the morning news desk for one reader in Korea who is about to commute. You have WebSearch and
WebFetch; use them. Input JSON: {"today", "topics", "count", "language"}.

Pick about twice `count` of the most consequential items of the last 24-36 hours across the topics, Korea and the world
mixed (at least two Korean and two international), one item per story, no opinion columns, no sports or celebrity news
unless of national significance. Prefer the outlet that reported it first or an established wire.

Each item: topic (one of the given topics), headline (Korean, factual, under 60 characters), detail (Korean, two
sentences: what happened, and why it matters to an ordinary reader), published_on (YYYY-MM-DD), publisher (outlet name),
source_title (the page title), source_url (the article page itself, https, on an established outlet: a wire, a national
daily or broadcaster, a major international paper), kind ("fact"). Only URLs you actually opened; never compose one.
Write every text in Korean. Output JSON that matches the schema; `gaps` lists topics with nothing worth reporting."""


def research_and_store(db: Path, *, now: datetime, today: date, topics: list[str], count: int, secrets: list[str],
                       executable: Path | None = None, runner: headless.Runner = subprocess.run,
                       researcher: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Today's news brief, reused when one was made within REUSE_WITHIN; never raises for a research failure.

    Returns {"outcome": success|failure|reused, "items", "brief_id", "error_code"}.
    """
    topic_list = [{"ref": "N1", "title": "daily-news", "question": f"{today.isoformat()} {' '.join(topics)} x{count}"}]
    with closing(ledger.connect(db)) as conn:
        found = ledger.latest_research(conn, since=now - REUSE_WITHIN, topics=topic_list)
        if found is not None:
            return {"outcome": "reused", "items": found["brief"]["items"], "brief_id": found["brief_id"], "error_code": None}
    brief_id = f"news-{today.isoformat()}-{uuid.uuid4().hex[:8]}"
    try:
        result = (researcher or research_news)(today=today, topics=topics, count=count, secrets=secrets,
                                               executable=executable or headless.find_claude(), runner=runner)
    except headless.HeadlessError as error:
        with closing(ledger.connect(db)) as conn:
            ledger.record_research(conn, brief_id=brief_id, created_at=now, topics=topic_list, outcome="failure",
                                   error_code=error.code if hasattr(error, "code") else str(error), model=MODEL)
        return {"outcome": "failure", "items": [], "brief_id": brief_id, "error_code": str(error)}
    with closing(ledger.connect(db)) as conn:
        ledger.record_research(conn, brief_id=brief_id, created_at=now, topics=topic_list, outcome="success", model=MODEL,
                               cost_usd=result.get("cost_usd"), brief={"items": result["items"], "gaps": result["gaps"],
                                                                         "dropped": result["dropped"],
                                                                         "prompt_version": PROMPT_VERSION})
    return {"outcome": "success", "items": result["items"], "brief_id": brief_id, "error_code": None}


def research_news(*, today: date, topics: list[str], count: int, secrets: list[str], executable: Path,
                  runner: headless.Runner = subprocess.run, model: str = MODEL,
                  verifier: Callable[..., tuple[list[dict[str, Any]], dict[str, int]]] | None = None) -> dict[str, Any]:
    """One web call, then selection and page checks. Raises headless.HeadlessError when the call fails."""
    prompt = json.dumps({"today": today.isoformat(), "topics": topics, "count": count, "language": "ko"}, ensure_ascii=False)
    check_outbound(prompt, secrets)
    output, cost = headless.run(prompt, system_prompt=SYSTEM_PROMPT, schema=research.brief_schema(count * 4, 10),
                                executable=executable, model=model, budget_usd=BUDGET_USD, timeout_seconds=TIMEOUT_SECONDS,
                                tools=headless.WEB_TOOLS, runner=runner)
    items, reasons = select(output.get("items", []), today=today, topics=topics)
    items, unreachable = (verifier or sources.verify)(items)
    for reason, number in unreachable.items():
        reasons[reason] = reasons.get(reason, 0) + number
    gaps = [mask_identifiers(str(g))[:200] for g in output.get("gaps", []) if str(g).strip()]
    return {"items": items[:count], "gaps": gaps, "dropped": reasons, "cost_usd": cost}


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
        if published is None or not (today - timedelta(days=FRESH_DAYS) <= published <= today + timedelta(days=1)):
            drop("stale-or-undated")
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
                     "source_title": _clean(item.get("source_title"), 200), "source_url": url, "kind": "fact"})
    return kept, reasons


def allowed_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in HOSTS)


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = mask_identifiers(re.sub(r"(?:https?://|www\.)\S+", "", text))
    return text[:limit].strip()


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
