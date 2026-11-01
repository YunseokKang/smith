"""Propose, approve and execute trades (trading design §5, §7). The only caller of the order client.

A plan reaches the broker only after: the client asked for it (CLI), code built and checked it, the client replied to
the confirmation mail with a valid authenticator code, and code checked the leg again right before the order. Each leg
carries a fixed clientOrderId (Toss keeps it as an idempotency key for 10 minutes): an order whose answer was lost is
resent with the same key inside that window and matched against the order list after it, never sent blind.
"""
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from smith import gmail, totp, trade_mail, trade_plan, trade_store
from smith.config import local_time
from smith.toss import ForbiddenRequest, TossError
from smith.toss_orders import OrderClient

logger = logging.getLogger(__name__)
IDEMPOTENCY_WINDOW = timedelta(minutes=9)    # Toss keeps a clientOrderId for 10 minutes; one minute of margin.
POLLS, POLL_SECONDS = 6, 10                   # Wait about a minute for a fill inside one run; later runs keep checking.
_OPEN = {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}
_ENDED = {"CANCELED", "REJECTED", "REPLACED"}

Mailer = Callable[[str, str, str], str]       # (subject, text, html) -> Gmail message id; raises gmail.MailError


@dataclass
class Deps:
    """What a trade run talks to. Tests pass fakes; `smith trade` and the scheduled run pass the real ones."""

    client: OrderClient
    account_seq: int
    account: str                                     # the opaque key of `account_seq`; every plan is pinned to one
    mail: Mailer
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    settings: dict[str, Any]
    tz: str
    replies: Callable[[str, datetime, set[str]], list[trade_mail.Reply]] | None = None  # (thread, after, seen)
    # Ids of the client's sent messages that contain a code between two epoch seconds (gmail.sent_containing).
    sent_with: Callable[[str, int, int], list[str]] | None = None
    thread_of: Callable[[str], str] | None = None    # Gmail thread of the confirmation message
    totp_secret: str | None = None


def propose(conn: Any, deps: Deps, request: trade_plan.Request, *, account: str) -> dict[str, Any]:
    """Build, store and mail a plan. Returns {"status", "code"?, "reason"?}. Nothing is ordered here."""
    now = deps.clock()
    if not deps.settings.get("enabled") or trade_store.halted(conn):
        return {"status": "rejected", "reason": "거래 기능이 꺼져 있거나 중단 상태입니다."}
    day = local_time(now, deps.tz).replace(hour=0, minute=0, second=0, microsecond=0)
    spent = trade_store.spent_krw(conn, since=day.astimezone(timezone.utc), until=now + timedelta(days=1))
    try:
        plan = trade_plan.build(deps.client, account_seq=deps.account_seq, account=account, request=request,
                                settings=deps.settings, now=now, spent_today_krw=spent)
    except trade_plan.PlanRejected as rejected:
        return {"status": "rejected", "reason": rejected.reason}
    except TossError as error:
        return {"status": "rejected", "reason": f"토스증권 조회 실패({error.code})"}
    waiting = trade_store.add_plan(conn, plan, sha256=_digest(plan), now=now)
    if waiting:
        return {"status": "rejected", "reason": f"승인을 기다리는 계획({waiting})이 있습니다. 그 메일에 \"취소\"로 답하시거나 "
                                                  "만료된 뒤 다시 요청해 주십시오."}
    text, html = trade_mail.confirmation(plan, deps.tz)
    try:
        message_id = deps.mail(trade_mail.subject(plan), text, html)
    except gmail.MailError as error:
        status = "confirm_unknown" if error.uncertain else "rejected"
        trade_store.move(conn, plan["plan_id"], expected=("proposed",), status=status, now=deps.clock(),
                         note=f"confirmation mail {error.code}")
        return {"status": status, "code": plan["code"], "reason": "확인 메일을 보내지 못했습니다."}
    trade_store.move(conn, plan["plan_id"], expected=("proposed",), status="confirm_sent", now=deps.clock(),
                     confirm_message_id=message_id, confirm_sent_at=deps.clock().isoformat())
    return {"status": "confirm_sent", "code": plan["code"], "plan": plan}


