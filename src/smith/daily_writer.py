"""The writer of the daily mail (FR-31): a model turns the code's figures into a warm morning voice — a friendly headline
and weather sentence, gift and event ideas for the day's anniversaries, two-line news summaries and the sentence of the
day — and code checks that nothing new slipped in.

What the model may not do is enforced here, not only asked: no number that is not in its input, no link, no quotation
hung on a person's name, no gift idea for a person whose tastes the client never mentioned (it must ask instead), no
idea for a day that is not in today's reminders, no summary for a news item that was not verified. A field that fails is
dropped and the mail falls back to the code's own line for it; the mail never waits for a repair.
"""
import json
import re
import subprocess
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from smith import daily, headless, keep, ledger, memory
from smith.payload import check_outbound, mask_identifiers

MODEL = "fable"
BUDGET_USD = "3.00"
TIMEOUT_SECONDS = 600
# v2 (user review 2026-10-25): the sentence grows out of the client's own Keep notes, a 역사의 오늘 aside, news at half length.
PROMPT_VERSION = "daily-writer-v2"
LIMITS = {"headline": 30, "weather_line": 180, "agenda_line": 140, "idea": 220, "summary": 130, "sentence": 120,
          "tasks_line": 140, "history": 170}
ASK_TASTES = "취향을 알려 주시면 다음부터 더 맞춰 드릴게요."
_TEXT = lambda n: {"type": "string", "maxLength": n}  # noqa: E731
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["headline", "weather_line", "agenda_line", "anniversaries", "news", "sentence", "thought_id", "history",
                 "tasks_line"],
    "properties": {
        "headline": _TEXT(LIMITS["headline"] * 2), "weather_line": _TEXT(LIMITS["weather_line"] * 2),
        "agenda_line": _TEXT(LIMITS["agenda_line"] * 2),
        "anniversaries": {"type": "array", "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False, "required": ["key", "idea"],
            "properties": {"key": _TEXT(80), "idea": _TEXT(LIMITS["idea"] * 2)}}},
        "news": {"type": "array", "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False, "required": ["ref", "summary"],
            "properties": {"ref": _TEXT(8), "summary": _TEXT(LIMITS["summary"] * 2)}}},
        "sentence": _TEXT(LIMITS["sentence"] * 2), "thought_id": _TEXT(120),
        "history": {"type": "object", "additionalProperties": False, "required": ["year", "line"],
                    "properties": {"year": {"type": "integer"}, "line": _TEXT(LIMITS["history"] * 2)}},
        "tasks_line": _TEXT(LIMITS["tasks_line"] * 2),
    },
}
SYSTEM_PROMPT = """You write the morning mail of Smith, a personal secretary, for one reader in Korea. Input JSON holds
today's figures and rule outcomes (weather, advice.items and advice.cautions decided by code, the calendar, reminders
with their keys, verified news items with refs, holidays, finance dates) and client_memory (what the client said about
the family's tastes and habits). Output JSON matching the schema, every text in warm, natural Korean, speaking to the
reader directly without a title or honorific name; short sentences; no emoji.

- headline: under 24 characters, a friendly version of advice.headline (the sky, the temperature feel, the one thing to
  take), e.g. "일교차 큰 맑은 날, 외투 하나". With a severe warning, name it plainly.
- weather_line: one or two sentences. Mention only the items in advice.items and the cautions in advice.cautions; you may
  describe the sky and temperatures from the figures given. Add nothing else to take or avoid. If weather is null,
  return "".
- agenda_line: one sentence on the shape of the day from agenda (how many events, the first timed one, overlaps or tight
  gaps, tasks due today); "" when there is nothing.
- anniversaries: one entry per reminder key given (and none for other days). idea: a concrete gift or outing suggestion
  in one or two sentences, using only facts in client_memory and family_notes (the client's own notes mentioning that
  person) about them; for a milestone (환갑·칠순·돌·주년) suggest the occasion too. When neither says anything about that
  person's tastes, do not guess: write what information would help and ask for it.
- news: one entry per news ref given, summary in one short sentence, two at most, within 120 characters, from that
  item's headline and detail only; no new facts.
- sentence: one sentence for the day that grows out of the client's own thinking. `thoughts` holds a few of the
  client's own notes (their words): pick the one that fits today, carry its direction forward in one fresh, well-made
  sentence — you may echo a short phrase of that note — and set thought_id to its id. When none fits, write your own
  encouraging sentence and set thought_id to "". Never a famous person's quotation, never a name.
- history: from history_events pick the one event most worth a smile or a thought this morning (a Korean one when it
  is as good) and write one or two lines in Korean on what happened and why it is remembered; year must be that event's
  year from the list. With an empty list, year 0 and line "".
- tasks_line: one gentle sentence when tasks_overdue_total > 0 (no scolding), else "".

Numbers: use only numbers that appear in the input. Never write a URL, an e-mail address or a phone number."""


