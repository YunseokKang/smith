"""Event notes (report design §29): a short mail, apart from the daily editions, on the day something happens
that matters to this household. Three events for now:

- the Bank of Korea sets the policy rate (the household carries a variable-rate mortgage): the decision, the
  vote and the governor's guidance from an official source, and what it does to the month's interest;
- the listed holdings move more than a threshold since the last check (market prices and FX together): how
  much, which holdings, and the researched why;
- a KR or US holding files a material disclosure (smith.disclosures, FR-44): the filings read from OpenDART and
  SEC EDGAR by code, with the dividend this household receives or the dilution a new issue means. No model, no web
  research.

Every figure is computed here. Research (one headless call with web tools, sources verified like a brief)
supplies the why and finds the Bank's own page for the rate decision; the decision, the vote and the governor's
words are then read from that page by code (`read_decision`), and only a verbatim quote of the guidance is shown.
A note never proposes: the rules that propose live in the regular reports.

Delivery (review 2026-10-23): one note per event (`event_notes`), claimed atomically
(`ledger.claim_event_note`), marked `unknown`/`sending` before the mail request so a run that dies after Gmail
accepted it never sends again, and every transition fenced by the claim time. A note that failed or could not be
confirmed is retried from the ledger (`ledger.retryable_event_notes`) at later checks inside its window, a move
rebuilt from the window it was first measured over. `check` is what `smith report run-due` calls at the daily check
time and after each report.
"""
import http.client
import json
import logging
import re
import ssl
import subprocess
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from pathlib import Path
from typing import Any, Callable

from smith import dart, disclosures, evidence, gmail, headless, ledger, report_data, report_html, research, sec, sources
from smith.config import local_time
from smith.holdings import STRESS_DRAWDOWN
from smith.payload import PayloadRejected, check_outbound, load_view

logger = logging.getLogger(__name__)

DEFAULT_CHECK_TIME = "16:10"            # After the Korean close; the broker's prices are then the day's closes.
DEFAULT_MOVE_PCT = 2.0                  # Assumption: a 2% move of the listed holdings (prices and FX) is an event.
WINDOW = timedelta(days=3)              # A rate decision older than this belongs to the regular report.
BASELINE_MAX = timedelta(days=4)        # A move is measured since the last check, at most this far back...
BASELINE_DEFAULT = timedelta(days=1)    # ...and over one day when there has been no check yet.
BOK_RESULT_TIME = time(11, 30)          # The decision is out by about 10:00, the press conference at 11:10.
MAX_ATTEMPTS = 3                        # Unconfirmed or failed notes are retried this often, then skipped.
BUILDING = timedelta(minutes=30)        # A note claimed this recently is being built by another run (manual + scheduled).
MODEL = "fable"
BUDGET_USD = "5.00"
TIMEOUT_SECONDS = 900
PROMPT_VERSION = "event-research-v2"  # v2: the decision is read from the official page; guidance as a verbatim quote.
OUTCOMES = {"hold": "동결", "hike": "인상", "cut": "인하"}

_TEXT = lambda limit: {"type": "string", "maxLength": limit}  # noqa: E731 - schema shorthand
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items", "gaps", "decision"],
    "properties": {
        "items": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["headline", "detail", "source_url", "published_on", "kind"],
            "properties": {"headline": _TEXT(200), "detail": _TEXT(600), "source_url": _TEXT(600),
                           "published_on": _TEXT(10), "kind": _TEXT(20)}}},
        "gaps": {"type": "array", "maxItems": 5, "items": _TEXT(300)},
        # The rate decision when asked for ("none" fields otherwise). Rates are percent figures as text ("3.00").
        "decision": {"type": "object", "additionalProperties": False,
                     "required": ["outcome", "new_rate_pct", "previous_rate_pct", "guidance_quote", "source_url"],
                     "properties": {"outcome": _TEXT(10), "new_rate_pct": _TEXT(8), "previous_rate_pct": _TEXT(8),
                                    "guidance_quote": _TEXT(300), "source_url": _TEXT(600)}},
    },
}
SYSTEM_PROMPT = """You are the research analyst of Smith, a read-only private wealth adviser for one Korean
household. Answer the one question in the input JSON from the public web, today being `today`.

Rules:
- Prefer official publishers. For a Bank of Korea decision, the Bank's own press release or the 통화정책방향
  결정문 on bok.or.kr comes first; news outlets only add context. Every item cites one plain https URL you
  actually opened, with that page's publication date (YYYY-MM-DD).
- Items are findings, each one fact or one explanation: headline (one Korean sentence), detail (two or three
  Korean sentences with the figures the page gives), kind (fact, policy, forecast or opinion).
- When `decision_wanted` is true, fill `decision` from the official source: outcome hold, hike or cut; the new
  and the previous policy rate as percent text ("3.00"); guidance_quote: one sentence about the path ahead copied
  word for word from that same page (the decision statement or the governor's press conference text), or "" when
  the page has none; source_url: that bok.or.kr page. The code reads the page itself and drops anything it does
  not find there. If the decision cannot be confirmed from an official source, set outcome to "unknown" and say
  so in gaps. Never guess a figure.
- When `decision_wanted` is false, set every decision field to "none".
- No investment advice and no reference to any household. Write Korean except the enumerated field values."""

