"""Trade mails: the confirmation that asks for approval, the result and halt notices, and reading the reply.

The reply is read by code only (no model): the first line must be "승인 <6 digits>" (or "OK <6 digits>") to approve,
or "취소" to decline. Anything else is not an answer. Only the client's own sent messages in the confirmation thread
count, the same rule as mail answers (FR-18); the authenticator code is checked by the executor.
"""
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any

from smith import gmail
from smith.config import local_time
from smith.qa import body_text, strip_quote

_APPROVE = re.compile(r"(?:승인|ok|okay|approve)\s*[:.\-]?\s*(\d{6})", re.IGNORECASE)
_DECLINE = re.compile(r"(?:취소|거절).*|(?:cancel|no)\b.*", re.IGNORECASE)
_SIDE = {"SELL": "매도", "BUY": "매수"}


@dataclass(frozen=True)
class Reply:
    message_id: str
    sent_at: datetime
    decision: str          # "approve" or "decline"
    code: str | None       # the authenticator code for "approve"


def subject(plan: dict[str, Any], kind: str = "거래 확인") -> str:
    what = (f"{plan['name']} {plan['quantity']}주 매도 후 재매수" if plan["rebuy"]
            else f"{plan['name']} {plan['quantity']}주 {_SIDE[plan['side']]}")
    return f"[Smith][{kind}] {what} · {plan['code']}"


def confirmation(plan: dict[str, Any], tz: str) -> tuple[str, str]:
    """(text, html) of the mail that asks the client to approve the plan."""
    when = lambda value: f"{local_time(datetime.fromisoformat(value), tz):%m/%d %H:%M}"  # noqa: E731
    unit = "$" if plan["market"] == "US" else ""
    won = "" if plan["market"] == "US" else "원"
    band, drift = plan["limit_band_pct"].rstrip("0").rstrip("."), plan["drift_halt_pct"].rstrip("0").rstrip(".")
    lines = ["고객님께서 요청하신 거래를 아래와 같이 진행하려 합니다. 승인해 주시면 진행하겠습니다.", ""]
    if plan.get("said"):
        lines.append(f"요청하신 말씀: \"{plan['said']}\"")
    lines += [f"계획 번호: {plan['code']} · 토스증권 종합매매 계좌", ""]
    first = plan["legs"][0]
    if first["side"] == "SELL":
        lines.append(f"1) 매도 {plan['name']}({plan['symbol']}) {plan['quantity']}주 · 지정가(당일) · 실행 때 현재가보다 "
                     f"{band}% 낮게, 최저 {unit}{first['floor']}{won}")
    else:
        lines.append(f"1) 매수 {plan['name']}({plan['symbol']}) {plan['quantity']}주 · 지정가(당일) · 실행 때 현재가보다 "
                     f"{band}% 높게, 최고 {unit}{first['cap']}{won}")
    if plan["rebuy"]:
        lines.append(f"2) 재매수 1에서 판 수량만큼 · 1이 모두 체결된 뒤 · 지정가(당일) · 실행 때 현재가보다 {band}% 높게, "
                     f"최고 {unit}{plan['legs'][1]['cap']}{won}")
    if plan.get("remainder"):
        lines.append(f"소수점 {plan['remainder']}주는 지정가로 팔 수 없어 남습니다.")
    fx = f" · 환율 {int(float(plan['fx'])):,}원" if plan["market"] == "US" else ""
    lines += ["", f"지금 가격 {unit}{plan['reference_price']}{won}{fx} · 주문 한 건 약 {plan['notional_krw']:,}원 · "
                  f"수수료율 {float(plan['commission_rate']) * 100:.3g}%"]
    lines += _tax_lines(plan)
    if plan.get("warnings"):
        lines.append(f"종목 유의사항: {', '.join(plan['warnings'])}")
    lines += ["", f"멈추는 조건: 실행 직전 가격이 지금보다 {drift}% 넘게 움직였거나, 매도 가능 수량이 모자라거나, 같은 종목에 "
                  "다른 미체결 주문이 있으면 주문하지 않고 다시 여쭙니다. 당일 체결되지 않은 수량은 장 마감에 자동 취소됩니다.",
              f"실행 시각: 승인을 확인한 뒤 15분 안의 정기 점검에서, {when(plan['not_before'])}부터 정규장 안에 주문합니다.",
              "", "승인: 이 메일에 답장하면서 첫 줄에 \"승인 123456\"처럼 인증 앱의 6자리를 적어 주십시오.",
              "인증 앱 코드는 이 메일 말고 다른 곳(지난 확인 메일 포함)에는 보내지 마십시오. 같은 코드가 다른 메일에 있으면 승인하지 않습니다.",
              f"취소: \"취소\"라고 답장하시거나 그냥 두시면 {when(plan['expires_at'])}에 만료됩니다."]
    return _pair(lines)


