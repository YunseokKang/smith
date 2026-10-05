"""Source verification for research items: does the cited page exist, and does it say what the item says?

The research model reports a URL for every finding. A host check alone (research.tier) cannot tell a
real official page from a dead or invented one, so before a brief is stored each URL is fetched:

- SSRF and DNS rebinding: the host is resolved once, every address must be public, and the connection
  goes to that checked address (TLS still verifies the certificate against the host name). A later DNS
  answer cannot redirect the request to a private or loopback address.
- Redirects are followed by hand, at most MAX_REDIRECTS, and each hop is checked like the first URL.
- Claim-body match: the numbers in the item's headline and detail are looked up in the page text.
- Stored per item: status, final URL, HTTP status, fetch time, SHA-256 of the body, matched numbers.

Outcomes are lenient, matching the client's choice (doubts are flagged, not deleted):
- dropped: the page is gone (404/410), the name does not resolve, or the address is not public;
- "matched": reachable and at least half of the item's numbers are in the page;
- "reachable": reachable, nothing checkable (no numbers, PDF/HWP, script-rendered page);
- "mismatch": reachable but none of the numbers appear; kept for context, never as an official source;
- "unchecked": blocked, timed out or server error; kept with its host tier.
"""
import hashlib
import html
import http.client
import ipaddress
import re
import socket
import ssl
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 10
WORKERS = 6
_DROP_STATUS = (404, 410)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_YEAR = re.compile(r"(?:19|20)\d{2}")

Resolver = Callable[[str], list[str]]
Requester = Callable[[str, str, str], tuple[int, dict[str, str], bytes]]  # (ip, host, path) -> response


class SourceRejected(Exception):
    """The URL must not be fetched or is gone; `code` is the drop reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


def https_get(ip: str, host: str, path: str) -> tuple[int, dict[str, str], bytes]:
    """GET https://host/path from the already-checked address `ip`, certificate verified for `host`."""
    context = ssl.create_default_context()
    raw = socket.create_connection((ip, 443), timeout=TIMEOUT_SECONDS)
    try:
        sock = context.wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise
    conn = http.client.HTTPSConnection(host, 443, timeout=TIMEOUT_SECONDS, context=context)
    conn.sock = sock  # http.client then skips its own connect (and its own DNS lookup).
    try:
        conn.request("GET", path or "/", headers={"User-Agent": "Mozilla/5.0 (Smith source check)",
                                                  "Accept": "text/html,application/xhtml+xml,*/*;q=0.5",
                                                  "Accept-Language": "ko,en;q=0.5"})
        response = conn.getresponse()
        body = response.read(MAX_BYTES + 1)[:MAX_BYTES]
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    finally:
        conn.close()


def public_address(ip: str) -> bool:
    address = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def fetch(url: str, *, resolver: Resolver = resolve, requester: Requester = https_get) -> dict[str, Any]:
    """Fetch a public https page, following up to MAX_REDIRECTS checked redirects.

    Raises:
        SourceRejected: "bad-url", "private-address", "no-such-host", "dead-url" (404/410) or
            "redirect-loop"; the caller drops the item.
        OSError, ssl.SSLError, http.client.HTTPException: the page could not be checked (kept, unchecked).
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host or parts.port not in (None, 443) or parts.username or parts.password:
            raise SourceRejected("bad-url")
        try:
            ipaddress.ip_address(host)
            raise SourceRejected("bad-url")  # IP literals are never a source.
        except ValueError:
            pass
        try:
            addresses = resolver(host)
        except socket.gaierror:
            raise SourceRejected("no-such-host") from None
        if not addresses or not all(public_address(a) for a in addresses):
            raise SourceRejected("private-address")
        path = parts.path or "/"
        status, headers, body = requester(addresses[0], host, path + (f"?{parts.query}" if parts.query else ""))
        if status in (301, 302, 303, 307, 308) and headers.get("location"):
            current = urljoin(current, headers["location"])
            continue
        if status in _DROP_STATUS:
            raise SourceRejected("dead-url")
        return {"status": status, "headers": headers, "body": body, "final_url": current}
    raise SourceRejected("redirect-loop")


def page_text(headers: dict[str, str], body: bytes) -> str | None:
    """Visible text of an HTML or plain-text page; None for other content (PDF, HWP, images)."""
    kind = headers.get("content-type", "").lower()
    if kind and not any(t in kind for t in ("text/html", "text/plain", "application/xhtml")):
        return None
    charset = re.search(r"charset=([\w-]+)", kind) or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    names = [charset.group(1).decode() if isinstance(charset.group(1), bytes) else charset.group(1)] if charset else []
    for name in names + ["utf-8", "cp949"]:
        try:
            text = body.decode(name)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    else:
        text = body.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text)))


def numbers(text: str) -> set[str]:
    """Checkable numbers: commas removed; years and single digits are too common to prove anything."""
    found = set()
    for token in _NUMBER.findall(text):
        plain = token.replace(",", "").rstrip(".")
        if not plain or _YEAR.fullmatch(plain) or (plain.isdigit() and len(plain) < 2):
            continue
        found.add(plain)
    return found


def check(item: dict[str, str], *, now: datetime, resolver: Resolver = resolve,
          requester: Requester = https_get) -> dict[str, Any]:
    """The check record for one item. Raises SourceRejected when the item must be dropped."""
    try:
        page = fetch(item["source_url"], resolver=resolver, requester=requester)
    except SourceRejected:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as error:
        return {"status": "unchecked", "reason": type(error).__name__, "checked_at": now.isoformat(timespec="seconds")}
    record = {"final_url": page["final_url"], "http_status": page["status"],
              "checked_at": now.isoformat(timespec="seconds"), "sha256": hashlib.sha256(page["body"]).hexdigest()}
    if page["status"] != 200:
        return {"status": "unchecked", "reason": f"http-{page['status']}", **record}
    text = page_text(page["headers"], page["body"])
    claimed = numbers(f"{item['headline']} {item['detail']}")
    if text is None or not claimed:
        return {"status": "reachable", **record}
    seen = numbers(text)
    hits = sorted(claimed & seen)
    status = "matched" if len(hits) * 2 >= len(claimed) else ("mismatch" if not hits else "reachable")
    return {"status": status, "matched": f"{len(hits)}/{len(claimed)}", **record}


def verify(items: list[dict[str, Any]], *, now: datetime | None = None, resolver: Resolver = resolve,
           requester: Requester = https_get, tier: Callable[[str], str] | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Check every item's source (a few at a time). Returns (kept items with a "check" record, drop counts).
    Kept items are renumbered R1, R2, ... in their original order. A "mismatch" item is never "official";
    the tier of a redirected page is the tier of its final host."""
    now = now or datetime.now(timezone.utc)

    def one(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | str]:
        try:
            return item, check(item, now=now, resolver=resolver, requester=requester)
        except SourceRejected as rejected:
            return item, rejected.code
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(one, items))
    kept, dropped = [], {}
    for item, outcome in results:
        if isinstance(outcome, str):
            dropped[outcome] = dropped.get(outcome, 0) + 1
            continue
        checked = dict(item, check=outcome, ref=f"R{len(kept) + 1}")
        if tier is not None and outcome.get("final_url"):
            checked["tier"] = tier(outcome["final_url"]) if checked.get("tier") == "official" else checked.get("tier")
        if outcome["status"] == "mismatch":
            checked["tier"] = "secondary"
        kept.append(checked)
    return kept, dropped
