"""Turn the client's trade request into a checked plan, and check a leg again right before its order (trading design §5).

Everything here is deterministic code over read-only Toss responses. A check that fails, or a value Smith cannot read,
rejects the plan with a reason in Korean: unknown is never treated as passing. Prices are Decimal throughout.
"""
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from smith.toss import TossClient

# Orders go in only inside the regular session, after the opening minutes and well before the close.
OPEN_MARGIN = timedelta(minutes=2)
CLOSE_MARGIN = timedelta(minutes=15)
# Warnings that stop a buy (a sell out of such a stock stays possible; the plan names the warning).
BUY_BLOCKING = {"LIQUIDATION_TRADING", "INVESTMENT_RISK", "INVESTMENT_WARNING", "OVERHEATED"}
# A volatility interruption pauses continuous trading for minutes: the order waits for a later run.
VI_WARNINGS = {"VI_STATIC", "VI_DYNAMIC", "VI_STATIC_AND_DYNAMIC"}
DEFAULTS = {"max_order_krw": 5_000_000, "max_daily_krw": 10_000_000, "plan_ttl_minutes": 60,
            "limit_band_pct": 1.0, "drift_halt_pct": 3.0}
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class PlanRejected(Exception):
    """The request cannot become a plan; `reason` is a Korean sentence for the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Request:
    """What the client asked for. `quantity` is a whole number of shares or None for every whole share held."""

    side: str            # "SELL" or "BUY"
    symbol: str
    quantity: int | None
    rebuy: bool = False  # SELL then BUY back the filled quantity (tax-gain harvest)
    said: str = ""       # The client's words, quoted in the confirmation mail


def build(client: TossClient, *, account_seq: int, account: str, request: Request, settings: dict[str, Any],
          now: datetime, spent_today_krw: int) -> dict[str, Any]:
    """A plan dict ready for trade_store.add_plan. Raises PlanRejected."""
    if request.side not in ("SELL", "BUY") or (request.rebuy and request.side != "SELL"):
        raise PlanRejected("요청 형식을 알 수 없습니다(매도, 매수, 매도 후 재매수만 가능합니다).")
    info = _stock(client, request.symbol)
    market = "KR" if info["currency"] == "KRW" else "US"
    held = _holding(client, account_seq, request.symbol)
    quantity = _quantity(client, account_seq, request, held)
    price = _price(client, request.symbol)
    fx = _fx(client) if market == "US" else Decimal(1)
    rate = _commission_rate(client, account_seq, market)
    warnings = active_warnings(client, request.symbol, now.date())
    if request.side == "BUY" and BUY_BLOCKING & set(warnings):
        raise PlanRejected(f"매수 유의 종목으로 지정되어 있어 매수 계획을 만들지 않습니다({', '.join(sorted(warnings))}).")
    not_before, expires_at = window(client, market, now, settings)
    band, drift = _pct(settings, "limit_band_pct"), _pct(settings, "drift_halt_pct")
    notional = to_krw(price * quantity, fx)
    sides = ["SELL", "BUY"] if request.rebuy else [request.side]
    cap = int(settings.get("max_order_krw", DEFAULTS["max_order_krw"]))
    if notional * (1 + drift + band) > cap:
        raise PlanRejected(f"주문 한 건 한도({cap:,}원)를 넘습니다(약 {notional:,}원).")
    daily = int(settings.get("max_daily_krw", DEFAULTS["max_daily_krw"]))
    if spent_today_krw + notional * len(sides) > daily:
        raise PlanRejected(f"하루 한도({daily:,}원)를 넘습니다(오늘 이미 {spent_today_krw:,}원, 이번 계획 약 "
                           f"{notional * len(sides):,}원).")
    if request.side == "BUY" and not request.rebuy:
        _check_buying_power(client, account_seq, info["currency"], price * quantity * (1 + band) * (1 + rate))
    plan_id = uuid.uuid4().hex
    legs = []
    for number, side in enumerate(sides, start=1):
        legs.append({"leg": number, "side": side, "symbol": request.symbol, "client_order_id": f"smith-{plan_id[:16]}-{number}",
                     # The buy-back leg buys exactly what the sell filled; it is fixed when that leg finishes.
                     "quantity": str(quantity) if number == 1 else None,
                     "floor": fmt_price(market, round_tick(market, info, price * (1 - drift) * (1 - band), ROUND_FLOOR)),
                     "cap": fmt_price(market, round_tick(market, info, price * (1 + drift) * (1 + band), ROUND_CEILING))})
    gain = None
    if request.side == "SELL" and market == "US" and held.get("averagePurchasePrice"):
        gain = to_krw((price - _decimal(held["averagePurchasePrice"], "averagePurchasePrice")) * quantity, fx)
    return {"plan_id": plan_id, "code": "T-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)),
            "account": account, "said": request.said[:300], "symbol": request.symbol, "name": info["name"],
            "market": market, "security_type": info.get("securityType"), "currency": info["currency"],
            "side": request.side, "rebuy": request.rebuy, "quantity": str(quantity),
            "remainder": str(held["remainder"]) if held.get("remainder") else None,
            "reference_price": str(price), "fx": str(fx), "commission_rate": str(rate), "notional_krw": notional,
            "gain_estimate_krw": gain, "average_cost": held.get("averagePurchasePrice"), "warnings": warnings,
            "limit_band_pct": str(band * 100), "drift_halt_pct": str(drift * 100),
            "not_before": not_before.isoformat(), "expires_at": expires_at.isoformat(), "legs": legs,
            "created_at": now.isoformat()}


def precheck(client: TossClient, *, account_seq: int, plan: dict[str, Any], leg: dict[str, Any], quantity: int,
             settings: dict[str, Any], now: datetime, fixed_price: Decimal | None = None) -> dict[str, Any]:
    """Right before one leg's order: {"ok": True, "price", "notional_krw"} or {"ok": False, "reason", "wait": bool}.

    `wait` means try again at a later run (outside the session, a volatility interruption); otherwise the plan halts.
    `fixed_price` checks an order whose price is already set (a resend under the same key) instead of pricing anew.
    """
    market, symbol = plan["market"], plan["symbol"]
    session = _session(client, market, now)
    if session is None or not session[0] + OPEN_MARGIN <= now <= session[1] - CLOSE_MARGIN:
        return {"ok": False, "wait": True, "reason": "정규장 주문 시간이 아닙니다."}
    warnings = active_warnings(client, symbol, now.date())
    if VI_WARNINGS & set(warnings):
        return {"ok": False, "wait": True, "reason": "변동성 완화장치가 발동 중입니다."}
    if leg["side"] == "BUY" and BUY_BLOCKING & set(warnings):
        return {"ok": False, "wait": False, "reason": f"매수 유의 종목 지정({', '.join(sorted(warnings))})"}
    open_orders = client.orders(account_seq, status="OPEN", symbol=symbol)
    if not isinstance(open_orders, dict) or open_orders.get("orders"):
        return {"ok": False, "wait": False, "reason": "같은 종목에 체결을 기다리는 다른 주문이 있습니다."}
    if leg["side"] == "SELL":
        sellable = _decimal(client.sellable_quantity(account_seq, symbol).get("sellableQuantity"), "sellableQuantity")
        if sellable < quantity:
            return {"ok": False, "wait": False, "reason": f"매도 가능 수량이 계획과 다릅니다(지금 {sellable}주)."}
    price = _price(client, symbol)
    reference = Decimal(plan["reference_price"])
    drift = _pct(settings, "drift_halt_pct")
    if abs(price / reference - 1) > drift:
        return {"ok": False, "wait": False,
                "reason": f"승인 뒤 가격이 {drift * 100:.0f}% 넘게 움직였습니다({plan['reference_price']} → {price})."}
    band = _pct(settings, "limit_band_pct")
    info = {"securityType": plan.get("security_type")}
    if fixed_price is not None:
        limit = fixed_price
    elif leg["side"] == "SELL":
        limit = max(round_tick(market, info, price * (1 - band), ROUND_FLOOR), Decimal(leg["floor"]))
    else:
        limit = min(round_tick(market, info, price * (1 + band), ROUND_CEILING), Decimal(leg["cap"]))
    fx = _fx(client) if market == "US" else Decimal(1)
    notional = to_krw(limit * quantity, fx)
    if notional > int(settings.get("max_order_krw", DEFAULTS["max_order_krw"])):
        return {"ok": False, "wait": False, "reason": "주문 한 건 한도를 넘습니다."}
    if leg["side"] == "BUY":
        rate = Decimal(plan["commission_rate"])
        try:
            _check_buying_power(client, account_seq, plan["currency"], limit * quantity * (1 + rate))
        except PlanRejected as rejected:
            return {"ok": False, "wait": False, "reason": rejected.reason}
    return {"ok": True, "price": fmt_price(market, limit), "notional_krw": notional, "session_end": session[1]}


def window(client: TossClient, market: str, now: datetime, settings: dict[str, Any]) -> tuple[datetime, datetime]:
    """(not_before, expires_at): the current session if enough of it is left, else the next one."""
    ttl = timedelta(minutes=int(settings.get("plan_ttl_minutes", DEFAULTS["plan_ttl_minutes"])))
    sessions = sessions_of(client.market_calendar(market), market)
    for start, end in sessions:
        last = end - CLOSE_MARGIN
        if start <= now and now + timedelta(minutes=10) <= last:
            return now, min(now + ttl, last)
        if now < start:
            return start + OPEN_MARGIN, last
    raise PlanRejected("다음 정규장 일정을 확인하지 못했습니다.")


def sessions_of(calendar: Any, market: str) -> list[tuple[datetime, datetime]]:
    """Regular sessions (start, end) from a market-calendar response, oldest first."""
    if not isinstance(calendar, dict):
        raise PlanRejected("장 운영 시간 응답을 읽지 못했습니다.")
    found = []
    for key in ("previousBusinessDay", "today", "nextBusinessDay"):
        day = calendar.get(key) or {}
        regular = day.get("regularMarket") if market == "US" else (day.get("integrated") or {}).get("regularMarket")
        if regular:
            try:
                found.append((datetime.fromisoformat(regular["startTime"]), datetime.fromisoformat(regular["endTime"])))
            except (KeyError, TypeError, ValueError):
                raise PlanRejected("장 운영 시간 응답을 읽지 못했습니다.") from None
    return sorted(set(found))


def _session(client: TossClient, market: str, now: datetime) -> tuple[datetime, datetime] | None:
    return next(((s, e) for s, e in sessions_of(client.market_calendar(market), market) if s <= now < e), None)


def active_warnings(client: TossClient, symbol: str, today: date) -> list[str]:
    active = []
    for item in client.warnings(symbol) or []:
        start, end = item.get("startDate"), item.get("endDate")
        if (start is None or date.fromisoformat(start) <= today) and (end is None or today <= date.fromisoformat(end)):
            active.append(str(item.get("warningType")))
    return sorted(set(active))


def round_tick(market: str, info: dict[str, Any], price: Decimal, rounding: str) -> Decimal:
    tick = tick_size(market, info.get("securityType"), price)
    return (price / tick).to_integral_value(rounding=rounding) * tick


def tick_size(market: str, security_type: str | None, price: Decimal) -> Decimal:
    """KRX tick sizes (stocks since the 2023 reform; ETFs and ETNs 1 won under 2,000 and 5 won above); US cents."""
    if market == "US":
        return Decimal("0.01") if price >= 1 else Decimal("0.0001")
    if security_type in ("ETF", "ETN", "FOREIGN_ETF"):
        return Decimal(1) if price < 2000 else Decimal(5)
    for bound, tick in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if price < bound:
            return Decimal(tick)
    return Decimal(1000)


def fmt_price(market: str, price: Decimal) -> str:
    if market == "KR":
        return str(int(price))
    return f"{price:.2f}" if price >= 1 else f"{price:.4f}"


def to_krw(amount: Decimal, fx: Decimal) -> int:
    return int((amount * fx).to_integral_value(rounding=ROUND_CEILING))


def _stock(client: TossClient, symbol: str) -> dict[str, Any]:
    found = [s for s in client.stocks([symbol]) or [] if isinstance(s, dict) and s.get("symbol") == symbol]
    if len(found) != 1:
        raise PlanRejected(f"{symbol} 종목 정보를 찾지 못했습니다.")
    info = found[0]
    if info.get("status") != "ACTIVE" or info.get("currency") not in ("KRW", "USD"):
        raise PlanRejected(f"{info.get('name', symbol)}은(는) 지금 거래할 수 있는 상장 상태가 아닙니다.")
    detail = info.get("koreanMarketDetail") or {}
    if detail.get("krxTradingSuspended") or detail.get("liquidationTrading"):
        raise PlanRejected(f"{info.get('name', symbol)}은(는) 거래정지 또는 정리매매 중입니다.")
    return info


def _holding(client: TossClient, account_seq: int, symbol: str) -> dict[str, Any]:
    items = (client.holdings(account_seq) or {}).get("items")
    if not isinstance(items, list):
        raise PlanRejected("보유 주식을 읽지 못했습니다.")
    found = [i for i in items if isinstance(i, dict) and i.get("symbol") == symbol]
    return dict(found[0]) if found else {}


def _quantity(client: TossClient, account_seq: int, request: Request, held: dict[str, Any]) -> int:
    if request.side == "BUY":
        if not request.quantity or request.quantity < 1:
            raise PlanRejected("매수 수량(1주 이상)이 필요합니다.")
        return request.quantity
    if not held:
        raise PlanRejected(f"{request.symbol}을(를) 보유하고 있지 않습니다.")
    owned = _decimal(held.get("quantity"), "quantity")
    whole = int(owned.to_integral_value(rounding=ROUND_FLOOR))
    quantity = whole if request.quantity is None else request.quantity
    held["remainder"] = owned - whole if request.quantity is None and owned != whole else None
    if quantity < 1 or quantity > whole:
        raise PlanRejected(f"매도 수량이 보유 수량({owned}주) 안의 1주 이상이어야 합니다.")
    sellable = _decimal(client.sellable_quantity(account_seq, request.symbol).get("sellableQuantity"), "sellableQuantity")
    if sellable < quantity:
        raise PlanRejected(f"지금 매도 가능한 수량은 {sellable}주입니다.")
    return quantity


def _price(client: TossClient, symbol: str) -> Decimal:
    found = [p for p in client.prices([symbol]) or [] if isinstance(p, dict) and p.get("symbol") == symbol]
    if len(found) != 1:
        raise PlanRejected("현재가를 읽지 못했습니다.")
    price = _decimal(found[0].get("lastPrice"), "lastPrice")
    if price <= 0:
        raise PlanRejected("현재가를 읽지 못했습니다.")
    return price


def _fx(client: TossClient) -> Decimal:
    rate = _decimal((client.exchange_rate("USD", "KRW") or {}).get("midRate"), "midRate")
    if rate <= 0:
        raise PlanRejected("환율을 읽지 못했습니다.")
    return rate


def _commission_rate(client: TossClient, account_seq: int, market: str) -> Decimal:
    rates = [c for c in client.commissions(account_seq) or [] if isinstance(c, dict) and c.get("marketCountry") == market]
    if not rates:
        raise PlanRejected("수수료율을 읽지 못했습니다.")
    return _decimal(rates[0].get("commissionRate"), "commissionRate")


def _check_buying_power(client: TossClient, account_seq: int, currency: str, needed: Decimal) -> None:
    power = _decimal(client.buying_power(account_seq, currency).get("cashBuyingPower"), "cashBuyingPower")
    if power < needed:
        raise PlanRejected(f"매수 가능 금액이 부족합니다(필요 약 {needed:.2f} {currency}, 가능 {power} {currency}).")


def _pct(settings: dict[str, Any], key: str) -> Decimal:
    return Decimal(str(settings.get(key, DEFAULTS[key]))) / 100


def _decimal(value: Any, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PlanRejected(f"응답 값({name})을 읽지 못했습니다.") from None
    if not number.is_finite():
        raise PlanRejected(f"응답 값({name})을 읽지 못했습니다.")
    return number
