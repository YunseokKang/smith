"""Official central bank announcements (titles, links and times) from verified RSS feeds.

Feed contents are untrusted external data: only metadata is stored, links must point at the
issuing institution over https, titles are cleaned and truncated, and nothing in a feed is ever
treated as an instruction. Feed URLs were verified on 2026-10-04; the BOK monetary policy decision
board's RSS address was not found and is deliberately not guessed.
"""
import http.client
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from smith.net import open_url

_TIMEOUT_SECONDS = 15
_MAX_BYTES = 2_000_000
_TITLE_MAX = 300
# C0/C1 controls, zero-width and bidirectional formatting characters, Unicode line/paragraph
# separators and the BOM: anything that can hide or reorder text in a terminal or a prompt.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿]+")


@dataclass(frozen=True)
class Feed:
    feed_id: str
    url: str
    allowed_host: str  # Links must stay on this host or its subdomains.
    label: str


FEEDS = (
    Feed("fed-monetary", "https://www.federalreserve.gov/feeds/press_monetary.xml", "federalreserve.gov",
         "Federal Reserve monetary policy press releases"),
    Feed("bok-press-conference", "https://www.bok.or.kr/portal/bbs/B0000169/news.rss?menuNo=200064",
         "bok.or.kr", "Bank of Korea governor press conferences"),
    Feed("bok-mpb-minutes", "https://www.bok.or.kr/portal/bbs/B0000245/news.rss?menuNo=200761",
         "bok.or.kr", "Bank of Korea Monetary Policy Board minutes"),
)


@dataclass(frozen=True)
class Announcement:
    feed_id: str
    link: str
    title: str
    published_at: datetime


class FeedError(Exception):
    def __init__(self, feed_id: str, code: str) -> None:
        super().__init__(f"{feed_id}: {code}")
        self.feed_id, self.code = feed_id, code


def urllib_fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "Smith/0.1 (personal read-only adviser)"})
    try:
        with open_url(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.status, response.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, b""
    except (OSError, http.client.HTTPException):
        raise FeedError("http", "network-error") from None


def fetch_feed(feed: Feed, fetch: Callable[[str], tuple[int, bytes]] = urllib_fetch) -> list[Announcement]:
    """Fetch and parse one feed. Items with a foreign link or no usable date are skipped.

    Raises:
        FeedError: the request failed, the body is too large, or it is not an RSS document with items.
    """
    try:
        status, body = fetch(feed.url)
    except FeedError as error:
        raise FeedError(feed.feed_id, error.code) from None
    if status != 200:
        raise FeedError(feed.feed_id, f"http-{status}")
    if len(body) > _MAX_BYTES:
        raise FeedError(feed.feed_id, "too-large")
    try:
        # Python's bundled expat (2.4.1+) limits entity expansion attacks; the size cap bounds the rest.
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise FeedError(feed.feed_id, "invalid-response") from None
    items = root.findall("./channel/item")
    if not items:
        raise FeedError(feed.feed_id, "no-items")
    announcements = [a for a in (_item(feed, item) for item in items) if a is not None]
    if not announcements:
        raise FeedError(feed.feed_id, "no-valid-items")
    return announcements


def _item(feed: Feed, item: ElementTree.Element) -> Announcement | None:
    link = (item.findtext("link") or "").strip()
    try:
        parts = urlsplit(link)
        host = parts.hostname or ""
    except ValueError:  # For example "https://[broken": an invalid item, not a failed feed.
        return None
    if parts.scheme != "https" or not (host == feed.allowed_host or host.endswith("." + feed.allowed_host)):
        return None
    try:
        published_at = parsedate_to_datetime((item.findtext("pubDate") or "").strip())
    except (TypeError, ValueError):
        return None
    if published_at.tzinfo is None:
        return None
    title = _CONTROL.sub(" ", item.findtext("title") or "").strip()[:_TITLE_MAX]
    return Announcement(feed.feed_id, link, title, published_at) if title else None
