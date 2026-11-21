"""The Saturday letter's "나의 소비" card: the stored card statements read from a different angle each week (FR-60,
docs/spending-design.md §5).

The client asked for three things: a feel for where the money goes by area, whether the same money could be spent
better or a benefit is being missed, and what to add or drop, in money, health and other terms. One statement lasts
about four Saturdays, so each week takes one angle (`ANGLES`), in order, until the next statement arrives.

Every amount on the card is computed in `spending`. The model writes a short note from an allowlisted payload
(category totals, a few merchant names, what the client said); a won amount in its note that the payload does not
hold is dropped with its paragraph, unless the paragraph cites a public page (a card's published terms).
"""
import json
import logging
import os
import subprocess
import uuid
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from smith import headless, korean, ledger, memory, spending
from smith.payload import PayloadRejected, check_outbound

logger = logging.getLogger(__name__)

MODEL = headless.MODEL
BUDGET_USD = "2.00"
TIMEOUT_SECONDS = 600
PROMPT_VERSION = "spending-note-v2"   # v2: rounded amounts, a short headline (first sample 2026-11-21).
ANGLES = ("feel", "benefits", "wellbeing", "trim")
ANGLE_LABEL = {"feel": "어디에 얼마나 쓰는가", "benefits": "같은 돈을 더 잘 쓰는 법", "wellbeing": "건강과 생활 리듬",
               "trim": "보탤 것과 뺄 것"}
_BRIEF = {
    "feel": "Give the reader a feel for where the money goes: the three or four areas that matter, what drives each "
            "(many small purchases or a few large ones), and how the newest month differs from the usual one. No advice "
            "to cut anything this week; the point is the picture.",
    "benefits": "Same money, better spent. Look up the card's published benefits (the card's name is in `card`; use the "
                "issuer's own page or an official product page, and put that page in `source_url`). Say which of the "
                "reader's real spending areas earn or miss a benefit, whether the annual fee pays for itself on this "
                "pattern, and one or two concrete ways to get more from the same spending (a benefit left unused, a "
                "cheaper way to pay for something they already buy). If you cannot open an official page, say what the "
                "reader should check and where, without guessing terms.",
    "wellbeing": "Read the pattern as a way of living, not as money: what it suggests about meals, movement, sleep and "
                 "time with people (late taxis, meals out, coffee rhythm, treatment the reader pays for regularly, "
                 "books and exhibitions). Offer one or two small, concrete adjustments that fit `client_notes`, "
                 "`health_constraints` and `client_memory`. No moralizing, and nothing medical beyond everyday habits.",
    "trim": "What to add, strengthen or drop. Name spending that returns little for its cost (a subscription, a fee, a "
            "repeated small leak) and spending worth protecting or growing (health, learning, experiences the reader "
            "keeps choosing). Be decisive: one thing to drop or renegotiate, one thing to keep or add, each with its "
            "monthly amount.",
}
_TEXT = lambda n: {"type": "string", "maxLength": n}  # noqa: E731
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["headline", "points"],
    "properties": {
        "headline": _TEXT(110),
        "points": {"type": "array", "minItems": 2, "maxItems": 4, "items": {
            "type": "object", "additionalProperties": False, "required": ["title", "text"],
            "properties": {"title": _TEXT(30), "text": _TEXT(280), "source_url": _TEXT(300)}}},
        "try_this_week": _TEXT(140),
    },
}
SYSTEM_PROMPT = """You are the world's best analyst of personal spending patterns, writing one short note for one
reader in Korea inside their Saturday letter from Smith, a personal adviser. The reader is not a finance expert and
wants plain, concrete observations they could not see themselves.

The input is JSON. `months` are the reader's card statements (one card only: other cards, cash and bank transfers are
not there, so never call a total "all your spending"). `spent` is before discounts; purchases paid early are included,
and `client_notes` says why some of them are not the reader's own consumption (shared bills, insured costs). Write
about one angle only, given in `angle` and `angle_brief`. `earlier_this_statement` lists what earlier notes on the
same statement already said: do not repeat them.

Rules:
- Every won amount and count you write must be a figure in the input (rounding to 만 원 is fine). Do not add, average
  or project figures yourself; `average_3m` and `usual_before_latest` are given. The one exception is a public term you
  read on a page you cite in that point's `source_url`.
- Name real things: the area, the shop, the count, the amount. No "지출이 많습니다" without the figure beside it.
- An observation is not a verdict. Never scold; never use 위반, 과소비, 낭비. Say what the pattern is and what a small
  change would give.
- Write an amount of 100,000 won or more rounded to 만 원 ("71만 원"), smaller ones as they are ("27,000원"). A point
  carries at most three or four figures: the ones that make the pattern visible, not every line of the statement.
- headline: one short sentence (under 50 Korean characters) that carries the week's finding. points: two to four, each a short title and two or three
  sentences. try_this_week: one small thing the reader could do in the coming week, or leave it out.
- Korean, 합쇼체 addressed to 고객님, no emoji, no markdown.

""" + korean.STYLE


