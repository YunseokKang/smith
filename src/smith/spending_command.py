"""`smith spending`: card statements into month-by-month spending (FR-60, docs/spending-design.md).

- `smith spending sync [--back N]`: read the newest statement mail (and up to N earlier statements) into data/spending/.
- `smith spending show [--months N] [--json]`: the stored months by category.
- `smith spending classify`: look up merchants no rule knows in Google Places (name only) and keep what it found.
- `smith spending login | status | logout`: the birth date that opens the statement mail, in the OS credential store.
"""
import argparse
import getpass
import json
import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from smith import credentials, gmail, samsungcard, spending

logger = logging.getLogger(__name__)

_MAIL_WINDOW_DAYS = 45   # a statement arrives once a month; the newest within this window is the one to read


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("spending", help="Card statements into month-by-month spending (FR-60)")
    command.add_argument("action", choices=("sync", "show", "classify", "login", "status", "logout"))
    command.add_argument("--back", type=int, default=0, help="sync: also read up to N earlier statements not stored yet")
    command.add_argument("--months", type=int, default=3, help="show: months to show (default 3)")
    command.add_argument("--json", action="store_true", help="show: machine-readable output")
    command.add_argument("--db", type=Path, default=default_db)


def folder(db: Path) -> Path:
    return db.parent / "spending"


def run(args: argparse.Namespace) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.action == "login":
        birth = getpass.getpass("Birth date that opens the statement mail (YYMMDD): ").strip()
        if not re.fullmatch(r"\d{6}", birth):
            print("Not saved: six digits expected.")
            return 2
        credentials.save_card_birth(birth)
        print("Saved to the OS credential store.")
        return 0
    if args.action == "logout":
        credentials.delete_card_birth()
        print("Removed.")
        return 0
    if args.action == "status":
        stored = sorted(spending.stored_days(folder(args.db)))
        print(f"statement password: {'stored' if credentials.load_card_birth() else 'missing (smith spending login)'}")
        print(f"chrome: {samsungcard.find_chrome() or 'not found'}")
        print(f"statements stored: {len(stored)}" + (f" ({stored[0]} ~ {stored[-1]})" if stored else ""))
        return 0
    if args.action == "classify":
        for line in classify(folder(args.db)) or ["Nothing to look up."]:
            print(line)
        return 0
    if args.action == "sync":
        outcome = sync(folder(args.db), back=max(0, min(args.back, 36)))
        print(outcome["message"])
        return 0 if outcome["ok"] else 1
    return _show(folder(args.db), max(1, args.months), args.json)


_CHECK_FROM_DAY = 8   # the statement is written around the 10th~12th and mailed by the 20th


def sync_due(target: Path, today: date) -> bool:
    """Whether the scheduled run should look for a statement mail now: once a day, from the 8th of a month until the
    statement paid in that month is stored. Without any statement stored it is always due (first run)."""
    stored = spending.stored_days(target)
    if stored and (today.day < _CHECK_FROM_DAY or max(stored)[:6] >= f"{today:%Y%m}"):
        return False
    return _state(target).get("checked") != today.isoformat()