Researcher = Callable[..., dict[str, Any]]
Sender = Callable[..., str]
Interpreter = Callable[..., dict[str, Any]]   # disclosure_insight.interpret: (filings, *, today, secrets) -> reading.


@dataclass(frozen=True)
class Event:
    kind: str       # "bok_decision", "portfolio_move" or "disclosure"
    day: date       # The meeting day, or the local day the move or the filings were found on.
    slot: str = ""  # "am"/"pm" for a move (the US session and the Korean one apart); a digest of the newest filing
                    # for disclosures (disclosures.slot), so filings found later the same day are a note of their own.

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.day.isoformat()}" + (f":{self.slot}" if self.slot else "")

    @classmethod
    def from_key(cls, key: str) -> "Event":
        kind, day, *slot = key.split(":")
        return cls(kind, date.fromisoformat(day), slot[0] if slot else "")


def slot_due(conn: Any, config: dict[str, Any], now: datetime) -> datetime | None:
    """Today's local check time when it has passed and no check has run since; None otherwise (or when the
    notes are disabled). `run-due` calls this every tick, so a check runs once a day however late the PC wakes."""
    section = config.get("events") or {}
    if not section.get("enabled", False):
        return None
    local = local_time(now, config["app"]["timezone"])
    hour, minute = (section.get("check_time") or DEFAULT_CHECK_TIME).split(":")
    slot = local.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if local < slot:
        return None
    last = ledger.last_event_check(conn)
    return None if last is not None and last >= slot else slot


def check(db: Path, config: dict[str, Any], *, now: datetime, reason: str, sync: bool = True,
          researcher: Researcher | None = None, sender: Sender = gmail.send, recipient: str | None = None,
          credentials: tuple[str, str, str] | None = None, secrets: list[str] | None = None,
          say: Callable[[str], None] = print, dart_key: str | None = None,
          dart_fetch: dart.Fetcher = dart.default_fetcher, sec_contact: str | None = None,
          sec_fetch: Any = None, interpreter: Interpreter | None = None) -> list[dict[str, Any]]:
    """Look for events since the last check, retry the notes not delivered yet, and send what is due. Returns one
    outcome per event ({"key", "status", "detail"}). `sync` refreshes the holdings and the evidence first (after
    the morning report they are fresh already). `researcher` is None to skip the web research (previews, tests).
    `dart_key` (OpenDART) and `sec_contact` (the e-mail the SEC requires in the User-Agent) enable the disclosure note;
    `interpreter` (disclosure_insight.interpret) adds what each filing means, None to send the facts alone. Nothing is raised: a failure is recorded on the note and
    retried at a later check inside its window."""
    if sync:
        _sync(db, say)
    today = local_time(now, config["app"]["timezone"]).date()
    with closing(ledger.connect(db)) as conn:
        last = ledger.last_event_check(conn)
        baseline = max(last if last is not None else now - BASELINE_DEFAULT, now - BASELINE_MAX)
        found: list[tuple[Event, dict[str, Any] | None]] = list(detect(conn, config, now, baseline))
        view = (load_view(conn, as_of=local_time(now, config["app"]["timezone"]), known_at=now)
                if dart_key or sec_contact else None)
    if view is not None:
        try:
            filings, failures = disclosures.scan_due(db, view, key=dart_key, today=today, now=now, fetch=dart_fetch,
                                                     contact=sec_contact, **({"sec_fetch": sec_fetch} if sec_fetch else {}))
        except Exception as error:  # noqa: BLE001 - the other events still go out; the next check scans again.
            filings, failures = [], [f"공시 확인 실패({type(error).__name__})"]
        for failure in failures:
            say(f"Event check: {failure}")
        if filings:
            found.append((Event("disclosure", today, disclosures.slot(filings)), {"filings": filings}))
    with closing(ledger.connect(db)) as conn:
        ledger.record_event_check(conn, checked_at=now, reason=reason, baseline=baseline, events=[e.key for e, _ in found])
        # The next check measures from this one, so a move whose note failed is never detected again: the ledger
        # is its outbox (review 2026-10-23). Its detail is rebuilt from the window it was claimed with.
        seen = {event.key for event, _ in found}
        found += [(Event.from_key(row["event_key"]), None) for row in ledger.retryable_event_notes(conn, since=today - WINDOW)
                  if row["event_key"] not in seen]
    return [_deliver(db, config, event, detail, now=now, baseline=baseline, researcher=researcher, sender=sender,
                     recipient=recipient, credentials=credentials, secrets=secrets or [], dart_key=dart_key,
                     dart_fetch=dart_fetch, interpreter=interpreter) for event, detail in found]


