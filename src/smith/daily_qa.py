"""Answers to replies on the daily-routine mail (FR-31 §6) and the research requests they may carry (FR-33).

A reply in a daily thread gets the daily context only: the day's figures (weather, calendar, tasks, dates), the next two
weeks of events, the morning's verified news and the client's memory. No finance data; a money question is pointed to
the finance mail. The flow is the report Q&A's with one more step: the first model call may ask for research
(`research.needed`), code runs one bounded web call under the FR-33 rules (public, professional, cited, same-name
caution; nothing private), verifies every URL, and a second call writes the answer with the findings. Third-party facts
never enter the client's memory.
"""
import json
import re
import subprocess
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

from smith import agenda, credentials, daily, daily_html, gmail, headless, keep, ledger, memory, people, sources
from smith.config import local_time
from smith.payload import check_outbound, mask_identifiers

MODEL = "fable"
BUDGET_USD = "3.00"
RESEARCH_BUDGET_USD = "6.00"
TIMEOUT_SECONDS = 600
RESEARCH_TIMEOUT_SECONDS = 900
PROMPT_VERSION = "daily-answer-v1"
RESEARCH_PROMPT_VERSION = "daily-research-v1"
WINDOW_DAYS = 14
MAX_FINDINGS = 12
_TEXT = lambda n: {"type": "string", "maxLength": n}  # noqa: E731
ANSWER_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["answer", "follow_up", "remember", "research"],
    "properties": {
        "answer": {"type": "object", "additionalProperties": False, "required": ["text", "refs"],
                   "properties": {"text": _TEXT(6000), "refs": {"type": "array", "maxItems": 20, "items": _TEXT(8)}}},
        "follow_up": {"type": "array", "maxItems": 3, "items": _TEXT(200)},
        "remember": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "additionalProperties": False, "required": ["topic", "fact"],
            "properties": {"topic": _TEXT(20), "fact": _TEXT(memory.MAX_FACT_CHARS)}}},
        # The headless validator knows strings, arrays and objects only: `needed` is "yes" or "no".
        "research": {"type": "object", "additionalProperties": False, "required": ["needed", "subject", "kind", "queries", "anchors"],
                     "properties": {"needed": _TEXT(3), "subject": _TEXT(120),
                                    "kind": {"type": "string", "maxLength": 12},
                                    "queries": {"type": "array", "maxItems": 5, "items": _TEXT(200)},
                                    "anchors": {"type": "array", "maxItems": 6, "items": _TEXT(80)}}},
    },
}
FINDINGS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["findings", "identity", "gaps"],
    "properties": {
        "findings": {"type": "array", "maxItems": MAX_FINDINGS * 2, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["claim", "source_title", "source_url", "published_on", "publisher", "confidence"],
            "properties": {"claim": _TEXT(600), "source_title": _TEXT(300), "source_url": _TEXT(2000),
                           "published_on": _TEXT(32), "publisher": _TEXT(200),
                           "confidence": {"type": "string", "maxLength": 16}}}},
        "identity": {"type": "object", "additionalProperties": False, "required": ["matched_anchors", "note"],
                     "properties": {"matched_anchors": {"type": "array", "maxItems": 6, "items": _TEXT(80)}, "note": _TEXT(600)}},
        "gaps": {"type": "array", "maxItems": 10, "items": _TEXT(300)},
    },
}
SYSTEM_PROMPT = """You are Smith's secretary voice answering the client's reply to this morning's daily mail. Input JSON:
today (the morning's figures and rule outcomes: weather, agenda, reminders, holidays, finance dates, news), window
(events and open tasks for the next two weeks), client_memory (what the client told Smith), client_notes (the client's
own notes that touch the question: their words, use them when they answer it or show what the client thinks),
conversation (earlier turns of this thread), client_question, message_date, and, on a second pass, research_findings
with refs R1, R2, ...

Write in warm, natural Korean, speaking to the client directly, short paragraphs, no emoji. Answer from the input only.
- Smith is read-only: never say an event, task or mail was created, moved, completed or forwarded. When asked to, say
  how the client can do it in the app, in one line.
- Money, holdings, loans, taxes: say that the finance mail's thread is the place ("재무 메일에 답장해 주시면 그 자료로
  답해 드립니다") and answer only the calendar side, if any.
- A question about a person, place, event or company that the input cannot answer is a research request: set
  research.needed = "yes" with the subject, its kind (person, place, event, company or topic), two to five web
  queries, and anchors (the clues you have for identity: organisation, title, city, the event in the calendar). Then
  keep answer.text to one short line saying you are looking it up. Otherwise research.needed = "no" and kind "none". On the second pass (research_findings present) write the full answer: facts first, each with its
  ref in brackets like [R2]; separate 확인됨 (two sources), 한 출처 (single-source) and 미확인; say plainly when the
  identity could not be confirmed (동명이인) and what clue would settle it; never state private matters (address,
  family, health, wealth, contact details).
- remember: up to 3 facts the client states about themselves or their family (tastes, plans, consents); never facts
  about other people from research; topic: profile, family, plan, preference or other.
- follow_up: up to 3 short things the client might ask next. Numbers only from the input; never a URL."""
RESEARCH_PROMPT = """You are a careful research assistant preparing one Korean reader for today's meeting or outing. You have
WebSearch and WebFetch. Input JSON: subject, kind (person|place|event|company|topic), queries, anchors (clues for
identity), today.

Rules (binding):
- Only publicly available pages. For a person: professional facts only (organisation, title, career, public talks,
  papers, interviews, articles). Never search for or record an address, family, health, wealth, contact details,
  private social media. Pages behind a login (LinkedIn, 사람인 profiles) are not readable: do not guess their content.
- Identity: a finding belongs to the subject only when at least two anchors match the page; list the anchors that
  matched in identity.matched_anchors and say in identity.note how sure you are.
- For a place or event: official pages first (organiser, venue, public office): hours, booking, fees, today's
  programme, how to get there. For a restaurant or menu suggestion, only official pages or two independent outlets.
- Each finding: one claim in Korean, its source page (https URL you actually opened), the page title, publisher,
  published_on (YYYY-MM-DD or ""), confidence: confirmed (two independent sources), single-source, unconfirmed.
- Output JSON matching the schema; gaps lists what could not be found."""
_RESEARCH_HINT = re.compile(r"조사|알아봐|찾아봐|검색|상세히|자세히|누구|어떤\s*(?:사람|곳|행사|회사)|정보\s*(?:를|좀)?\s*(?:알려|보고)")