def write_and_record(db: Path, data: dict[str, Any], *, now: datetime, secrets: list[str], executable: Path | None = None,
                     runner: headless.Runner = subprocess.run, model: str = MODEL) -> dict[str, Any]:
    """Prose for `data` with the audit in advice_runs; never raises for a model failure.

    Returns {"prose": dict | None, "problems": [...], "error_code": str | None, "cost_usd"}.
    """
    facts = memory.load(memory.directory(db))
    query = " ".join([r.occurrence.label for r in data["reminders"]] + ["선물 취향 좋아하는"])
    chosen = memory.retrieve(facts, query)
    # The client's Keep notes that mention the people of today's reminders (daily design §13): tastes in their own words.
    notes = keep.load(keep.directory(db))
    family_notes = {r.occurrence.key: keep.for_model(keep.search(notes, r.occurrence.label.split()[0], limit=2))
                    for r in data["reminders"]}
    payload = {**daily.payload(data), "client_memory": memory.for_model(chosen),
               "family_notes": {k: v for k, v in family_notes.items() if v},
               "reminder_keys": [r.occurrence.key for r in data["reminders"]],
               "news_refs": [n.get("ref") for n in (data["news"] or [])]}
    text = json.dumps(payload, ensure_ascii=False, default=str)
    check_outbound(text, secrets)
    run_id, cost, outcome, error_code, prose, problems = f"daily-{uuid.uuid4().hex[:12]}", None, "failure", None, None, []
    try:
        output, cost = headless.run(text, system_prompt=SYSTEM_PROMPT, schema=SCHEMA, executable=executable or headless.find_claude(),
                                    model=model, budget_usd=BUDGET_USD, timeout_seconds=TIMEOUT_SECONDS, runner=runner)
        known = _tastes_known(data, chosen)
        known.update({k: True for k, v in family_notes.items() if v})
        prose, problems = verify(output, data, text, tastes_known=known)
        outcome = "success"
        if prose.get("thought_id"):
            keep.mark_used(keep.directory(db), prose["thought_id"], data["day"])
    except headless.HeadlessError as error:
        error_code = str(error)
    finally:
        with closing(ledger.connect(db)) as conn:
            ledger.record_advice_run(conn, run_id=run_id, created_at=now, use_case="daily-writer", question="daily",
                                     payload=text, prompt_version=PROMPT_VERSION, model=model, cost_usd=cost, outcome=outcome,
                                     error_code=error_code, advice=None if prose is None else json.dumps(prose, ensure_ascii=False))
    return {"prose": prose, "problems": problems, "error_code": error_code, "cost_usd": cost}


