"""The monthly memory check (FR-52): once a month Smith shows the client part of what it remembers (FR-19) and asks for
corrections by reply (`memory_qa`). The lesson from the Pickle review (2026-11-15): an agent's memory must be visible
and correctable, and what the client said must not blur into what was inferred. Smith's memory holds only the client's
own words, so every line shows when and through which channel the client said it.

The mail is code alone, with no model: up to `[memory_review] size` facts (default 12), the never-shown ones first
(newest first, so the month's new facts lead), then the ones shown longest ago, so the whole memory cycles in a few
months. Which fact was shown when is read back from the sent snapshots (kept a year, `ledger.SNAPSHOT_KEEP_DAYS`), so no
state file is needed. The counsel topic stays out (`memory.PRIVATE`: only the counsel thread reads it).

Sending follows the health letter's run discipline: one run per month in the shared run table (`product = "memory"`,
key "memory:YYYY-MM"), claimed atomically, `sending` committed before the Gmail request, never resent after an
uncertain outcome, and a clean failure retried a bounded number of times. The month's slot is its first `weekday` at
`time` (default Sunday 10:00); a missed slot still goes out later that month. The snapshot numbers every fact (the shown
ones first, then the rest), so a reply's "3번" names exactly the line the client saw, whatever changed since.
"""
import hashlib
import uuid
from contextlib import closing
from datetime import date, datetime, time as clock, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from smith import gmail, ledger, memory
from smith.config import WEEKDAYS, local_time
from smith.daily_html import ACCENT, BODY, INK, MUTED, _card, _page, _pill

PRODUCT = "memory"
PREFIX = "[Smith][기억 점검]"
DEFAULT_SIZE = 12
LEASE = timedelta(minutes=30)
MAX_ATTEMPTS = 3
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
HOW_TO = ('틀렸거나 바뀐 것이 있으면 이 메일에 번호와 함께 답장해 주십시오. 예: "3번 지워 줘", "5번은 이제 전세로 바뀌었어", '
          '"새로 기억해 줘: …". 답장이 없으면 아무것도 바꾸지 않습니다.')
FOOTER = ("여기 보이지 않는 기억도 내용을 적어 답장하시면 찾아서 고칩니다. 지우거나 고친 줄은 기억 폴더의 archive에 원래 모습 그대로 "
          "남습니다. 상담 메일에서 들은 것은 상담 스레드에서만 다루므로 이 목록에 넣지 않았습니다.")
Sender = Any


def settings(config: dict[str, Any]) -> dict[str, Any]:
    table = config.get("memory_review") or {}
    return {"enabled": bool(table.get("enabled", False)), "weekday": str(table.get("weekday", "sunday")),
            "time": str(table.get("time", "10:00")), "size": int(table.get("size", DEFAULT_SIZE))}


def run_key(day: date) -> str:
    return f"{PRODUCT}:{day:%Y-%m}"


def review_day(config: dict[str, Any], today: date) -> date:
    """The month's slot day: the first configured weekday of `today`'s month."""
    name = settings(config)["weekday"]
    weekday = WEEKDAY_NAMES.index(name) if name in WEEKDAYS else 6
    first = today.replace(day=1)
    return first + timedelta(days=(weekday - first.weekday()) % 7)


def slot_for(config: dict[str, Any], day: date) -> datetime:
    hour, minute = (int(p) for p in settings(config)["time"].split(":"))
    return datetime.combine(day, clock(hour, minute), ZoneInfo(config["app"]["timezone"]))


def due(conn: Any, config: dict[str, Any], now: datetime) -> date | None:
    """This month's slot day when it has passed and the month has no final run (a manual send counts)."""
    today = local_time(now, config["app"]["timezone"]).date()
    day = review_day(config, today)
    if now < slot_for(config, day):
        return None
    run = ledger.daily_run(conn, run_key(day))
    if run is None:
        return day
    if run["status"] in ledger.DAILY_FINAL or (run["status"] == "failed" and run["attempts"] >= MAX_ATTEMPTS):
        return None
    return day


def shown_history(conn: Any, *, now: datetime) -> tuple[dict[str, date], date | None]:
    """(fact key -> the last day it was shown, the last review day) from the memory checks sent in the kept year."""
    shown: dict[str, date] = {}
    last: date | None = None
    for run in ledger.sent_daily_runs(conn, since=now - timedelta(days=ledger.SNAPSHOT_KEEP_DAYS), product=PRODUCT):
        stored = ledger.daily_snapshot(conn, run["run_key"]) or {}
        # The day it actually went out (a manual send late in the month), else the month's slot day.
        day = date.fromisoformat(stored["sent_on"]) if isinstance(stored.get("sent_on"), str) else run["day"]
        last = day if last is None or day > last else last
        for item in stored.get("facts") or []:
            if item.get("shown"):
                key = memory.fact_key(_fact(item))
                shown[key] = max(shown.get(key, day), day)
    return shown, last