def run(conn: Any, deps: Deps) -> list[str]:
    """One pass: read approvals, execute approved plans, follow orders in flight. Returns log lines.

    The caller holds `trade_store.exclusive` for the whole pass (one pass at a time across processes)."""
    lines: list[str] = []
    for found in trade_store.plans(conn, statuses=trade_store.OPEN_PLAN, limit=50, oldest_first=True):
        try:
            lines += _advance(conn, deps, found)
        except (TossError, gmail.MailError) as error:
            # A failed read leaves the plan where it was; the next run tries again. Orders keep their state.
            lines.append(f"Trade {found['code']}: {type(error).__name__} {getattr(error, 'code', '')}")
    return lines


def _advance(conn: Any, deps: Deps, found: dict[str, Any]) -> list[str]:
    if found["plan"]["account"] != deps.account:
        # The configured account changed after the plan: nothing is sent or resent there; a plan that never ordered
        # stops, one with orders in flight waits for the client (they are on the plan's account, not this one).
        if any(leg["first_submit_at"] for leg in found["legs"]):
            return [f"Trade {found['code']}: its account is no longer the configured one; check the orders in the app."]
        if found["status"] == "confirm_sent":
            trade_store.move(conn, found["plan_id"], expected=("confirm_sent",), status="halted", now=deps.clock(),
                             note="account changed")
            return [f"Trade {found['code']}: halted, the configured account changed."]
        return _halt(conn, deps, found, "계획을 만든 뒤 거래 계좌 설정이 바뀌었습니다. 다시 요청해 주십시오.")
    if found["status"] == "confirm_sent":
        return _check_approval(conn, deps, found)
    if found["status"] in ("approved", "executing", "unknown"):
        return _execute(conn, deps, found)
    return []


def _check_approval(conn: Any, deps: Deps, found: dict[str, Any]) -> list[str]:
    now, code = deps.clock(), found["code"]
    expires = datetime.fromisoformat(found["expires_at"])
    if deps.replies is None or deps.sent_with is None or deps.totp_secret is None:
        return [f"Trade {code}: approval cannot be read (no Gmail read grant or no authenticator enrolled)."]
    if not found["thread_id"] and deps.thread_of is not None:
        found["thread_id"] = deps.thread_of(found["confirm_message_id"])
        conn.execute("UPDATE trade_plans SET thread_id = ? WHERE plan_id = ?", (found["thread_id"], found["plan_id"]))
    sent_at = datetime.fromisoformat(found["confirm_sent_at"])
    for reply in deps.replies(found["thread_id"], sent_at, trade_store.seen_replies(conn)):
        step = _code_step(deps.totp_secret, reply, sent_at) if reply.decision == "approve" else None
        holders = _holders(deps, reply, step) if step is not None and reply.sent_at < expires else None
        if holders is not None and reply.message_id not in holders:
            # Gmail search has not indexed this reply yet: look again at the next run (the reply stays unseen).
            return [f"Trade {code}: approval reply not searchable yet."]
        trade_store.event(conn, now, found["plan_id"], "reply", reply.message_id)
        if reply.sent_at >= expires:
            continue
        if reply.decision == "decline":
            trade_store.move(conn, found["plan_id"], expected=("confirm_sent",), status="declined", now=now,
                             note="client declined")
            return [f"Trade {code}: declined by the client."]
        if holders is not None and holders != [reply.message_id]:
            trade_store.event(conn, now, found["plan_id"], "code-elsewhere", reply.message_id)
            step = None
        if step is not None and trade_store.approve(conn, found["plan_id"], step=step, message_id=reply.message_id,
                                                    sent_at=reply.sent_at, now=deps.clock()):
            found["status"] = "approved"
            return [f"Trade {code}: approved."] + _execute(conn, deps, found)
        if step is not None and (trade_store.plan(conn, found["plan_id"]) or {}).get("status") != "confirm_sent":
            return [f"Trade {code}: no longer awaiting approval."]
        trade_store.event(conn, now, found["plan_id"], "approval-rejected", reply.message_id)
        _mail(deps, conn, found, f"[Smith][거래 확인] 승인 코드가 맞지 않습니다 · {code}",
              ["답장의 인증 앱 코드가 맞지 않거나 이미 쓴 코드여서 승인하지 않았습니다.",
               "다른 메일에 같은 코드가 있어도 승인하지 않습니다(복사 방지).",
               "확인 메일에 다시 \"승인 123456\"처럼 지금 인증 앱에 보이는 6자리로 답장해 주십시오."])
        return [f"Trade {code}: approval code rejected."]
    if now >= expires:
        trade_store.move(conn, found["plan_id"], expected=("confirm_sent",), status="expired", now=now)
        return [f"Trade {code}: expired without approval."]
    return []


