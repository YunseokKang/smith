"""The client's decision on a proposal, answered by a reply to the letter (report design §45, FR-58).

A letter numbers its proposal cards through the whole mail and delivery keeps each number with its proposal instance
(`ledger.asked_proposal`). A reply that is nothing but decisions on those numbers ("1번 할게", "2번 보류, 3번 했어") is
read here by a fixed grammar, recorded in the proposal ledger and confirmed with a fixed text: no model reads it, so
no model output can make, change or drop a decision. Anything else in the reply (a question, a reason, a greeting)
makes it an ordinary question for the answering model, which records nothing and says how to decide.

Only the client's replies to their own letters come here; the spouse's replies record nothing (§42.11).
"""
import re
from contextlib import closing
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from smith import ledger

MAX_CHARS = 200   # A decision reply is short; a longer text is a question.
# What each decision is called in a reply. Narrow on purpose: the whole reply must be made of these, so a question
# that merely contains one ("2번 안 해도 돼?") is not a decision. Within a group the longer wording comes first
# (the pattern takes the first alternative that fits).
_VERBS = {
    "accepted": r"진행할게요?|진행하겠습니다|진행|실행할게요?|실행하겠습니다|실행|할게요?|할께요?|하겠습니다|하겠어요?",
    "declined": (r"보류할게요?|보류하겠습니다|보류|나중에 ?할게요?|나중에|미룰게요?|미루겠습니다|안 ?할게요?|안 ?할래요?|"
                 r"안 ?하겠습니다|안 ?합니다|안 ?해요?|패스"),
    "done": r"완료했어요?|완료했습니다|완료|했어요?|했습니다|끝냈어요?|끝냈습니다|마쳤어요?|마쳤습니다",
}
# One card number with what may join it to the next, then the decision. Each is matched once at a position and nothing
# after the digits can fail, so reading is linear in the text (Codex review 2026-11-19: one pattern with a repeated
# group took a second to reject thirty digits).
_NUMBER = re.compile(r"\s*(\d+)\s*번?\s*(?:,|과|와|랑|하고)?\s*")
_DECISION = re.compile(r"(?:은|는|도|만)?\s*(?:" + "|".join(f"(?P<{status}>{words})" for status, words in _VERBS.items())
                       + r")\s*[,!~]*\s*")
_BREAKS = re.compile(r"[\n.;]+|그리고")
MAX_DIGITS = 2   # Cards are numbered 1 to a handful; a longer number is not split into shorter ones, it is no decision.
LABELS = {"accepted": "진행 중으로 기록했습니다", "declined": "보류로 기록했습니다", "done": "완료로 기록했습니다"}


def read(text: str) -> list[tuple[int, str]] | None:
    """The decisions a reply states as (card number, status), in order, or None when the reply is anything but
    decisions. Every part of the text must be a decision; a number named twice keeps its last decision."""
    text = text.strip()
    if not text or len(text) > MAX_CHARS:
        return None
    found: dict[int, str] = {}
    for part in (p for p in _BREAKS.split(text) if p.strip()):
        position = 0
        while position < len(part):
            numbers = []
            while (number := _NUMBER.match(part, position)) is not None:
                if len(number.group(1)) > MAX_DIGITS:
                    return None
                numbers.append(int(number.group(1)))
                position = number.end()
            decision = _DECISION.match(part, position)
            if not numbers or decision is None:
                return None
            status = next(s for s in _VERBS if decision.group(s))
            for card in numbers:
                found.pop(card, None)   # Re-inserted below, so the order is that of the last mention.
                found[card] = status
            position = decision.end()
    return list(found.items()) or None