ModelCall = Callable[[str, str, dict[str, Any], str, int, bool], tuple[Any, str | None]]


def answerer(db: Path, config: dict[str, Any], *, now: datetime, tz: str, secrets: list[str],
             executable: Path | None = None, runner: headless.Runner = subprocess.run) -> Callable[..., tuple[str, str, dict[str, Any]]]:
    """The callable `qa.poll` uses for a daily-thread question: (question, history) -> (html, text, result)."""
    cache: dict[str, dict[str, Any]] = {}

    def call(question: Any, history: list[dict[str, str | None]]) -> tuple[str, str, dict[str, Any]]:
        day = local_time(now, tz).date()
        if "context" not in cache:
            cache["context"] = context(db, config, now=now, day=day)
        result = answer(db, cache["context"], question.text, now=now, received_at=question.received_at, history=history,
                        secrets=secrets, tz=tz, executable=executable, runner=runner)
        html, text = render(question.text, result)
        return html, text, result
    return call


def context(db: Path, config: dict[str, Any], *, now: datetime, day: Any) -> dict[str, Any]:
    """The daily thread's material: today's payload, the two-week window, the morning's news, the household's dates."""
    tz = config["app"]["timezone"]
    inputs = daily.collect(db, config, now=now, day=day)
    data = daily.assemble(inputs, day=day, tz=tz, now=now,
                          airways_sensitive=bool(config.get("daily", {}).get("airways_sensitive", False)))
    window: dict[str, Any] = {"events": [], "tasks": [], "failures": {}}
    stored = credentials.load_gmail()
    if stored is not None:
        try:
            token, scopes = gmail.access_token(stored)
            if not agenda.missing_scopes(scopes):
                start = datetime.combine(day, datetime.min.time(), local_time(now, tz).tzinfo)
                events = agenda.events(token, start, start + timedelta(days=WINDOW_DAYS), tz=tz)
                window["events"] = [{"date": e.start.strftime("%Y-%m-%d"), "title": e.title, "all_day": e.all_day,
                                     "start": None if e.all_day else e.start.strftime("%H:%M"),
                                     "location": e.location, "description": e.description} for e in events]
                window["tasks"] = [{"title": t.title, "due": t.due.isoformat() if t.due else None, "list": t.list_title}
                                   for t in agenda.tasks(token, day + timedelta(days=WINDOW_DAYS))]
        except (gmail.MailError, agenda.AgendaError) as error:
            window["failures"]["calendar"] = error.code
    with closing(ledger.connect_read_only(db)) as conn:
        brief = ledger.latest_research(conn, since=now - timedelta(hours=30))
    news = (brief or {}).get("brief", {}).get("items", []) if brief and brief["topics"][0].get("title") == "daily-news" else []
    data["news"] = news
    upcoming = [{"label": o.label, "date": o.on.isoformat(), "milestone": o.milestone}
                for o in people.upcoming(inputs.book or people.parse({}), day, 45)]
    return {"today": daily.payload(data), "window": window, "upcoming_45_days": upcoming, "news_links": {
        n.get("ref"): (n.get("source_title") or n.get("headline"), n.get("source_url")) for n in news}}