def detect(conn: Any, config: dict[str, Any], now: datetime, baseline: datetime) -> list[tuple[Event, dict[str, Any]]]:
    """The events in view at `now`: a Bank of Korea rate meeting in the window once its result is out, and a
    move of the listed holdings since `baseline` beyond the configured share. Each with its measured detail."""
    local = local_time(now, config["app"]["timezone"])
    today = local.date()
    found: list[tuple[Event, dict[str, Any]]] = []
    for meeting in evidence.BOK_RATE_MEETINGS:
        if meeting <= today <= meeting + WINDOW and (today > meeting or local.time() >= BOK_RESULT_TIME):
            found.append((Event("bok_decision", meeting), {}))
    threshold = Decimal(str((config.get("events") or {}).get("portfolio_move_pct", DEFAULT_MOVE_PCT))) / 100
    move = measure_move(conn, baseline, now)
    if move is not None and abs(move["ratio"]) >= threshold:
        found.append((Event("portfolio_move", today, "am" if local.hour < 12 else "pm"), move))
    return found


def measure_move(conn: Any, t0: datetime, t1: datetime) -> dict[str, Any] | None:
    """The listed holdings' market and FX change from t0 to t1 against their value at t0 (Hermes-managed and
    unconvertible holdings apart, purchases excluded). None without a valued holding at t0."""
    rows = report_data.holding_changes(conn, t0, t1, t1)
    counted = [r for r in rows if not r["hermes"] and not r["unconverted"] and r["start"]]
    start = sum((r["start"] for r in counted), Decimal(0))
    if not start:
        return None
    market = sum((r["market_change"] for r in counted), Decimal(0))
    fx = sum((r["fx"] for r in counted), Decimal(0))
    return {"changes": counted, "start": start, "market": market, "fx": fx, "change": market + fx,
            "ratio": (market + fx) / start}


def build_note(db: Path, event: Event, detail: dict[str, Any], *, now: datetime, baseline: datetime, tz: str,
               researcher: Researcher | None, secrets: list[str], dart_key: str | None = None,
               dart_fetch: dart.Fetcher = dart.default_fetcher,
               interpreter: Interpreter | None = None) -> dict[str, Any] | None:
    """The note's data for one event, or None when a rate decision is not confirmed yet (neither the statistics
    nor an official source have it). Research failures leave `research_failed` set; the figures still go out."""
    local = local_time(now, tz)
    with closing(ledger.connect_read_only(db)) as conn:
        view = load_view(conn, as_of=local, known_at=now)
    if event.kind == "disclosure":
        if not dart_key and any(f.get("source") != "sec" for f in detail["filings"]):
            return None     # Kept pending: retried once the key is back.
        filings = disclosures.enrich(db, view, local, detail["filings"], key=dart_key, known_at=now, fetch=dart_fetch)
        insight = None
        if interpreter is not None:
            try:
                insight = interpreter(filings, today=local.date(), secrets=secrets)
            except Exception as error:  # noqa: BLE001 - the facts still go out, said to be without the reading.
                logger.warning("Disclosure reading failed for %s: %s", event.key, getattr(error, "code", type(error).__name__))
        for f in filings:
            f["insight"] = None if insight is None else insight["by_id"].get(f["rcept_no"])
        return {"kind": "disclosure", "key": event.key, "day": event.day, "as_of": local, "filings": filings,
                "overview": "" if insight is None else insight["overview"],
                "overview_unchecked": bool(insight and insight.get("overview_unchecked")),
                "insight_sources": [] if insight is None else insight["sources"],
                "research_failed": interpreter is not None and insight is None,
                "research": None if insight is None else {"items": len(insight["by_id"]), "cost_usd": insight["cost_usd"],
                                                          "prompt_version": insight["prompt_version"]}}
    found = None
    if researcher is not None:
        question, wanted = _question(event, local.date(), local_time(baseline, tz).date())
        try:
            found = researcher(question, decision_wanted=wanted, today=local.date(), secrets=secrets,
                               meeting=event.day if event.kind == "bok_decision" else None)
        except (headless.HeadlessError, PayloadRejected) as error:
            logger.warning("Event research failed for %s: %s", event.key, getattr(error, "code", type(error).__name__))
    if event.kind == "bok_decision":
        return _bok_note(view, event, local, found, researched=researcher is not None)
    return _move_note(event, detail, local, local_time(baseline, tz), found, researched=researcher is not None)