def _holders(deps: Deps, reply: trade_mail.Reply, step: int) -> list[str]:
    """Ids of the client's sent messages around the code's time that contain it; approval needs exactly this reply.

    An authenticator code is not bound to a plan, so a code the client sent anywhere else (another plan's thread, an
    old confirmation, any mail) could be copied here by something with access to the mailbox. A copy is always later
    than its original, so when this reply is found the original is found with it, and the approval is refused.
    """
    window = totp.STEP_SECONDS
    return deps.sent_with(reply.code or "", (step - 4) * window, (step + 5) * window)


def _code_step(secret: str, reply: trade_mail.Reply, confirm_sent_at: datetime) -> int | None:
    """The time step of a code that matches the reply's send time and was generated after the confirmation went out
    (whether it was used before is decided in the approving transaction)."""
    step = totp.matching_step(secret, reply.code or "", reply.sent_at)
    if step is None or (step + 1) * totp.STEP_SECONDS <= confirm_sent_at.timestamp():
        return None
    return step


def _execute(conn: Any, deps: Deps, found: dict[str, Any]) -> list[str]:
    """Walk the legs in order; stop at the first one still open, unknown or refused."""
    code = found["code"]
    trade_store.move(conn, found["plan_id"], expected=("approved",), status="executing", now=deps.clock())
    previous: dict[str, Any] | None = None
    for leg in trade_store.legs(conn, found["plan_id"]):
        if leg["status"] in ("submitting", "unknown"):
            leg = _reconcile(conn, deps, found, leg)
        if leg["status"] == "submitted":
            if trade_store.halted(conn):
                _cancel(conn, deps, found, leg)    # halted after this order went out: stop it, then record the outcome
            leg = _follow(conn, deps, found, leg)
        if leg["status"] in ("submitting", "unknown"):
            if found["status"] == "unknown":
                return [f"Trade {code}: leg {leg['leg']} still unknown."]
            trade_store.move(conn, found["plan_id"], expected=("executing",), status="unknown", now=deps.clock(),
                             note=f"leg {leg['leg']} outcome unknown")
            return _finish(conn, deps, found, "unknown", "주문이 접수되었는지 확인하지 못했습니다.")
        if found["status"] == "unknown":
            trade_store.move(conn, found["plan_id"], expected=("unknown",), status="executing", now=deps.clock(),
                             note=f"leg {leg['leg']} settled")
            found = found | {"status": "executing"}
        if leg["status"] == "submitted":
            return [f"Trade {code}: leg {leg['leg']} waiting for a fill."]
        if leg["status"] in ("rejected", "failed"):
            return _halt(conn, deps, found, f"{leg['leg']}번 주문을 증권사가 받지 않았습니다({leg['error_code']}).")
        if leg["status"] != "pending":           # filled, partial, canceled or skipped
            previous = leg
            continue
        if previous is not None and previous["status"] != "filled":
            if previous["status"] == "partial":   # the mail promised the next leg only after a full fill
                return _halt(conn, deps, found, f"앞 주문이 {previous['filled_quantity']}주만 체결되고 끝나 다음 주문은 "
                                                 "내지 않았습니다.")
            _skip_rest(conn, deps, found["plan_id"])
            return _finish(conn, deps, found, "done", "앞 주문이 체결되지 않아 다음 주문은 내지 않았습니다.")
        quantity = int(leg["quantity"]) if leg["quantity"] else int(Decimal(previous["filled_quantity"] or "0"))
        outcome = _submit(conn, deps, found, leg, quantity)
        if outcome is not None:
            return outcome
        return _execute(conn, deps, found | {"status": "executing"})   # Follow the new order, then the next leg.
    return _finish(conn, deps, found, "done", None)