def answer(db: Path, ctx: dict[str, Any], question_text: str, *, now: datetime, received_at: datetime,
           history: list[dict[str, str | None]] | None, secrets: list[str], tz: str, executable: Path | None = None,
           runner: headless.Runner = subprocess.run, caller: ModelCall | None = None) -> dict[str, Any]:
    """Write the answer (one or two model calls plus at most one research call).

    Returns {"text", "refs", "links": {ref: (title, url)}, "follow_up", "remember": [(topic, text)], "memos",
    "researched": bool, "problems"}.
    """
    call = caller or _headless_caller(db, now, executable or headless.find_claude(), runner)
    facts = memory.load(memory.directory(db))
    chosen = memory.retrieve(facts, question_text)
    # The client's own Keep notes about the matter asked (daily design §13): a few, masked, never the whole store.
    notes = keep.search(keep.load(keep.directory(db)), question_text, limit=5)
    payload = {**ctx, "client_memory": memory.for_model(chosen), "client_notes": keep.for_model(notes),
               "client_question": mask_identifiers(question_text),
               "message_date": f"{local_time(received_at, tz):%Y-%m-%d}",
               "conversation": [{"client": mask_identifiers(t["client"]), "smith": None if t["smith"] is None else mask_identifiers(t["smith"])}
                                for t in history or []],
               "research_hint": bool(_RESEARCH_HINT.search(question_text))}
    payload.pop("news_links", None)
    text = json.dumps(payload, ensure_ascii=False, default=str)
    check_outbound(text, secrets)
    output, _ = call(text, SYSTEM_PROMPT, ANSWER_SCHEMA, "daily-answer", TIMEOUT_SECONDS, False)
    links: dict[str, tuple[str, str]] = dict(ctx.get("news_links", {}))
    memos: list[str] = []
    problems: list[str] = []
    researched = False
    request = output.get("research") or {}
    if str(request.get("needed", "")).lower() in ("yes", "true") and request.get("kind") in ("person", "place", "event", "company", "topic") \
            and request.get("queries"):
        findings, identity, gaps = research(request, now=now, secrets=secrets, caller=call)
        researched = True
        for item in findings:
            links[item["ref"]] = (item["source_title"] or item["publisher"] or item["ref"], item["source_url"])
        second = dict(payload, research_findings={"items": [{k: v for k, v in f.items() if k != "source_url"} for f in findings],
                                                  "identity": identity, "gaps": gaps})
        second_text = json.dumps(second, ensure_ascii=False, default=str)
        check_outbound(second_text, secrets)
        output, _ = call(second_text, SYSTEM_PROMPT, ANSWER_SCHEMA, "daily-answer-researched", TIMEOUT_SECONDS, False)
        if not findings:
            memos.append("공개 자료에서 확인된 내용을 찾지 못했습니다. 소속이나 직함 같은 단서를 주시면 다시 찾아보겠습니다.")
        elif len(identity.get("matched_anchors", [])) < 2 and request.get("kind") == "person":
            memos.append("동일인 확인이 되지 않아 참고로만 보시기 바랍니다(일치한 단서 " + f"{len(identity.get('matched_anchors', []))}개).")
    answer_text = _clean_text(str((output.get("answer") or {}).get("text", "")))
    if not answer_text:
        raise headless.HeadlessError("empty-answer")
    refs = [r for r in (output.get("answer") or {}).get("refs", []) if r in links]
    stray = re.findall(r"\[(R\d+)\]", answer_text)
    for ref in stray:
        if ref not in links:
            problems.append(f"ref {ref} not in findings")
            answer_text = answer_text.replace(f"[{ref}]", "")
        elif ref not in refs:
            refs.append(ref)
    subject = str(request.get("subject") or "")
    remember = []
    for item in output.get("remember", []) if isinstance(output.get("remember"), list) else []:
        fact = memory.clean(str(item.get("fact", "")))
        if not fact or (subject and subject in fact) or researched and _third_party(fact):
            continue
        remember.append((item.get("topic") if item.get("topic") in memory.TOPICS else "other", fact))
    return {"text": answer_text, "refs": refs, "links": links, "follow_up": [_clean_text(f)[:200] for f in output.get("follow_up", [])][:3],
            "remember": remember[:3], "memos": memos, "researched": researched, "problems": problems}