def select(facts: list[memory.Fact], shown: dict[str, date], size: int) -> list[memory.Fact]:
    """The facts this month shows: never shown (newest first), then shown longest ago; at most `size`."""
    newest = lambda f: f.recorded_on or date.min   # noqa: E731
    never = sorted((f for f in facts if memory.fact_key(f) not in shown), key=newest, reverse=True)
    seen = sorted((f for f in facts if memory.fact_key(f) in shown), key=lambda f: (shown[memory.fact_key(f)], newest(f)))
    return (never + seen)[:max(size, 0)]


def assemble(facts: list[memory.Fact], *, day: date, shown: dict[str, date], last_review: date | None,
             size: int) -> dict[str, Any]:
    """The month's mail as data: every fact numbered, the shown ones first (new ones, then the rest, by topic)."""
    chosen = select(facts, shown, size)
    keys = {memory.fact_key(f) for f in chosen}
    order = list(memory.TOPICS)
    by_topic = lambda f: (order.index(f.topic) if f.topic in order else len(order), f.recorded_on or date.min)  # noqa: E731
    new = [f for f in chosen if last_review and f.recorded_on and f.recorded_on > last_review]
    again = [f for f in chosen if f not in new]
    rest = [f for f in facts if memory.fact_key(f) not in keys]
    items = []
    for group, flag in ((sorted(new, key=by_topic), "new"), (sorted(again, key=by_topic), "again"), (sorted(rest, key=by_topic), "")):
        for fact in group:
            items.append({"n": len(items) + 1, "topic": fact.topic, "file": fact.file, "text": fact.text, "source": fact.source,
                          "recorded_on": None if fact.recorded_on is None else fact.recorded_on.isoformat(),
                          "shown": bool(flag), "new": flag == "new"})
    return {"day": day, "last_review": last_review, "facts": items, "total": len(items),
            "shown_count": sum(1 for i in items if i["shown"])}


def render(data: dict[str, Any]) -> tuple[str, str, str]:
    """(subject, html, text) of the month's check."""
    day, shown = data["day"], [i for i in data["facts"] if i["shown"]]
    subject = f"{PREFIX} {day.year}년 {day.month}월 · {len(shown)}개 확인"
    rest = data["total"] - len(shown)
    lead = (f"Smith가 고객님께 들은 내용 가운데 {len(shown)}개입니다(전체 {data['total']}개). "
            + (f"나머지 {rest}개는 다음 달부터 차례로 보여 드립니다." if rest else ""))
    groups = ([("지난 점검 뒤 새로 기억한 것", [i for i in shown if i["new"]]), ("그 밖에 확인할 것", [i for i in shown if not i["new"]])]
              if data["last_review"] else [("기억하고 있는 것", shown)])
    cards = "".join(_card(title, "", "".join(_row(i) for i in items)) for title, items in groups if items)
    hero = _card("기억 점검", escape(lead) + f'<div style="margin-top:8px;color:{BODY}">{escape(HOW_TO)}</div>', "", accent=True)
    footer = (f'<tr><td style="padding:4px 6px 12px"><div style="font-size:12px;line-height:1.6;color:{MUTED}">'
              f'{escape(FOOTER)}</div></td></tr>')
    html = _page(hero + cards + footer, "Smith 기억 점검")
    lines = [subject, "", lead, HOW_TO, ""]
    for title, items in groups:
        if items:
            lines += [f"[{title}]"] + [f"{i['n']}. {i['text']} ({memory.TOPICS.get(i['topic'], '기타')}, {_when(i)})"
                                       for i in items] + [""]
    return subject, html, "\n".join(lines + [FOOTER])


def _row(item: dict[str, Any]) -> str:
    return (f'<div style="padding:10px 0;border-top:1px solid #efe5d8">'
            f'<div style="font-size:15px;line-height:1.6;color:{INK}"><b style="color:{ACCENT}">{item["n"]}.</b> {escape(item["text"])}</div>'
            f'<div style="margin-top:4px">{_pill(memory.TOPICS.get(item["topic"], "기타"))} '
            f'<span style="font-size:12px;color:{MUTED}">{escape(_when(item))}</span></div></div>')


def _when(item: dict[str, Any]) -> str:
    return f"{item['recorded_on'] or '날짜 없음'} · {item['source']}"


def _fact(item: dict[str, Any]) -> memory.Fact:
    recorded = item.get("recorded_on")
    return memory.Fact(str(item.get("topic", "other")), str(item.get("text", "")),
                       date.fromisoformat(recorded) if recorded else None, str(item.get("source", "")), str(item.get("file", "")))


def snapshot(data: dict[str, Any], subject: str) -> dict[str, Any]:
    """What the mail showed and every fact's number: a reply is resolved against this, never against the files as they
    are later."""
    return {"version": 1, "product": PRODUCT, "month": f"{data['day']:%Y-%m}", "day": data["day"].isoformat(),
            "sent_on": data["sent_on"].isoformat(), "subject": subject, "facts": data["facts"], "total": data["total"]}