def preview(db: Path, config: dict[str, Any], kind: str, *, now: datetime, researcher: Researcher | None = None,
            secrets: list[str] | None = None, dart_key: str | None = None, sec_contact: str | None = None,
            disclosure_days: int = disclosures.SCAN_DAYS, interpreter: Interpreter | None = None) -> tuple[str, str, str] | None:
    """One note from current data, never sent or recorded: the latest rate meeting (from the statistics, or from
    research when `researcher` is given), the holdings' move over the last day whatever its size, or the material
    filings of the KR holdings over the last `disclosure_days` (with `dart_key`), carried before or not."""
    tz = config["app"]["timezone"]
    local = local_time(now, tz)
    baseline = now - BASELINE_DEFAULT
    if kind == "disclosure":
        if not dart_key and not sec_contact:
            return None
        with closing(ledger.connect_read_only(db)) as conn:
            view = load_view(conn, as_of=local, known_at=now)
        kr = [] if not dart_key else disclosures.scan(view, key=dart_key, today=local.date(), now=now,
                                                      cache=dart.cache_path(db), already=set(), days=disclosure_days)[0]
        us = [] if not sec_contact else disclosures.scan_us(view, contact=sec_contact, today=local.date(), now=now,
                                                            cache=sec.cache_path(db), already=set(), days=disclosure_days)[0]
        filings = disclosures.newest_first(kr + us)[:disclosures.MAX_FILINGS * 2]
        if not filings:
            return None
        note = build_note(db, Event("disclosure", local.date(), disclosures.slot(filings)), {"filings": filings}, now=now,
                          baseline=baseline, tz=tz, researcher=None, secrets=secrets or [], dart_key=dart_key,
                          interpreter=interpreter)
        return None if note is None else report_html.render_event(note)
    if kind == "bok":
        meeting = max((m for m in evidence.BOK_RATE_MEETINGS if m <= local.date()), default=None)
        if meeting is None:
            return None
        event, detail = Event("bok_decision", meeting), {}
    else:
        with closing(ledger.connect_read_only(db)) as conn:
            detail = measure_move(conn, baseline, now)
        if detail is None:
            return None
        event = Event("portfolio_move", local.date(), "am" if local.hour < 12 else "pm")
    note = build_note(db, event, detail, now=now, baseline=baseline, tz=tz, researcher=researcher, secrets=secrets or [])
    return None if note is None else report_html.render_event(note)


PageReader = Callable[[str], tuple[str | None, str | None]]  # url -> (final url, page text), both None when unreadable


def research_event(question: str, *, decision_wanted: bool, today: date, secrets: list[str], meeting: date | None = None,
                   executable: Path | None = None, runner: headless.Runner = subprocess.run,
                   verifier: Callable[..., tuple[list[dict[str, Any]], dict[str, int]]] | None = None,
                   pages: PageReader | None = None) -> dict[str, Any]:
    """One research call for an event: findings with verified sources and, when asked, the page the decision is
    on. Code reads that page (`decision_check`: `read_decision` on its text, its final host's tier) so the decision
    is confirmed by the Bank's own words, not by the model's (review 2026-10-23). The decision's source item is kept
    apart from the findings shown (`decision_source`).

    Raises:
        headless.HeadlessError: the call failed or returned output that fails the schema.
        PayloadRejected: the question matched an identifier or secret rule (it never should: it is generic).
    """
    prompt = json.dumps({"today": today.isoformat(), "question": question, "decision_wanted": decision_wanted},
                        ensure_ascii=False)
    check_outbound(prompt, secrets)
    output, cost = headless.run(prompt, system_prompt=SYSTEM_PROMPT, schema=SCHEMA,
                                executable=executable or headless.find_claude(), model=MODEL, budget_usd=BUDGET_USD,
                                timeout_seconds=TIMEOUT_SECONDS, tools=headless.WEB_TOOLS, runner=runner)
    items = [item for item in (_item(raw) for raw in output["items"]) if item is not None]
    decision = output.get("decision") or {}
    url = str(decision.get("source_url", "")).strip()
    check = None
    if decision_wanted and meeting is not None and decision.get("outcome") in OUTCOMES and research._plain_https(url):
        final, text = (pages or read_page)(url)
        if text is not None and final is not None:
            check = {"url": final, "official": research.tier(final) == "official",
                     **read_decision(text, meeting, quote=str(decision.get("guidance_quote", "")))}
            items.append({"headline": f"한국은행 통화정책방향 결정({OUTCOMES[decision['outcome']]})", "detail": "",
                          "source_url": final, "published_on": meeting.isoformat(), "kind": "fact",
                          "tier": research.tier(final), "decision_source": True})
    items, dropped = (verifier or partial(sources.verify, tier=research.tier))(items)
    gaps = [g for g in (research._clean(gap, 300) for gap in output.get("gaps", [])) if g]
    return {"items": items, "gaps": gaps, "decision": decision, "decision_check": check, "dropped": dropped,
            "cost_usd": cost, "prompt_version": PROMPT_VERSION}


