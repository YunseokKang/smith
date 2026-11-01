"""The Toss order client: the only code that can place or cancel an order (trading design §4).

Only `trade_executor` uses it, and only for a plan the client approved with an authenticator code. The adviser,
reports, mail answers and research never import it (a test pins that boundary). Beyond the read-only client's
allowlist it allows exactly two writes, and checks each order body before anything reaches the network: a limit,
day-only, whole-share cash order that never sets the high-value confirmation flag.
"""
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from smith.toss import ForbiddenRequest, TossClient, TossError, _segment

_ORDER_KEYS = {"clientOrderId", "symbol", "side", "orderType", "timeInForce", "quantity", "price",
               "confirmHighValueOrder"}
_CLIENT_ORDER_ID = re.compile(r"[A-Za-z0-9_\-]{1,36}")
_SYMBOL = re.compile(r"[A-Za-z0-9.\-]{1,20}")
_WHOLE = re.compile(r"[1-9][0-9]{0,8}")
_PRICE = re.compile(r"[0-9]{1,12}(\.[0-9]{1,4})?")


class OrderClient(TossClient):
    """Read-only client plus order placement and cancellation."""

    def place(self, account_seq: int, order: dict[str, Any]) -> dict[str, Any]:
        """Send one order and return {"orderId", "clientOrderId"}.

        Raises:
            ForbiddenRequest: the body is outside the allowed order shape (nothing was sent).
            TossError: status 0 (network) or >= 500 means the order may have been accepted.
        """
        check_order(order)
        return self._post("/api/v1/orders", order, account_seq)

    def cancel(self, account_seq: int, order_id: str) -> dict[str, Any]:
        return self._post(f"/api/v1/orders/{_segment(order_id)}/cancel", {}, account_seq)

    def _allows(self, method: str, path: str) -> bool:
        if super()._allows(method, path):      # the reads and token issuance
            return True
        parts = path.strip("/").split("/")
        return method == "POST" and (parts == ["api", "v1", "orders"] or (
            len(parts) == 5 and parts[:3] == ["api", "v1", "orders"] and parts[4] == "cancel"))

    def _post(self, path: str, body: dict[str, Any], account_seq: int) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        for attempt in range(2):
            headers = {"X-Tossinvest-Account": str(account_seq), "Content-Type": "application/json",
                       "Authorization": f"Bearer {self._access_token()}"}
            try:
                payload = self._send("POST", path, None, headers, data)
            except TossError as error:
                # A rejected token means the order was not accepted; the same clientOrderId makes the resend safe.
                if attempt == 0 and error.status == 401 and error.code in ("expired-token", "invalid-token"):
                    self._token = None
                    continue
                raise
            result = payload.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("orderId"), str):
                raise TossError(200, "invalid-response")
            return result
        raise AssertionError("unreachable")


def check_order(order: dict[str, Any]) -> None:
    """Refuse any order but a whole-share LIMIT DAY order with a client order id (trading design §2)."""
    if set(order) - _ORDER_KEYS:
        raise ForbiddenRequest("order has fields outside the allowed shape")
    if not _CLIENT_ORDER_ID.fullmatch(str(order.get("clientOrderId", ""))):
        raise ForbiddenRequest("order needs a clientOrderId")
    if not _SYMBOL.fullmatch(str(order.get("symbol", ""))):
        raise ForbiddenRequest("invalid symbol")
    if order.get("side") not in ("BUY", "SELL"):
        raise ForbiddenRequest("invalid side")
    if order.get("orderType") != "LIMIT" or order.get("timeInForce") != "DAY":
        raise ForbiddenRequest("only LIMIT DAY orders are allowed")
    if order.get("confirmHighValueOrder", False) is not False:
        raise ForbiddenRequest("high-value orders are not allowed")
    if not _WHOLE.fullmatch(str(order.get("quantity", ""))):
        raise ForbiddenRequest("quantity must be a whole number of shares")
    price = str(order.get("price", ""))
    try:
        if not _PRICE.fullmatch(price) or Decimal(price) <= 0:
            raise ForbiddenRequest("invalid price")
    except InvalidOperation:
        raise ForbiddenRequest("invalid price") from None