def _submit(conn: Any, deps: Deps, found: dict[str, Any], leg: dict[str, Any], quantity: int) -> list[str] | None:
    """Check again, reserve the leg within the daily limit, then send one order. None when the order went out."""
    now = deps.clock()
    if not deps.settings.get("enabled") or trade_store.halted(conn):
        return _halt(conn, deps, found, "거래 기능이 꺼졌거나 중단 명령이 있었습니다.")
    expires = datetime.fromisoformat(found["expires_at"])
    # An order left Smith (sent, or possibly sent): from then on later legs are bound to its session, not to expiry.
    ordered = any(l["status"] not in ("pending", "skipped") for l in trade_store.legs(conn, found["plan_id"]))
    session_end = (trade_store.plan(conn, found["plan_id"]) or {}).get("session_end") if ordered else None
    if not ordered and now >= expires:
        trade_store.move(conn, found["plan_id"], expected=("executing",), status="expired", now=now)
        return _finish(conn, deps, found, "expired", "승인하신 계획의 시간 안에 주문할 수 있는 장이 열리지 않았습니다.")
    if ordered and (session_end is None or now >= datetime.fromisoformat(session_end)):
        return _halt(conn, deps, found, "다음 주문은 첫 주문과 같은 장에서만 냅니다. 그 장이 끝났습니다.")
    if now < datetime.fromisoformat(found["not_before"]):
        return [f"Trade {found['code']}: approved, waiting for the session."]
    check = trade_plan.precheck(deps.client, account_seq=deps.account_seq, plan=found["plan"], leg=_plan_leg(found, leg),
                                quantity=quantity, settings=deps.settings, now=now)
    if not check["ok"]:
        if check["wait"]:
            return [f"Trade {found['code']}: waiting ({check['reason']})."]
        return _halt(conn, deps, found, check["reason"])
    prior, _ = _listed(deps, leg["symbol"], now)
    now = deps.clock()                       # the checks took time: charge the day the order actually goes out
    if not ordered and now >= expires:
        trade_store.move(conn, found["plan_id"], expected=("executing",), status="expired", now=now)
        return _finish(conn, deps, found, "expired", "승인하신 계획의 시간 안에 주문할 수 없었습니다.")
    if now >= check["session_end"] - trade_plan.CLOSE_MARGIN:
        return [f"Trade {found['code']}: waiting (the session is closing)."]
    local = local_time(now, deps.tz).replace(hour=0, minute=0, second=0, microsecond=0)
    reserved = trade_store.reserve_leg(
        conn, found["plan_id"], leg["leg"], now=now, day=(local.astimezone(timezone.utc), local + timedelta(days=1)),
        max_daily_krw=int(deps.settings.get("max_daily_krw", trade_plan.DEFAULTS["max_daily_krw"])),
        session_end=check["session_end"], quantity=str(quantity), limit_price=check["price"],
        notional_krw=check["notional_krw"], prior_orders=json.dumps(sorted(str(o.get("orderId")) for o in prior)))
    if reserved == "halted":
        return _halt(conn, deps, found, "중단 명령이 있었습니다.")
    if reserved == "limit":
        return _halt(conn, deps, found, "오늘 주문 합계가 하루 한도를 넘게 됩니다.")
    if reserved == "taken":
        return [f"Trade {found['code']}: leg {leg['leg']} was taken by another run."]
    return _place(conn, deps, found, leg | {"quantity": str(quantity), "limit_price": check["price"],
                                            "first_submit_at": now.isoformat()}, resend=False)


def _place(conn: Any, deps: Deps, found: dict[str, Any], leg: dict[str, Any], *, resend: bool) -> list[str] | None:
    """Send the leg's order. A resend (same clientOrderId) that fails in any way stays unknown: only the order
    list, after the idempotency window, may decide that the first send never arrived."""
    if not trade_store.owns_leg(conn, found["plan_id"], leg["leg"], str(leg.get("first_submit_at"))):
        return [f"Trade {found['code']}: leg {leg['leg']} changed before its send; nothing sent."]
    body = {"clientOrderId": leg["client_order_id"], "symbol": leg["symbol"], "side": leg["side"], "orderType": "LIMIT",
            "timeInForce": "DAY", "quantity": leg["quantity"], "price": leg["limit_price"], "confirmHighValueOrder": False}
    try:
        result = deps.client.place(deps.account_seq, body)
    except ForbiddenRequest:
        trade_store.update_leg(conn, found["plan_id"], leg["leg"], now=deps.clock(), status="failed",
                               error_code="forbidden-shape")
        return _halt(conn, deps, found, "허용되지 않은 주문 형식이라 보내지 않았습니다.")
    except TossError as error:
        # No answer, a server error, or a success status whose body cannot be read: the order may exist.
        uncertain = (error.status == 0 or 200 <= error.status < 300 or error.status >= 500
                     or error.code == "request-in-progress")
        if resend or uncertain:
            trade_store.update_leg(conn, found["plan_id"], leg["leg"], now=deps.clock(), status="unknown",
                                   error_code=error.code)
            logger.warning("Trade %s leg %s outcome unknown (%s)", found["code"], leg["leg"], error.code)
            return None
        if error.status == 429:                  # A first send refused for rate: not accepted, the next run retries.
            trade_store.update_leg(conn, found["plan_id"], leg["leg"], now=deps.clock(), status="pending",
                                   error_code=error.code)
            return [f"Trade {found['code']}: rate limited, retrying at the next run."]
        trade_store.update_leg(conn, found["plan_id"], leg["leg"], now=deps.clock(), status="rejected",
                               error_code=error.code)
        return None
    trade_store.update_leg(conn, found["plan_id"], leg["leg"], now=deps.clock(), status="submitted",
                           order_id=result["orderId"])
    return None