def verify(output: dict[str, Any], data: dict[str, Any], payload_text: str, *,
           tastes_known: dict[str, bool]) -> tuple[dict[str, Any], list[str]]:
    """The prose the mail may use, and what was dropped and why (for the system notes)."""
    problems: list[str] = []
    prose: dict[str, Any] = {}
    for field in ("headline", "weather_line", "agenda_line", "sentence", "tasks_line"):
        text = _clean(output.get(field), LIMITS[field])
        if not text:
            continue
        if _foreign_numbers(text, payload_text):
            problems.append(f"{field}: number not in input")
            continue
        if field == "sentence" and _attributed(text):
            problems.append("sentence: attributed quotation")
            continue
        prose[field] = text
    if data["weather"] is None:
        prose.pop("weather_line", None)
        prose.pop("headline", None)
    keys = {r.occurrence.key for r in data["reminders"]}
    ideas = []
    for item in output.get("anniversaries", []) if isinstance(output.get("anniversaries"), list) else []:
        key, idea = str(item.get("key", "")), _clean(item.get("idea"), LIMITS["idea"])
        if key not in keys or not idea:
            problems.append(f"anniversaries: unknown key or empty ({key[:40]})")
            continue
        if _foreign_numbers(idea, payload_text):
            problems.append(f"anniversaries: number not in input ({key[:40]})")
            continue
        if not tastes_known.get(key, False) and "취향" not in idea:
            idea = (idea.rstrip("。.") + ". " + ASK_TASTES)[:LIMITS["idea"] + len(ASK_TASTES) + 2]
        ideas.append({"key": key, "idea": idea})
    prose["anniversaries"] = ideas
    refs = {n.get("ref") for n in (data["news"] or [])}
    summaries = []
    for item in output.get("news", []) if isinstance(output.get("news"), list) else []:
        ref, summary = str(item.get("ref", "")), _clean(item.get("summary"), LIMITS["summary"])
        if ref not in refs or not summary:
            problems.append(f"news: unknown ref or empty ({ref})")
            continue
        if _foreign_numbers(summary, payload_text):
            problems.append(f"news: number not in input ({ref})")
            continue
        summaries.append({"ref": ref, "summary": summary})
    prose["news"] = summaries
    ids = {t.id for t in data.get("thoughts", [])}
    thought_id = str(output.get("thought_id") or "")
    prose["thought_id"] = thought_id if thought_id in ids and "sentence" in prose else ""
    aside = output.get("history") if isinstance(output.get("history"), dict) else {}
    years = {e.year for e in data.get("history_events", [])}
    line = _clean(aside.get("line"), LIMITS["history"])
    year = aside.get("year")
    if line and isinstance(year, int) and year in years and not _foreign_numbers(line, payload_text):
        prose["history"] = {"year": year, "line": line}
    elif line:
        problems.append("history: year not in the list or a number not in input")
    return prose, problems


def _tastes_known(data: dict[str, Any], facts: list[memory.Fact]) -> dict[str, bool]:
    """Whether any remembered fact mentions the person of each reminder (its label), so an idea may lean on it."""
    known = {}
    for reminder in data["reminders"]:
        label = reminder.occurrence.label.split()[0]   # "아버지 생신" -> "아버지"
        known[reminder.occurrence.key] = any(label in f.text for f in facts)
    return known


def _clean(value: Any, limit: int) -> str:
    text = mask_identifiers(re.sub(r"(?:https?://|www\.)\S+", "", str(value or "")))
    text = " ".join(text.replace("[식별정보 제거]", "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _foreign_numbers(text: str, payload_text: str) -> list[str]:
    """Numbers of two or more digits (or decimals) that the input never mentions."""
    return [n for n in re.findall(r"\d+(?:\.\d+)?", text) if (len(n) >= 2 or "." in n) and n not in payload_text]


def _attributed(text: str) -> bool:
    return bool(re.search(r"[-–—]\s*[가-힣A-Za-z.·\s]{2,30}$", text.strip()) or re.search(r"(?:말했|했다|says|said)\b", text)
                or re.search(r"[가-힣A-Za-z]{2,20}(?:의|이|가)\s*(?:말|명언)", text))