def _state(target: Path) -> dict[str, Any]:
    try:
        state = json.loads((target / "sync.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _mark(target: Path, key: str, today: date) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "sync.json").write_text(json.dumps({**_state(target), key: today.isoformat()}), encoding="utf-8")


def sync(target: Path, *, back: int = 0, now: float | None = None, today: date | None = None) -> dict[str, Any]:
    """Read the newest statement mail into `target`. Never raises: {"ok", "message", "stored": [payment days]}.

    Nothing is fetched when the newest mail's statement is already stored and no earlier one is asked for. `today`
    marks the day's scheduled check as done (`sync_due`), whatever it found.
    """
    if today is not None:
        _mark(target, "checked", today)
    birth, chrome, grant = credentials.load_card_birth(), samsungcard.find_chrome(), credentials.load_gmail()
    if not birth or chrome is None or grant is None:
        return _failed("setup-missing (smith spending status)")
    try:
        token, scopes = gmail.access_token(grant)
        if gmail.READ_SCOPE not in scopes:
            return _failed("no-read-scope (smith mail login --read)")
        after = int((now or time.time()) - _MAIL_WINDOW_DAYS * 86400)
        found = None
        for message in gmail.statements_from(token, samsungcard.SENDER, after=after):
            found = gmail.attachment(token, message, samsungcard.ATTACHMENT)
            if found:
                break
    except gmail.MailError as error:
        return _failed(f"mail-{error.code}")
    if not found:
        return _failed("no-statement-mail")
    settled = samsungcard.ATTACHMENT.fullmatch(found[0]).group(1)
    known = spending.stored_days(target)
    if settled in known and not back:
        return {"ok": True, "message": f"up to date ({settled})", "stored": []}
    try:
        form = samsungcard.login_form(found[1].decode("utf-8", "replace"), birth)
        session = samsungcard.Session(chrome)
        try:
            readings = samsungcard.read(session, form, settled, earlier=back, known=known)
        finally:
            session.close()
    except samsungcard.StatementError as error:
        return _failed(error.code)
    stored, refused = [], []
    for raw in readings:
        try:
            spending.parse(raw)   # a reading whose rows do not add up to the page's total is not kept
        except spending.SpendingError as error:
            refused.append(f"{raw.get('settled')}:{error.code}")
            continue
        spending.save_raw(target, raw)
        stored.append(str(raw["settled"]))
    logger.info("spending sync stored=%d refused=%d", len(stored), len(refused))
    message = f"stored {len(stored)} statement(s): {', '.join(stored) or '-'}" + (f"; refused {', '.join(refused)}" if refused else "")
    return {"ok": not refused, "message": message, "stored": stored}


def _failed(code: str) -> dict[str, Any]:
    logger.warning("spending sync failed: %s", code)
    return {"ok": False, "message": f"spending sync failed: {code}", "stored": []}


def _show(target: Path, months: int, as_json: bool) -> int:
    picture = spending.summary(spending.load(target, last=months), spending.load_own(target))
    if picture is None:
        print("No statements stored (smith spending sync).")
        return 1
    if as_json:
        print(json.dumps(picture, ensure_ascii=False, indent=1))
        return 0
    for month in picture["months"]:
        print(f"\n[{month['label']}] 쓴 돈 {month['spent']:,}원 (청구 {month['billed']:,}원, 받은 혜택 {month['benefit_total']:,}원)")
        for key, group in month["categories"].items():
            names = ", ".join(m["name"] for m in group["merchants"][:3])
            print(f"  {spending.CATEGORIES[key]:<12} {group['amount']:>10,}원 {group['count']:>3}건  {names}")
        if month["unsorted"]:
            print(f"  미분류 가맹점: {', '.join(month['unsorted'])}")
    return 0


_LOOKUPS_A_RUN = 40   # merchants looked up in one run; each is one Google call inside the dining allowance


def classify_scheduled(target: Path, today: date, *, lookup: Any = None, key: str | None = None) -> str | None:
    """The scheduled run's turn, once a day: merchants a failed call or the run's limit left unsorted are taken up
    again without a command. Returns a line for the run log, or None when there was nothing to ask. The line carries
    counts and a reason only: where the client shops belongs in data/spending, never in a log."""
    if _state(target).get("classified") == today.isoformat():
        return None
    _mark(target, "classified", today)
    lines = classify(target, lookup=lookup, key=key)
    if not lines:
        return None
    stopped = [line for line in lines if line.startswith("classify ")]
    asked = len(lines) - len(stopped)
    found = sum(not line.endswith("찾지 못함") for line in lines if not line.startswith("classify "))
    reason = f"; {stopped[0].split('(')[0].strip()}" if stopped else ""
    return f"{asked} merchant(s) looked up, {found} sorted{reason}"


def classify(target: Path, *, now: datetime | None = None, lookup: Any = None, key: str | None = None) -> list[str]:
    """Look up the stored merchants no table or rule knows, once each, and keep the answer. Never raises.

    The request carries the merchant's name only. A name already looked up (found or not) is not asked again; the
    household's own table always wins over what a search found. The returned lines name merchants: they are for the
    client's own console (`smith spending classify`), not for a log.
    """
    from smith import dining_log, google_places

    own, asked = spending.load_own(target), spending.looked_up(target)
    unknown = sorted({e.merchant for s in spending.load(target) for e in s.entries
                      if e.spent > 0 and e.merchant not in asked and spending.categorize(e, own) == "other"})
    key = key or credentials.load_api_key(google_places.PROVIDER)
    if not unknown or not key:
        return [] if not unknown else [f"classify skipped: no Google Places key ({len(unknown)} merchants unknown)"]
    today, where, lines = (now or datetime.now()), target.parent / "dining", []
    for name in unknown[:_LOOKUPS_A_RUN]:
        try:
            if not dining_log.reserve_google(where, f"{today:%Y-%m}", google_places.MONTHLY_CAP):
                lines.append("classify stopped: this month's Google allowance is used up")
                break
            found = (lookup or google_places.kind)(name, key=key)
        except (google_places.GoogleError, dining_log.DiningLogError, OSError) as error:
            lines.append(f"classify stopped: {getattr(error, 'code', type(error).__name__)}")
            break
        category = spending.category_of_place(found[1]) if found else None
        detail = {"matched": found[0], "types": found[1][:4], "address": found[2]} if found else {}
        spending.save_lookup(target, name, {"category": category, "source": "google-places", "asked": f"{today:%Y-%m-%d}", **detail})
        lines.append(f"{name} → {spending.CATEGORIES[category] if category else '찾지 못함'}"
                     + (f" ({found[0]}, {found[1][0] if found[1] else '-'})" if found else ""))
    return lines