def _cancel(conn: Any, deps: Deps, found: dict[str, Any], leg: dict[str, Any]) -> None:
    try:
        deps.client.cancel(deps.account_seq, leg["order_id"])
        trade_store.event(conn, deps.clock(), found["plan_id"], f"leg{leg['leg']}:cancel-requested", "")
    except TossError as error:    # Already filled or gone: following the order records what happened.
        trade_store.event(conn, deps.clock(), found["plan_id"], f"leg{leg['leg']}:cancel-failed", error.code)


def _follow(conn: Any, deps: Deps, found: dict[str, Any], leg: dict[str, Any]) -> dict[str, Any]:
    """Poll one submitted order for about a minute and store what the broker reports."""
    for attempt in range(POLLS):
        order = deps.client.order(deps.account_seq, leg["order_id"])
        status = _store_order(conn, deps, found["plan_id"], leg["leg"], order)
        if status != "submitted":
            break
        if attempt < POLLS - 1:
            deps.sleep(POLL_SECONDS)
    return next(l for l in trade_store.legs(conn, found["plan_id"]) if l["leg"] == leg["leg"])


def _store_order(conn: Any, deps: Deps, plan_id: str, number: int, order: dict[str, Any]) -> str:
    execution = order.get("execution") or {}
    filled = Decimal(str(execution.get("filledQuantity") or "0"))
    state = order.get("status")
    if state == "FILLED":
        status = "filled"
    elif state in _ENDED:
        status = "partial" if filled > 0 else ("rejected" if state == "REJECTED" else "canceled")
    else:
        status = "submitted"     # Open states and any state Toss adds later: keep following.
    trade_store.update_leg(conn, plan_id, number, now=deps.clock(), status=status, order_id=order.get("orderId"),
                           filled_quantity=str(filled), average_price=execution.get("averageFilledPrice"),
                           commission=execution.get("commission"), tax=execution.get("tax"),
                           settlement_date=execution.get("settlementDate"),
                           error_code="broker-rejected" if status == "rejected" else None)
    return status


def _reconcile(conn: Any, deps: Deps, found: dict[str, Any], leg: dict[str, Any]) -> dict[str, Any]:
    """Settle a leg whose order may or may not have reached Toss.

    The order list comes first: one new order (not listed before Smith sent) with the leg's side, quantity and price,
    placed while Smith was sending, is Smith's. Several stay unknown for the client. With none, the same key is resent
    only inside the idempotency window and only if a first order would pass every check now; after the window a
    complete list with no match means the order never arrived.
    """
    now = deps.clock()
    first = datetime.fromisoformat(leg["first_submit_at"])
    orders, complete = _listed(deps, leg["symbol"], first)
    prior = set(json.loads(leg["prior_orders"] or "[]"))
    window = (first - timedelta(seconds=5), first + IDEMPOTENCY_WINDOW + timedelta(minutes=2))
    matches = [o for o in orders if str(o.get("orderId")) not in prior and o.get("side") == leg["side"]
               and str(o.get("quantity")) == leg["quantity"] and o.get("price") is not None
               and Decimal(str(o.get("price"))) == Decimal(leg["limit_price"])
               and window[0] <= datetime.fromisoformat(str(o.get("orderedAt"))) <= window[1]]
    if len(matches) == 1:
        _store_order(conn, deps, found["plan_id"], leg["leg"], matches[0])
    elif not matches and now - first < IDEMPOTENCY_WINDOW:
        if _may_resend(conn, deps, found, leg, now):
            _place(conn, deps, found, leg, resend=True)
    elif not matches and complete:
        trade_store.update_leg(conn, found["plan_id"], leg["leg"], now=now, status="failed", error_code="not-placed")
    return next(l for l in trade_store.legs(conn, found["plan_id"]) if l["leg"] == leg["leg"])