def folder(db: Path) -> Path:
    return Path(db).parent / "spending"


def _weeks(where: Path) -> list[dict[str, Any]]:
    try:
        stored = json.loads((where / "letters.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [w for w in stored if isinstance(w, dict)] if isinstance(stored, list) else []


def _week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def picture(where: Path, today: date) -> dict[str, Any] | None:
    """What the card shows this week, or None without a stored statement.

    {"months": the newest three, "latest", "usual", "average", "angle", "first_week", "note": this week's note or None}.
    The angle is the one already written this week, else the first the newest statement has not had yet.
    """
    statements = spending.load(where, last=6)
    whole = spending.summary(statements, spending.load_own(where))
    if whole is None:
        return None
    months = whole["months"][-3:]
    latest = months[-1]
    weeks = [w for w in _weeks(where) if w.get("settled") == latest["settled"]]
    this = next((w for w in weeks if w.get("week") == _week(today)), None)
    used = [w.get("angle") for w in weeks]
    angle = this["angle"] if this else next((a for a in ANGLES if a not in used), ANGLES[len(used) % len(ANGLES)])
    average = {key: round(sum(m["categories"].get(key, {}).get("amount", 0) for m in months) / len(months))
               for key in spending.CATEGORIES}
    return {"months": months, "latest": latest, "usual": whole["usual"], "recurring": whole["recurring"],
            "average": {k: v for k, v in average.items() if v}, "angle": angle,
            "first_week": not [w for w in weeks if w is not this], "note": this.get("note") if this else None,
            "earlier": [w["note"] for w in weeks if w is not this and isinstance(w.get("note"), dict)]}


def payload(view: dict[str, Any], *, today: date, client_notes: list[str], health_constraints: list[str],
            client_memory: list[dict[str, Any]], card: list[dict[str, Any]]) -> dict[str, Any]:
    """The allowlisted model input. Merchants of the fixed and the unsorted groups are left out: a maintenance-fee line
    names the apartment, and an unsorted name may be a building or a person."""
    label = spending.CATEGORIES

    def month(m: dict[str, Any]) -> dict[str, Any]:
        groups = {label[key]: {"amount": g["amount"], "count": g["count"], "share": g["share"],
                               **({} if key in spending.FIXED or key == "other" else {"merchants": g["merchants"]})}
                  for key, g in m["categories"].items()}
        return {"period": m["label"], "spent": m["spent"], "fixed": m["fixed"], "chosen": m["spent"] - m["fixed"],
                "paid_early": m["prepaid"], "billed": m["billed"], "benefits_received": m["benefits"],
                "benefit_total": m["benefit_total"], "weekend": m["weekend"], "under_10000_won": m["small"],
                "overseas": m["overseas"], "categories": groups,
                "largest": [{**line, "category": label[line["category"]]} for line in m["largest"]
                            if line["category"] != "other"]}

    return {
        "angle": ANGLE_LABEL[view["angle"]], "angle_brief": _BRIEF[view["angle"]], "today": today.isoformat(),
        "months": [month(m) for m in view["months"]],
        "average_3m": {label[k]: v for k, v in view["average"].items()},
        "usual_before_latest": {label[k]: v for k, v in view["usual"].items()},
        "every_month": [{"merchant": r["merchant"], "category": label[r["category"]], "monthly": r["monthly"]}
                        for r in view["recurring"] if r["category"] not in spending.FIXED and r["category"] != "other"],
        "card": card, "client_notes": client_notes, "health_constraints": health_constraints,
        "client_memory": client_memory,
        "earlier_this_statement": [{"headline": n.get("headline"), "titles": [p.get("title") for p in n.get("points", [])]}
                                   for n in view["earlier"]],
    }


def _card(where: Path) -> list[dict[str, Any]]:
    """The card's own name as the statement prints it on a fee line (the search hint for its published benefits)."""
    own = spending.load_own(where)
    fees = {e.merchant: e.spent for s in spending.load(where, last=13) for e in s.entries
            if e.fee and "연회비" in e.merchant and spending.categorize(e, own) == "fee"}
    return [{"issuer": "삼성카드", "fee_line": name, "annual_fee": won} for name, won in fees.items()]


def verify(output: dict[str, Any], sent_text: str) -> dict[str, Any] | None:
    """The note with every paragraph whose won amounts the payload holds (or that cites a public page); None when
    fewer than one paragraph is left."""
    from smith import narrative

    evidence = narrative._amounts(sent_text)

    def held(text: str) -> bool:
        return all(any(abs(value - known) <= max(precision, known * Decimal("0.03")) for known in evidence)
                   for _, _, value, precision in narrative._money(text) if value >= 1000)

    fixes: list[dict[str, str]] = []
    points = []
    for point in output.get("points", []):
        source = str(point.get("source_url") or "")
        cited = source.startswith("https://")
        if not (cited or held(point["text"])):
            continue
        points.append({"title": korean.tidy(point["title"], fixes), "text": korean.tidy(point["text"], fixes),
                       **({"source_url": source} if cited else {})})
    if not points:
        return None
    note = {"headline": korean.tidy(output["headline"], fixes) if held(output["headline"]) else "", "points": points}
    extra = output.get("try_this_week")
    if extra and held(extra):
        note["try_this_week"] = korean.tidy(extra, fixes)
    return note


def write(db: Path, *, today: date, now: datetime, secrets: list[str], executable: Path | None = None,
          runner: headless.Runner = subprocess.run, model: str = MODEL) -> dict[str, Any]:
    """Write this week's note unless it exists, with the audit in advice_runs. Never raises for a model failure.

    Returns {"outcome": "written" | "kept" | "nothing" | "failure", "error_code"}.
    """
    from smith import health

    where = folder(db)
    view = picture(where, today)
    if view is None:
        return {"outcome": "nothing", "error_code": None}
    if view["note"] is not None:
        return {"outcome": "kept", "error_code": None}
    try:
        constraints = [c.text for c in health.load(health.path(db)).constraints]
    except (health.HealthError, OSError):
        constraints = []
    facts = memory.retrieve(memory.load(memory.directory(db)), "소비 지출 취향 좋아하는 커피 술 운동 건강 여행 책 구독 카드")
    text = json.dumps(payload(view, today=today, client_notes=spending.notes(where), health_constraints=constraints,
                              client_memory=memory.for_model(facts), card=_card(where)), ensure_ascii=False)
    run_id, cost, outcome, error_code, note = f"spending-{uuid.uuid4().hex[:12]}", None, "failure", None, None
    try:
        check_outbound(text, secrets)
        output, cost = headless.run(text, system_prompt=SYSTEM_PROMPT, schema=SCHEMA,
                                    executable=executable or headless.find_claude(), model=model, budget_usd=BUDGET_USD,
                                    timeout_seconds=TIMEOUT_SECONDS, runner=runner,
                                    tools=headless.WEB_TOOLS if view["angle"] == "benefits" else ())
        note = verify(output, text)
        outcome, error_code = ("success", None) if note else ("failure", "no-supported-paragraph")
    except PayloadRejected:
        error_code = "payload-rejected"
    except headless.HeadlessError as error:
        error_code = str(error)
    finally:
        with closing(ledger.connect(db)) as conn:
            ledger.record_advice_run(conn, run_id=run_id, created_at=now, use_case="spending-note", question=view["angle"],
                                     payload=text, prompt_version=PROMPT_VERSION, model=model, cost_usd=cost,
                                     outcome=outcome, error_code=error_code,
                                     advice=None if note is None else json.dumps(note, ensure_ascii=False))
    if note is None:
        return {"outcome": "failure", "error_code": error_code}
    _keep(where, {"week": _week(today), "settled": view["latest"]["settled"], "angle": view["angle"], "note": note})
    return {"outcome": "written", "error_code": None}


def _keep(where: Path, entry: dict[str, Any]) -> None:
    weeks = [w for w in _weeks(where) if (w.get("week"), w.get("settled")) != (entry["week"], entry["settled"])]
    temporary = where / "letters.tmp"
    temporary.write_text(json.dumps((weeks + [entry])[-60:], ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, where / "letters.json")


def attach(db: Path, data: dict[str, Any], *, now: datetime, secrets: list[str] | None = None) -> str | None:
    """Put the card's picture on the client's own life letter (`data["own_spending"]`), writing this week's note first
    when `secrets` is given (the narrative stage). Returns a log line for a failure, else None. Never raises."""
    if data.get("kind") != "life" or data.get("audience", "self") != "self":
        return None
    today = data["as_of"].date() if isinstance(data.get("as_of"), datetime) else now.date()
    problem = None
    try:
        if secrets is not None:
            result = write(db, today=today, now=now, secrets=secrets)
            if result["outcome"] == "failure":
                problem = f"Spending note failed: {result['error_code']}"
        data["own_spending"] = picture(folder(db), today)
    except Exception as error:  # noqa: BLE001 - a card must never stop the letter
        logger.warning("spending card unavailable: %s", type(error).__name__)
        return f"Spending card unavailable: {type(error).__name__}"
    return problem