def run_due(db: Path, config: dict[str, Any], *, now: datetime, recipient: str | None,
            credentials: tuple[str, str, str] | None, sender: Sender = gmail.send) -> list[str]:
    """Called by `smith report run-due`: sends this month's check when it is due. Returns log lines. Interrupted runs of
    any product are closed first, as the health letter does, so they are when only this mail is enabled."""
    lines: list[str] = []
    with closing(ledger.connect(db)) as conn:
        for run in ledger.recover_daily_runs(conn, now=now, lease=LEASE):
            lines.append(f"{run['run_key']} interrupted while {run['was']}: closed")
        day = due(conn, config, now)
    if day is None:
        return lines
    result = publish(db, config, now=now, day=day, trigger="scheduled", recipient=recipient, credentials=credentials, sender=sender)
    return lines + [as_line(result)]


def as_line(result: dict[str, Any]) -> str:
    return f"memory {result['run_key']}: {result['status']}" + (f" ({result['error_code']})" if result.get("error_code") else "")


def build(db: Path, config: dict[str, Any], *, now: datetime, day: date) -> dict[str, Any]:
    """The month's data from the memory folder as it is now and the checks sent before."""
    facts = memory.load(memory.directory(db))   # The counsel topic stays out (memory.PRIVATE).
    with closing(ledger.connect_read_only(db)) as conn:
        shown, last = shown_history(conn, now=now)
    data = assemble(facts, day=day, shown=shown, last_review=last, size=settings(config)["size"])
    return {**data, "sent_on": local_time(now, config["app"]["timezone"]).date()}


def publish(db: Path, config: dict[str, Any], *, now: datetime, day: date, trigger: str, recipient: str | None,
            credentials: tuple[str, str, str] | None, sender: Sender = gmail.send, force: bool = False) -> dict[str, Any]:
    """Claim, build, send and record one month's check. Returns {"status", "run_key", "subject", "error_code"}."""
    key = run_key(day) if not force else f"{run_key(day)}#manual-{uuid.uuid4().hex[:6]}"
    with closing(ledger.connect(db)) as conn:
        token, outcome = ledger.claim_daily_run(conn, run_key=key, day=day, run_id=uuid.uuid4().hex, trigger=trigger, now=now,
                                                lease=LEASE, max_attempts=MAX_ATTEMPTS, product=PRODUCT)
    if token is None:
        return {"status": "skipped", "run_key": key, "subject": None, "error_code": outcome}
    if recipient is None or credentials is None:
        return _finish(db, key, token, "building", "failed", error_code="no-mail-setup")
    try:
        data = build(db, config, now=now, day=day)
        if not data["shown_count"]:
            return _finish(db, key, token, "building", "skipped", error_code="no-memory")
        subject, html, text = render(data)
    except Exception as error:  # noqa: BLE001 - any build failure releases the claim as a clean, retryable failure.
        return _finish(db, key, token, "building", "failed", error_code=f"build-{type(error).__name__}")
    with closing(ledger.connect(db)) as conn:
        # Committed before the request: a run that dies after Gmail accepted the mail never sends it again.
        held = ledger.advance_daily_run(conn, run_key=key, token=token, from_status="building", status="sending",
                                        subject=subject, html_sha256=hashlib.sha256(html.encode("utf-8")).hexdigest())
        if held:
            ledger.record_daily_snapshot(conn, run_key=key, day=day, created_at=now, snapshot=snapshot(data, subject))
    if not held:
        return {"status": "skipped", "run_key": key, "subject": subject, "error_code": "claim-lost"}
    try:
        message_id = sender(credentials, recipient=recipient, subject=subject, html=html, text=text)
    except gmail.MailError as error:
        return _finish(db, key, token, "sending", "unknown" if error.uncertain else "failed", error_code=error.code, subject=subject)
    return _finish(db, key, token, "sending", "sent", message_id=message_id, subject=subject)


def _finish(db: Path, key: str, token: str, from_status: str, status: str, *, error_code: str | None = None,
            subject: str | None = None, message_id: str | None = None) -> dict[str, Any]:
    with closing(ledger.connect(db)) as conn:
        ledger.advance_daily_run(conn, run_key=key, token=token, from_status=from_status, status=status,
                                 error_code=error_code, subject=subject, message_id=message_id,
                                 finished_at=datetime.now(timezone.utc))
    return {"status": status, "run_key": key, "subject": subject, "error_code": error_code}


def describe(conn: Any, limit: int = 10) -> list[str]:
    """Recent runs for `smith memory review-status`."""
    lines = [f"{r['run_key']:<24} {r['status']:<8} attempts {r['attempts']} {r['error_code'] or ''} {r['subject'] or ''}".rstrip()
             for r in ledger.daily_runs(conn, limit=limit, product=PRODUCT)]
    return lines or ["(no memory checks yet)"]
