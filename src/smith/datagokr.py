"""The public data portal (data.go.kr) call shared by the weather and holiday sources.

One account key serves every service the household applied for; it travels only in the query string and never into
an exception, a log or a model payload. The portal answers in three shapes: a normal body with
`response.header.resultCode == "00"`, a normal body with another code (no data, out of season), or its own refusal
`OpenAPI_ServiceResponse` (service not applied for, quota, unknown service), which can come with HTTP 200.
"""
import urllib.parse
from collections.abc import Callable
from typing import Any

from smith import gmail

Getter = Callable[[str], tuple[int, dict[str, Any]]]


class Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def default_getter(url: str) -> tuple[int, dict[str, Any]]:
    return gmail.get(url, {})


def call(get: Getter, url: str, key: str, **params: str) -> dict[str, Any]:
    """One call; raises Error with network-error, portal-<reason>, http-<status>, out-of-season, no-data or result-<code>."""
    try:
        status, body = get(f"{url}?{urllib.parse.urlencode({'serviceKey': key, **params})}")
    except OSError:
        raise Error("network-error") from None
    if "OpenAPI_ServiceResponse" in body:
        refusal = body["OpenAPI_ServiceResponse"]
        header = refusal.get("cmmMsgHeader", {}) if isinstance(refusal, dict) else {}
        raise Error(f"portal-{header.get('returnReasonCode', status)}")
    if status != 200:
        raise Error(f"http-{status}")
    header = (body.get("response") or {}).get("header") or {}
    code = str(header.get("resultCode", "00"))
    if code == "00":
        return body
    if code == "99" and "제공기간" in str(header.get("resultMsg", "")):
        raise Error("out-of-season")
    raise Error("no-data" if code == "03" else f"result-{code}")


def items(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The item list: KMA and KASI wrap it in {"item": [...]} (a single item may come as a dict), AirKorea gives a list."""
    found = ((body.get("response") or {}).get("body") or {}).get("items")
    if isinstance(found, dict):
        found = found.get("item")
    if isinstance(found, dict):
        found = [found]
    return [i for i in found if isinstance(i, dict)] if isinstance(found, list) else []