def research(request: dict[str, Any], *, now: datetime, secrets: list[str], caller: ModelCall,
             verifier: Callable[..., tuple[list[dict[str, Any]], dict[str, int]]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """One bounded web call under the FR-33 rules, then URL checks. Returns (findings with refs, identity, gaps)."""
    prompt = json.dumps({"subject": mask_identifiers(str(request.get("subject", "")))[:120], "kind": request.get("kind"),
                         "queries": [mask_identifiers(str(q))[:200] for q in request.get("queries", [])][:5],
                         "anchors": [mask_identifiers(str(a))[:80] for a in request.get("anchors", [])][:6],
                         "today": now.date().isoformat()}, ensure_ascii=False)
    check_outbound(prompt, secrets)
    try:
        output, _ = caller(prompt, RESEARCH_PROMPT, FINDINGS_SCHEMA, "daily-research", RESEARCH_TIMEOUT_SECONDS, True)
    except headless.HeadlessError as error:
        return [], {"matched_anchors": [], "note": f"조사 실패({error.code})"}, [f"조사 실패({error.code})"]
    candidates = []
    for item in output.get("findings", []) if isinstance(output.get("findings"), list) else []:
        url = str(item.get("source_url", "")).strip()
        claim = _clean_text(str(item.get("claim", "")))[:600]
        if not claim or not url.startswith("https://") or any(ch.isspace() for ch in url):
            continue
        candidates.append({"claim": claim, "source_title": _clean_text(str(item.get("source_title", "")))[:300],
                           "source_url": url, "published_on": str(item.get("published_on", ""))[:10],
                           "publisher": _clean_text(str(item.get("publisher", "")))[:200],
                           "confidence": item.get("confidence") if item.get("confidence") in ("confirmed", "single-source", "unconfirmed") else "unconfirmed",
                           "headline": claim[:120], "detail": claim, "topic": "research", "kind": "fact"})
    kept, _ = (verifier or sources.verify)(candidates[:MAX_FINDINGS * 2])
    findings = [{k: v for k, v in item.items() if k not in ("headline", "detail", "topic", "kind", "check")} for item in kept][:MAX_FINDINGS]
    identity = output.get("identity") if isinstance(output.get("identity"), dict) else {"matched_anchors": [], "note": ""}
    identity = {"matched_anchors": [str(a)[:80] for a in identity.get("matched_anchors", [])][:6],
                "note": _clean_text(str(identity.get("note", "")))[:600]}
    gaps = [_clean_text(str(g))[:300] for g in output.get("gaps", []) if str(g).strip()][:10]
    return findings, identity, gaps


def render(question_text: str, result: dict[str, Any]) -> tuple[str, str]:
    """(html, text) of the reply in the daily mail's warm register, with the sources it leaned on."""
    paragraphs = "".join(f'<p style="margin:0 0 12px;font-size:15px;line-height:1.7;color:{daily_html.BODY}">{escape(p)}</p>'
                         for p in result["text"].split("\n") if p.strip())
    quoted = escape(question_text[:500]).replace("\n", "<br>")
    source_items = [(ref, *result["links"][ref]) for ref in result["refs"] if ref in result["links"]]
    source_html = "" if not source_items else (
        f'<div style="font-size:12px;color:{daily_html.MUTED};margin-top:10px">출처: ' + " · ".join(
            f'<a href="{escape(url, quote=True)}" style="color:{daily_html.ACCENT};text-decoration:none">[{escape(ref)}] {escape(title[:60])}</a>'
            for ref, title, url in source_items if url.startswith("https://")) + "</div>")
    memo_html = "".join(f'<div style="font-size:12px;color:{daily_html.MUTED};margin-top:6px">참고: {escape(m)}</div>' for m in result["memos"])
    follow = "" if not result["follow_up"] else (
        f'<div style="font-size:12px;font-weight:700;color:{daily_html.MUTED};margin:16px 0 4px">이어서 물어보실 만한 것</div>'
        + "".join(f'<div style="font-size:13px;color:{daily_html.BODY}">· {escape(f)}</div>' for f in result["follow_up"]))
    remembered = result["remember"]
    follow += "" if not remembered else (
        f'<div style="font-size:12px;font-weight:700;color:{daily_html.MUTED};margin:16px 0 4px">기억해 두겠습니다</div>'
        + "".join(f'<div style="font-size:13px;color:{daily_html.BODY}">· {escape(t)}</div>' for _, t in remembered))
    html = (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"></head><body style="margin:0;padding:16px;'
            f'background:{daily_html.PAGE};font-family:{daily_html.SANS};color:{daily_html.INK};word-break:keep-all">'
            f'<div style="max-width:600px;margin:0 auto;background:{daily_html.CARD};border:1px solid {daily_html.HAIRLINE};'
            f'border-radius:20px;padding:20px 22px">'
            f'<div style="font-size:12px;font-weight:700;color:{daily_html.ACCENT};margin-bottom:12px">SMITH · 하루 일과 답변</div>'
            f'<div style="border-left:3px solid {daily_html.HAIRLINE};padding:4px 12px;margin-bottom:16px;font-size:13px;color:{daily_html.MUTED}">'
            f'{quoted}</div>{paragraphs}{memo_html}{source_html}{follow}'
            f'<div style="font-size:12px;color:{daily_html.MUTED};margin-top:20px;padding-top:12px;border-top:1px solid {daily_html.HAIRLINE}">'
            f'Smith는 조회 전용입니다: 캘린더·할 일을 바꾸거나 메일을 보내지 않습니다. 재무 질문은 재무 메일에 답장해 주세요. '
            f'이 메일에 다시 답장하시면 이어서 답해 드립니다.</div></div></body></html>')
    text = result["text"] + "".join(f"\n참고: {m}" for m in result["memos"]) \
        + ("\n\n출처: " + ", ".join(f"[{ref}] {url}" for ref, _, url in source_items) if source_items else "") \
        + ("\n\n기억해 두겠습니다: " + " / ".join(t for _, t in remembered) if remembered else "")
    return html, text


def _headless_caller(db: Path, now: datetime, executable: Path, runner: headless.Runner) -> ModelCall:
    def call(text: str, system_prompt: str, schema: dict[str, Any], use_case: str, timeout: int, web: bool) -> tuple[Any, str | None]:
        outcome, code, output, cost = "failure", None, None, None
        try:
            output, cost = headless.run(text, system_prompt=system_prompt, schema=schema, executable=executable, model=MODEL,
                                        budget_usd=RESEARCH_BUDGET_USD if web else BUDGET_USD, timeout_seconds=timeout,
                                        tools=headless.WEB_TOOLS if web else (), runner=runner)
            outcome = "success"
            return output, cost
        except headless.HeadlessError as error:
            code = str(error.code)[:80]
            raise
        finally:
            with closing(ledger.connect(db)) as conn:
                ledger.record_advice_run(conn, run_id=uuid.uuid4().hex, created_at=now, use_case=use_case, question="daily",
                                         payload=text, prompt_version=RESEARCH_PROMPT_VERSION if web else PROMPT_VERSION,
                                         model=MODEL, cost_usd=cost, outcome=outcome, error_code=code,
                                         advice=None if output is None else json.dumps(output, ensure_ascii=False))
    return call


def _clean_text(text: str) -> str:
    text = mask_identifiers(re.sub(r"(?:https?://|www\.)\S+", "", text))
    return "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()


_PERSON = re.compile(r"(?<![가-힣])([가-힣]{2,4})\s*(?:님|씨|대표|교수|이사|팀장|부장|과장|박사|변호사|의원)")
_TITLE_WORDS = {"교수", "대표", "이사", "팀장", "부장", "과장", "박사", "변호사", "의원", "선생", "고객", "사장", "회장", "원장", "담당"}
_FAMILY = re.compile(r"와이프|아내|남편|아버지|어머니|아빠|엄마|장인|장모|여동생|매제|처남|조카|부모")


def _third_party(fact: str) -> bool:
    """A fact that reads as being about someone outside the household: a name followed by a title or honorific
    ("김민수 교수"), not a bare title ("교수님") and not a family word."""
    if _FAMILY.search(fact):
        return False
    return any(match.group(1) not in _TITLE_WORDS for match in _PERSON.finditer(fact))