def read_page(url: str) -> tuple[str | None, str | None]:
    """The page's final URL and text (smith.sources: public hosts only, bounded size). Both None when it cannot be
    read; the decision then waits for the statistics."""
    try:
        page = sources.fetch(url)
    except (sources.SourceRejected, OSError, ssl.SSLError, http.client.HTTPException, ValueError):
        return None, None
    if page["status"] != 200:
        return None, None
    return page["final_url"], sources.page_text(page["headers"], page["body"])


# The decision sentence of the Bank's statement ("기준금리를 현재의 3.00% 수준에서 유지하여", 2026-10-22) and of the
# governor's opening remarks ("기준금리를 현재의 3.00%로 유지하기로 결정"), and a change ("기준금리를 2.75%에서 3.00%로
# 상향 조정"); the first one on the page is the decision (a later recap of an earlier step is not).
_HOLD = re.compile(r"기준금리를\s*(?:현재의\s*(\d+(?:\.\d+)?)\s*%\s*(?:수준)?|현\s*수준\s*\(\s*(\d+(?:\.\d+)?)\s*%\s*\))"
                   r"\s*(?:로|으로|에서)\s*유지")
_CHANGE = re.compile(r"기준금리를\s*(?:현재의\s*)?(\d+(?:\.\d+)?)\s*%\s*에서\s*(\d+(?:\.\d+)?)\s*%\s*(?:로|으로)\s*(상향|하향|인상|인하)")
# The vote, only from the Bank's own sentences: the statement's "금융통화위원 7명 모두 찬성하였다" or the governor's
# "이 결정은 금통위원 전원 일치였습니다"; a dissent recorded as "…소수의견을 개진하였다". A questioner's "만장일치가
# 나왔는데" or the governor's "소수의견을 내면서 소통한다는" are not votes.
_UNANIMOUS = re.compile(r"(?:전원\s*일치|만장일치)\s*(?:였|이었|로\s*결정|로\s*이루어)|위원\s*\d+\s*명\s*모두\s*찬성")
_DISSENT = re.compile(r"소수\s*의견(?:이|은)?\s*(?:있었|나왔|개진되었|제시되었)|소수\s*의견을\s*(?:개진|제시)하였")
_QUOTE_STRIP = re.compile(r"[\s\"'“”‘’「」『』]")
QUOTE_MIN = 15


def read_decision(text: str, meeting: date, *, quote: str = "") -> dict[str, Any]:
    """What the Bank's page itself says: whether it is dated on the meeting day, the decision (outcome, the rate
    before and after) from its first decision sentence, the vote only from an explicit sentence (unanimous, or
    a dissent recorded), and whether `quote` appears on it word for word (spaces and quotation marks aside)."""
    flat = re.sub(r"\s+", " ", text)
    forms = (f"{meeting.year}.{meeting.month:02d}.{meeting.day:02d}", f"{meeting.year}.{meeting.month}.{meeting.day}",
             f"{meeting.year}. {meeting.month}. {meeting.day}", f"{meeting.year}년 {meeting.month}월 {meeting.day}일",
             meeting.isoformat())
    found: dict[str, Any] = {"dated": any(form in flat for form in forms), "outcome": None, "rate_after": None,
                             "rate_before": None}
    hold, change = _HOLD.search(flat), _CHANGE.search(flat)
    first = min((m for m in (hold, change) if m is not None), key=lambda m: m.start(), default=None)
    if first is not None and first is hold:
        rate = Decimal(hold.group(1) or hold.group(2))
        found.update(outcome="hold", rate_after=rate, rate_before=rate)
    elif first is not None:
        found.update(outcome="hike" if change.group(3) in ("상향", "인상") else "cut", rate_before=Decimal(change.group(1)),
                     rate_after=Decimal(change.group(2)))
    # A recorded dissent outweighs any unanimity wording elsewhere on the page.
    found["vote_unanimous"] = False if _DISSENT.search(flat) else True if _UNANIMOUS.search(flat) else None
    plain = _QUOTE_STRIP.sub("", quote)
    found["quote_found"] = len(plain) >= QUOTE_MIN and plain in _QUOTE_STRIP.sub("", flat)
    return found


