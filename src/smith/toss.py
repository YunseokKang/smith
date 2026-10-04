"""Read-only Toss Securities Open API client.

The provider has no read-only scope: the same token can place orders. Every request is
therefore checked against an allowlist before it reaches the network. See docs/toss-openapi.md.
"""
import http.client
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from smith.net import open_url

logger = logging.getLogger(__name__)

BASE_URL = "https://openapi.tossinvest.com"
_TIMEOUT_SECONDS = 10
# Token issuance is authentication, not a financial action. Nothing else may use POST.
_ALLOWED = frozenset({
    ("POST", "/oauth2/token"),
    ("GET", "/api/v1/accounts"),
    ("GET", "/api/v1/holdings"),
    ("GET", "/api/v1/buying-power"),
    ("GET", "/api/v1/exchange-rate"),
})

# (method, url, headers, body) -> (status, payload bytes)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class TossError(Exception):
    """A failed Toss request. Carries only safe diagnostics: status, error code and request ID."""

    def __init__(self, status: int, code: str, request_id: str | None = None) -> None:
        super().__init__(f"HTTP {status} {code}" + (f" (requestId {request_id})" if request_id else ""))
        self.status, self.code, self.request_id = status, code, request_id


class ForbiddenRequest(Exception):
    """A request outside the read-only allowlist was attempted; nothing was sent."""


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with open_url(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read()
    except (OSError, http.client.HTTPException):
        # DNS failure, timeout, refused or dropped connection. URLError is an OSError. The
        # original message may contain hosts or request details, so only a code is kept.
        raise TossError(0, "network-error") from None


class TossClient:
    """Read-only client. One access token is issued per client instance and reused until rejected.

    Toss keeps one valid token per API client, so issuing a token here invalidates tokens
    held by any other tool that uses the same client credentials.
    """

    def __init__(self, client_id: str, client_secret: str, *, transport: Transport = urllib_transport) -> None:
        self._client_id, self._client_secret = client_id, client_secret
        self._transport = transport
        self._token: str | None = None
        self.token_expires_in: int | None = None

    def accounts(self) -> list[dict[str, Any]]:
        return self._get("/api/v1/accounts")

    def holdings(self, account_seq: int) -> dict[str, Any]:
        return self._get("/api/v1/holdings", account_seq=account_seq)

    def buying_power(self, account_seq: int, currency: str) -> dict[str, Any]:
        return self._get("/api/v1/buying-power", {"currency": currency}, account_seq=account_seq)

    def exchange_rate(self, base: str, quote: str) -> dict[str, Any]:
        return self._get("/api/v1/exchange-rate", {"baseCurrency": base, "quoteCurrency": quote})

    def _get(self, path: str, query: dict[str, str] | None = None, *, account_seq: int | None = None) -> Any:
        headers = {} if account_seq is None else {"X-Tossinvest-Account": str(account_seq)}
        for attempt in range(2):
            headers["Authorization"] = f"Bearer {self._access_token()}"
            try:
                payload = self._send("GET", path, query, headers)
            except TossError as error:
                # An expired or replaced token is re-issued once; other failures surface.
                if attempt == 0 and error.status == 401 and error.code in ("expired-token", "invalid-token"):
                    self._token = None
                    continue
                raise
            if "result" not in payload:
                raise TossError(200, "invalid-response")
            return payload["result"]
        raise AssertionError("unreachable")

    def _access_token(self) -> str:
        if self._token is None:
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self._client_id,
                                           "client_secret": self._client_secret}).encode("ascii")
            payload = self._send("POST", "/oauth2/token", None,
                                 {"Content-Type": "application/x-www-form-urlencoded"}, body)
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise TossError(200, "invalid-response")
            self._token, self.token_expires_in = token, payload.get("expires_in")
        return self._token

    def _send(self, method: str, path: str, query: dict[str, str] | None, headers: dict[str, str],
              body: bytes | None = None) -> dict[str, Any]:
        if (method, path) not in _ALLOWED:
            raise ForbiddenRequest(f"{method} {path} is not an allowed read-only request")
        url = BASE_URL + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
        status, raw = self._transport(method, url, {"Accept": "application/json", **headers}, body)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            raise TossError(status, "non-json-response") from None
        if not 200 <= status < 300 or not isinstance(payload, dict):
            # API errors use {"error": {"code", "requestId", ...}}; the OAuth endpoint may use
            # {"error": "<code>"}. Messages may echo request details, so only codes are kept.
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                raise TossError(status, str(error.get("code") or "unknown"), error.get("requestId"))
            raise TossError(status, error if isinstance(error, str) else "unknown")
        logger.debug("Toss %s %s -> %s", method, path, status)
        return payload