def _tax_lines(plan: dict[str, Any]) -> list[str]:
    if plan["side"] != "SELL":
        return []
    if plan["market"] == "KR":
        return ["세금: 국내 상장 주식은 소액주주의 매도 차익에 양도소득세가 없고, 매도 금액에서 증권거래세가 빠집니다."]
    gain = plan.get("gain_estimate_krw")
    if gain is None:
        return ["세금: 평균 단가를 읽지 못해 이익을 추정하지 못했습니다. 토스 앱의 해외주식 양도소득세 예상 화면을 확인해 주십시오."]
    return [f"세금 추정: 이익 약 {gain / 10_000:,.0f}만 원(평균 단가 ${plan['average_cost']}, 지금 환율로 계산). 실제 과세 이익은 "
            "산 날과 판 날의 환율로 각각 계산하고 같은 해의 다른 해외주식 매매 손익과 합치므로 다를 수 있습니다. "
            "기본공제 250만 원 안인지 토스 앱의 해외주식 양도소득세 예상 화면에서 한 번 확인해 주십시오."]


def result(plan: dict[str, Any], legs: list[dict[str, Any]], status: str, note: str | None) -> tuple[str, str, str]:
    """(subject, text, html) after a plan finished, halted or reached an unknown state."""
    unit = "$" if plan["market"] == "US" else ""
    headline = {"done": "거래를 마쳤습니다.", "halted": "거래를 멈췄습니다.",
                "unknown": "주문이 접수되었는지 아직 확인하지 못했습니다. Smith가 다음 점검에서 같은 주문 번호로 다시 확인하며 "
                           "새 주문은 내지 않습니다. 토스 앱의 주문 내역도 한 번 봐 주십시오.",
                "expired": "계획이 만료되어 주문하지 않았습니다."}.get(status, status)
    lines = [headline]
    if note:
        lines.append(f"사유: {note}")
    lines.append("")
    for leg in legs:
        label = f"{leg['leg']}) {_SIDE[leg['side']]} {plan['name']}"
        if leg.get("filled_quantity") and leg["filled_quantity"] not in ("0", "0.0"):
            lines.append(f"{label} {leg['filled_quantity']}주 체결 · 평균 {unit}{leg['average_price']} · 수수료 "
                         f"{leg.get('commission') or '-'} · 세금 {leg.get('tax') or '-'} · 결제일 {leg.get('settlement_date') or '-'}")
        else:
            lines.append(f"{label}: {_LEG_WORDS.get(leg['status'], leg['status'])}")
    if status == "halted" and any(l["side"] == "SELL" and l["status"] in ("filled", "partial") for l in legs) and plan["rebuy"]:
        lines += ["", "매도는 체결되었고 재매수는 하지 않았습니다. 판 금액은 현금으로 남아 있습니다. 다시 사려면 새로 요청해 주십시오."]
    return (subject(plan, "거래 결과"), *_pair(lines))


_LEG_WORDS = {"pending": "주문하지 않음", "skipped": "주문하지 않음", "submitted": "체결 대기", "canceled": "체결 없이 취소됨",
              "rejected": "증권사가 거부함", "failed": "주문하지 않음", "unknown": "결과 확인 필요", "submitting": "결과 확인 필요"}


def notice(text_lines: list[str]) -> tuple[str, str]:
    return _pair(text_lines)


def _pair(lines: list[str]) -> tuple[str, str]:
    text = "\n".join(lines)
    body = "".join(f"<p style=\"margin:0 0 8px\">{html.escape(line)}</p>" if line else "<br>" for line in lines)
    return text, f"<div style=\"font-family:sans-serif;font-size:14px;color:#111;line-height:1.55\">{body}</div>"


def decide(text: str) -> tuple[str, str | None] | None:
    """("approve", code), ("decline", None) or None from the reply's first non-empty line."""
    first = next((line.strip() for line in strip_quote(text).splitlines() if line.strip()), "")
    approve = _APPROVE.fullmatch(first)
    if approve:
        return "approve", approve.group(1)
    if _DECLINE.fullmatch(first):
        return "decline", None
    return None


def replies(token: str, thread_id: str, *, recipient: str, after: datetime, seen: set[str],
            getter: gmail.Getter = gmail.get) -> list[Reply]:
    """The client's approve or decline replies in the confirmation thread, oldest first."""
    found = []
    address = recipient.strip().lower()
    for message in gmail.thread_headers(token, thread_id, getter=getter).get("messages", []):
        message_id = str(message.get("id", ""))
        headers = {h.get("name", "").lower(): h.get("value", "") for h in message.get("payload", {}).get("headers", [])}
        if not message_id or message_id in seen or gmail.SMITH_HEADER.lower() in headers:
            continue
        if "SENT" not in message.get("labelIds", []) or parseaddr(headers.get("from", ""))[1].lower() != address:
            continue
        sent_at = datetime.fromtimestamp(int(message.get("internalDate", "0")) / 1000, tz=timezone.utc)
        if sent_at <= after:
            continue
        decided = decide(body_text(gmail.message_body(token, message_id, getter=getter)))
        if decided:
            found.append(Reply(message_id, sent_at, *decided))
    return sorted(found, key=lambda reply: reply.sent_at)