def apply(db: Path, report_id: str | None, decisions: list[tuple[int, str]], *, now: datetime,
          source: str = "") -> list[dict[str, Any]]:
    """Record each decision on the proposal the replied-to report showed under that number. One outcome per decision:
    recorded, unchanged, superseded (the proposal was raised anew since that report) or missing (no such card).
    `source` is the reply's message id: a reply applied once is never applied again, so a retried confirmation cannot
    undo a later reply's decision."""
    outcomes = []
    with closing(ledger.connect(db)) as conn:
        for number, status in decisions:
            asked = None if report_id is None else ledger.asked_proposal(conn, report_id=report_id, number=number)
            outcome = "missing" if asked is None else ledger.decide_instance(
                conn, instance_id=asked["instance_id"], status=status, decided_at=now, note="메일 답장",
                source=source or None)
            outcomes.append({"number": number, "status": status, "outcome": outcome,
                             "label": None if asked is None else asked["label"]})
    return outcomes


def _line(item: dict[str, Any]) -> str:
    name = f"{item['number']}번" + (f"({item['label']})" if item["label"] else "")
    if item["outcome"] in ("recorded", "unchanged"):
        return f"{name}: {LABELS[item['status']]}."
    if item["outcome"] == "superseded":
        return f"{name}: 그 뒤 수치가 바뀌어 새로 안내드린 제안이라 기록하지 않았습니다. 최근 보고서의 번호로 답장해 주십시오."
    return f"{name}: 이 보고서에서 그 번호의 제안을 찾지 못해 기록하지 않았습니다."


def render(reply: str, outcomes: list[dict[str, Any]]) -> tuple[str, str]:
    """(html, text) of the confirmation: what was recorded for each number, what it changes and how to undo it."""
    lines = [_line(item) for item in outcomes]
    kept = any(item["outcome"] in ("recorded", "unchanged") for item in outcomes)
    note = ("진행 중인 제안은 종합·생활 보고의 '지난 제안 점검'에서 살피고, 보류·완료한 제안은 수치가 크게 바뀌거나 기한이 가까워질 "
            "때만 다시 말씀드립니다. 바꾸시려면 같은 보고서에 같은 방식으로 다시 답장해 주십시오." if kept else
            "제안에 대한 결정은 보고서의 카드 번호로 「1번 할게」, 「1번 보류」, 「1번 했어」처럼 보내 주시면 기록합니다.")
    text = "\n".join(lines) + "\n\n" + note
    body = "".join(f'<div style="font-size:15px;line-height:1.65;color:#0a0b0d;margin:6px 0">· {escape(line)}</div>'
                   for line in lines)
    html = (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"></head><body style="margin:0;padding:16px;'
            f'background:#ffffff;font-family:Inter,Pretendard,\'Malgun Gothic\',sans-serif;word-break:keep-all">'
            f'<div style="max-width:640px;margin:0 auto">'
            f'<div style="font-size:12px;font-weight:600;color:#ffffff;background:#0052ff;display:inline-block;'
            f'border-radius:100px;padding:4px 12px;margin-bottom:12px">Smith 결정 기록</div>'
            f'<div style="border-left:3px solid #dee1e6;padding:4px 12px;margin-bottom:16px;font-size:13px;color:#3c4043">'
            f'{escape(reply[:MAX_CHARS])}</div>{body}'
            f'<div style="font-size:13px;line-height:1.6;color:#3c4043;margin-top:16px;padding-top:12px;'
            f'border-top:1px solid #dee1e6">{escape(note)} Smith는 기록만 하며 매매·이체는 실행하지 않습니다.</div>'
            f'</div></body></html>')
    return html, text


def answer(db: Path, report_id: str | None, reply: str, *, now: datetime,
           source: str = "") -> tuple[str, str, dict[str, Any]] | None:
    """Handle a reply that is only decisions: (html, text, result) for the confirmation, or None when the reply is a
    question for the answering model. The result has the shape the answer loop reads (no memos, nothing to remember).
    `source`: the reply's message id (see `apply`)."""
    decisions = read(reply)
    if decisions is None:
        return None
    outcomes = apply(db, report_id, decisions, now=now, source=source)
    html, text = render(reply, outcomes)
    return html, text, {"memos": [], "remember": [], "decisions": outcomes}