def confirmed_decision(found: dict[str, Any] | None, rate_before: Decimal | None) -> dict[str, Any] | None:
    """The rate decision as the code accepts it from research: the official page, dated on the meeting day, states
    it (`decision_check`), the model's outcome and new rate agree with the page, the page's rate before agrees with
    the statistics when they have it, and the step lies on the 0.25%p grid within 0.75%p. The vote is the page's
    explicit sentence and the guidance a quote found on the page, or nothing. Anything less is None: the note waits
    for a later check or for the statistics (review 2026-10-23: a reachable but unrelated page passed)."""
    found = found or {}
    decision, page = found.get("decision") or {}, found.get("decision_check")
    if decision.get("outcome") not in OUTCOMES or not page or not page["official"] or not page["dated"] \
            or page["outcome"] is None:
        return None
    try:
        claimed = Decimal(str(decision["new_rate_pct"]).strip())
    except (InvalidOperation, KeyError):
        return None
    after, before = page["rate_after"], page["rate_before"]
    if page["outcome"] != decision["outcome"] or claimed != after or (rate_before is not None and before != rate_before):
        return None
    change = after - before
    if after % Decimal("0.25") != 0 or abs(change) > Decimal("0.75"):
        return None
    source = next((i for i in found.get("items") or [] if i.get("decision_source")), None)
    quote = research._clean(str(decision.get("guidance_quote", "")), 300) if page["quote_found"] else ""
    return {"outcome": page["outcome"], "rate_after": after, "rate_before": before, "change": change, "source": "research",
            "vote_unanimous": page["vote_unanimous"], "guidance": quote,
            "ref": None if source is None else source.get("ref"), "source_url": page["url"]}


def _bok_note(view: Any, event: Event, local: datetime, found: dict[str, Any] | None, *, researched: bool) -> dict[str, Any] | None:
    row = next((r for r in view.evidence if r["spec"].series_id == evidence.BOK_BASE_RATE), None)
    before = _rate_before(row, event.day)
    decision = None
    if row is not None and row["value"] is not None and row["observed_on"] >= event.day:
        change = row["value"] - before
        decision = {"outcome": "hold" if change == 0 else "hike" if change > 0 else "cut", "rate_after": row["value"],
                    "rate_before": before, "change": change, "source": "ecos", "vote_unanimous": None, "guidance": "",
                    "ref": None, "source_url": None}
    from_research = confirmed_decision(found, before)
    if decision is None:
        decision = from_research
    elif from_research is not None and from_research["rate_after"] == decision["rate_after"]:
        decision.update(vote_unanimous=from_research["vote_unanimous"], guidance=from_research["guidance"])
    if decision is None:
        return None
    variable = next((e.amount for e in view.exposures.exposures if e.key == "variable_rate_debt"), Decimal(0))
    return {"kind": "bok_decision", "key": event.key, "day": event.day, "as_of": local, "decision": decision,
            "variable_total": variable, "monthly_change": variable * decision["change"] / 100 / 12,
            "step": variable * Decimal("0.0025") / 12,
            "next_meeting": evidence.next_policy_meeting(event.day + timedelta(days=1)),
            "items": (found or {}).get("items", []), "gaps": (found or {}).get("gaps", []),
            "research_failed": researched and found is None, "research": _research_summary(found)}


def _rate_before(row: dict[str, Any] | None, day: date) -> Decimal | None:
    """The policy rate on the eve of the meeting: the latest observation when it is older than the meeting, else
    the latest value less the steps on or after the meeting day."""
    if row is None or row["value"] is None:
        return None
    if row["observed_on"] < day:
        return row["value"]
    return row["value"] - sum((delta for moved, delta in row["moves"] if moved >= day), Decimal(0))