def _may_resend(conn: Any, deps: Deps, found: dict[str, Any], leg: dict[str, Any], now: datetime) -> bool:
    """A resend may be the first order to arrive, so it needs everything a first send needs. It also stays on the
    KST day of the first send: the daily limit charged the leg to that day."""
    if not deps.settings.get("enabled") or trade_store.halted(conn):
        return False
    first = datetime.fromisoformat(leg["first_submit_at"])
    check = trade_plan.precheck(deps.client, account_seq=deps.account_seq, plan=found["plan"], leg=_plan_leg(found, leg),
                                quantity=int(leg["quantity"]), settings=deps.settings, now=now,
                                fixed_price=Decimal(leg["limit_price"]))
    if not check["ok"] or check["notional_krw"] > int(leg["notional_krw"] or 0):
        return False                         # never more won than the day's limit already charged for this leg
    day = local_time(first, deps.tz).replace(hour=0, minute=0, second=0, microsecond=0)
    spent = trade_store.spent_krw(conn, since=day.astimezone(timezone.utc), until=day + timedelta(days=1))
    if spent > int(deps.settings.get("max_daily_krw", trade_plan.DEFAULTS["max_daily_krw"])):
        return False                         # the limit may have been lowered since; this leg is already in `spent`
    later = deps.clock()                     # the checks took time: the date, the window and halt are read again
    return (local_time(first, deps.tz).date() == local_time(later, deps.tz).date()
            and later - first < IDEMPOTENCY_WINDOW and not trade_store.halted(conn))


def _listed(deps: Deps, symbol: str, day_of: datetime) -> tuple[list[dict[str, Any]], bool]:
    """Open and the day's closed orders for one symbol, and whether the closed list was complete (one page)."""
    day = f"{local_time(day_of, 'Asia/Seoul'):%Y-%m-%d}"     # Toss filters orders by their KST date.
    orders: list[dict[str, Any]] = []
    complete = True
    for status in ("OPEN", "CLOSED"):
        listed = deps.client.orders(deps.account_seq, status=status, symbol=symbol, day=day)
        if not isinstance(listed, dict) or not isinstance(listed.get("orders"), list):
            raise TossError(200, "invalid-response")
        orders += [o for o in listed["orders"] if isinstance(o, dict)]
        complete = complete and not listed.get("hasNext")
    return orders, complete


def _halt(conn: Any, deps: Deps, found: dict[str, Any], reason: str) -> list[str]:
    _skip_rest(conn, deps, found["plan_id"])
    trade_store.move(conn, found["plan_id"], expected=("approved", "executing", "unknown"), status="halted",
                     now=deps.clock(), note=reason)
    return _finish(conn, deps, found, "halted", reason)


def _skip_rest(conn: Any, deps: Deps, plan_id: str) -> None:
    for leg in trade_store.legs(conn, plan_id):
        trade_store.update_leg(conn, plan_id, leg["leg"], now=deps.clock(), expected=("pending",), status="skipped")


def _finish(conn: Any, deps: Deps, found: dict[str, Any], status: str, note: str | None) -> list[str]:
    if status == "done":
        trade_store.move(conn, found["plan_id"], expected=("executing", "unknown"), status="done", now=deps.clock(),
                         note=note)
    legs = trade_store.legs(conn, found["plan_id"])
    subject, text, html = trade_mail.result(found["plan"], legs, status, note)
    _send(deps, conn, found, subject, text, html)
    return [f"Trade {found['code']}: {status}" + (f" ({note})" if note else "")]


def _mail(deps: Deps, conn: Any, found: dict[str, Any], subject: str, lines: list[str]) -> None:
    text, html = trade_mail.notice(lines)
    _send(deps, conn, found, subject, text, html)


def _send(deps: Deps, conn: Any, found: dict[str, Any], subject: str, text: str, html: str) -> None:
    """Result and notice mails are best effort: a failure is recorded and never retried (no duplicate mail)."""
    try:
        deps.mail(subject, text, html)
        trade_store.event(conn, deps.clock(), found["plan_id"], "mail", subject[:120])
    except gmail.MailError as error:
        trade_store.event(conn, deps.clock(), found["plan_id"], "mail-failed", error.code)
        logger.error("Trade %s mail failed: %s", found["code"], error.code)


def _plan_leg(found: dict[str, Any], leg: dict[str, Any]) -> dict[str, Any]:
    return next(l for l in found["plan"]["legs"] if l["leg"] == leg["leg"])


def _digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()