def _move_note(event: Event, detail: dict[str, Any], local: datetime, since: datetime, found: dict[str, Any] | None,
               *, researched: bool) -> dict[str, Any]:
    rows = detail["changes"]
    by_market: dict[str, Decimal] = {}
    for r in rows:
        key = r["market"] or "other"
        by_market[key] = by_market.get(key, Decimal(0)) + r["market_change"] + r["fx"]
    movers = sorted(rows, key=lambda r: abs(r["market_change"] + r["fx"]), reverse=True)[:3]
    return {"kind": "portfolio_move", "key": event.key, "day": event.day, "as_of": local, "since": since,
            "start": detail["start"], "change": detail["change"], "ratio": detail["ratio"], "market": detail["market"],
            "fx": detail["fx"], "by_market": by_market,
            "movers": [{"name": r["name"], "market": r["market"], "change": r["market_change"] + r["fx"],
                        "ratio": (r["market_change"] + r["fx"]) / r["start"]} for r in movers],
            "stress_share": abs(detail["ratio"]) / STRESS_DRAWDOWN,
            "items": (found or {}).get("items", []), "gaps": (found or {}).get("gaps", []),
            "research_failed": researched and found is None, "research": _research_summary(found)}


def _question(event: Event, today: date, since: date) -> tuple[str, bool]:
    """The generic research question: no amounts, holdings or identifiers, only the public event."""
    if event.kind == "bok_decision":
        return (f"{event.day:%Y년 %m월 %d일} 한국은행 금융통화위원회 통화정책방향 결정: 기준금리 결정 내용(동결·인상·인하와 새 기준금리 "
                "수준), 표결이 만장일치였는지, 결정 배경, 총재 기자간담회에서 향후 방향에 대해 한 말. 한국은행 공식 자료를 우선 "
                "출처로.", True)
    return (f"{since:%Y년 %m월 %d일}부터 {today:%Y년 %m월 %d일}까지 한국(코스피·코스닥)과 미국(S&P 500·나스닥) 주식시장의 움직임과 "
            "주된 원인, 원/달러 환율의 움직임. 지수 등락률과 원인을 날짜·출처와 함께.", False)


def _item(raw: dict[str, Any]) -> dict[str, Any] | None:
    url = str(raw["source_url"]).strip()
    published = str(raw["published_on"]).strip()
    headline = research._clean(str(raw["headline"]), 200)
    if not research._plain_https(url) or not research._DATE.fullmatch(published) or not headline:
        return None
    kind = research._kind(str(raw["kind"]))
    return {"headline": headline, "detail": research._clean(str(raw["detail"]), 600), "source_url": url,
            "published_on": published, "kind": kind if kind in research.KINDS else "fact", "tier": research.tier(url)}


def _research_summary(found: dict[str, Any] | None) -> dict[str, Any] | None:
    if found is None:
        return None
    return {"items": len(found["items"]), "cost_usd": found.get("cost_usd"), "prompt_version": found.get("prompt_version"),
            "dropped": found.get("dropped")}


def _deliver(db: Path, config: dict[str, Any], event: Event, detail: dict[str, Any] | None, *, now: datetime,
             baseline: datetime, researcher: Researcher | None, sender: Sender, recipient: str | None,
             credentials: tuple[str, str, str] | None, secrets: list[str], dart_key: str | None = None,
             dart_fetch: dart.Fetcher = dart.default_fetcher, interpreter: Interpreter | None = None) -> dict[str, Any]:
    """Claim, build, mark sending, send and record one note. `detail` is None for a note retried from the ledger."""
    window = (None if event.kind != "portfolio_move" or detail is None
              else {"from": baseline.isoformat(), "to": now.isoformat()})
    claimed = ({"window": window} if window is not None
               else {"filings": detail["filings"]} if event.kind == "disclosure" and detail else None)
    with closing(ledger.connect(db)) as conn:
        token, outcome, stored = ledger.claim_event_note(conn, key=event.key, kind=event.kind, event_date=event.day, now=now,
                                                         lease=BUILDING, max_attempts=MAX_ATTEMPTS, details=claimed)
    if token is None:
        return {"key": event.key, "status": outcome, "detail": ""}
    advance = partial(_advance, db, event.key, token)
    at = now
    if event.kind == "portfolio_move":
        # A retried or re-detected move is the move first claimed: rebuilt over its stored window (bitemporal).
        span = (stored or {}).get("window")
        if span is None:
            advance("pending", "skipped", error_code="no-window")
            return {"key": event.key, "status": "skipped", "detail": "no-window"}
        baseline, at = datetime.fromisoformat(span["from"]), datetime.fromisoformat(span["to"])
        if detail is None or span != window:
            with closing(ledger.connect_read_only(db)) as conn:
                detail = measure_move(conn, baseline, at)
        if detail is None:
            advance("pending", "skipped", error_code="no-holdings")
            return {"key": event.key, "status": "skipped", "detail": "no-holdings"}
    if event.kind == "disclosure":
        # The filings the note was claimed with: a retried note carries the same filings (they are marked seen).
        with closing(ledger.connect_read_only(db)) as conn:
            mine = disclosures.owned(conn, event.key, (stored or {}).get("filings") or [])
        if not mine:
            advance("pending", "skipped", error_code="carried-elsewhere")
            return {"key": event.key, "status": "skipped", "detail": "carried-elsewhere"}
        detail = {"filings": mine}
    try:
        note = build_note(db, event, detail or {}, now=at, baseline=baseline, tz=config["app"]["timezone"],
                          researcher=researcher, secrets=secrets, dart_key=dart_key, dart_fetch=dart_fetch,
                          interpreter=interpreter)
    except Exception as error:  # noqa: BLE001 - a failed note is recorded and retried, never raised into the scheduler.
        logger.warning("Event note %s could not be built: %s", event.key, type(error).__name__)
        advance("pending", "pending", error_code=f"build-{type(error).__name__}"[:80])
        return {"key": event.key, "status": "pending", "detail": type(error).__name__}
    if note is None:
        advance("pending", "pending", error_code="unconfirmed")
        return {"key": event.key, "status": "pending", "detail": "unconfirmed"}
    subject, html, text = report_html.render_event(note)
    details = {**_details(note), **({"window": stored["window"]} if (stored or {}).get("window") else {})}
    if recipient is None or credentials is None:
        advance("pending", "failed", error_code="setup", subject=subject, details=details)
        return {"key": event.key, "status": "failed", "detail": "setup"}
    # Committed before the request: from here on the message may reach Gmail, so a run that dies is never retried.
    if not advance("pending", "unknown", error_code="sending", subject=subject, details=details):
        return {"key": event.key, "status": "claim-lost", "detail": ""}
    try:
        message_id = sender(credentials, recipient=recipient, subject=subject, html=html, text=text)
    except gmail.MailError as error:
        status = "unknown" if error.uncertain else "failed"
        advance("unknown", status, error_code=error.code)
        return {"key": event.key, "status": status, "detail": error.code}
    if not advance("unknown", "sent", message_id=message_id):
        logger.warning("Event note %s was sent but its state could not be recorded; it stays unknown", event.key)
    return {"key": event.key, "status": "sent", "detail": subject}


def _advance(db: Path, key: str, token: str, from_status: str, status: str, **fields: Any) -> bool:
    with closing(ledger.connect(db)) as conn:
        return ledger.advance_event_note(conn, key=key, token=token, from_status=from_status, status=status, **fields)


def _details(note: dict[str, Any]) -> dict[str, Any]:
    """What the ledger keeps of a note: its figures as text and the research summary, no identifiers."""
    kept = {key: note[key] for key in ("kind", "day", "monthly_change", "variable_total", "change", "ratio", "start")
            if key in note}
    if note.get("decision"):
        kept["decision"] = {k: v for k, v in note["decision"].items() if k in ("outcome", "rate_after", "rate_before",
                                                                                "source", "vote_unanimous", "source_url")}
    kept["research"] = note.get("research")
    if note.get("filings"):     # What was carried (smith.disclosures.seen), without the household's amounts.
        kept["filings"] = [{**{k: f[k] for k in ("rcept_no", "filed_on", "title", "corp_name", "corp_code", "category",
                                                  "symbols", "url")},
                            **({"source": f["source"]} if f.get("source") else {}),
                            # A cash dividend's pay date, for the daily mail's date line (a date, never an amount).
                            "record_date": (f.get("figures") or {}).get("record_date") if f["category"] == "dividend" else None,
                            "pay_date": (f.get("figures") or {}).get("pay_date")
                            if f["category"] == "dividend" and (f.get("figures") or {}).get("cash") and not f.get("superseded")
                            else None}
                           for f in note["filings"]]
    kept["refs"] = [i.get("source_url") for i in note.get("items") or []]
    return json.loads(json.dumps(kept, default=str, ensure_ascii=False))


def _sync(db: Path, say: Callable[[str], None]) -> None:
    from smith.cli import main as cli_main  # Late: the CLI registers this module's command.

    for argv, label in ((["toss", "sync", "--close-missing", "--db", str(db)], "토스증권 보유 내역"),
                        (["evidence", "sync", "--db", str(db)], "금리·공식 발표")):
        if cli_main(argv) != 0:
            say(f"Event check: {label} sync failed; checking with stored data")
