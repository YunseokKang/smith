"""OpenDART, the FSS electronic disclosure system: company filings read by code (FR-43).

Verified against the official guide (opendart.fss.or.kr/guide, DS001) and live responses on 2026-10-29:
- corpCode.xml: a zip with CORPCODE.xml, every company's 8-digit corp_code with its 6-digit stock_code (blank when
  unlisted). Only the common share's code is listed: a KR preferred share (005935) maps to its issuer through the
  first five digits (005930).
- list.json: filings of a corp_code between two dates (YYYYMMDD), status "000" on success, "013" when there is none.
  Exchange filings (pblntf_ty I) such as 현금ㆍ현물배당결정 are listed, corrections carry "[기재정정]".
- document.xml: a zip with the filing as one HTML-like file. A dividend decision is a fixed form: 1. 배당구분,
  3. 1주당 배당금(원) with rows 보통주식 and 종류주식, 6. 배당기준일, 7. 배당금지급 예정일자, 10. 이사회결의일 (dates
  as YYYY-MM-DD).

The key travels only in the query string, never in an exception, a log or a model payload; redirects are refused
(smith.net). Responses are bounded in size, and the corp-code list is cached next to the ledger for a week.
"""
import io
import json
import re
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import unescape
from pathlib import Path

from smith.net import open_url

BASE = "https://opendart.fss.or.kr/api/"
VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="
TIMEOUT_SECONDS = 30
MAX_BYTES = 20_000_000          # The corp-code zip was 3.6 MB on 2026-10-29.
CACHE_AGE = timedelta(days=7)
LOOKBACK = timedelta(days=120)  # A quarter and a little: the newest dividend decision of a quarterly payer.
_CODE = re.compile(r"\d{6}")

Fetcher = Callable[[str], bytes]


class Error(Exception):
    """A failed DART call. `code`: network-error, http-<status>, status-<DART status>, bad-response."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Filing:
    rcept_no: str
    filed_on: date
    title: str
    corp_name: str

    @property
    def url(self) -> str:
        return VIEWER + self.rcept_no


@dataclass(frozen=True)
class Dividend:
    """A dividend decision as the filing states it. `per_share` maps "common" and "preferred" to won; `cash` is True
    only when 배당종류 says 현금배당 (an in-kind or mixed dividend is no cash amount)."""
    filing: Filing
    kind: str
    cash: bool
    per_share: dict[str, Decimal]
    record_date: date | None
    pay_date: date | None
    decided_on: date | None


def default_fetcher(url: str) -> bytes:
    try:
        with open_url(url, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise Error(f"http-{error.code}") from None
    except (OSError, ValueError):
        raise Error("network-error") from None
    if len(body) > MAX_BYTES:
        raise Error("bad-response")
    return body


def _get(fetch: Fetcher, path: str, key: str, **params: str) -> bytes:
    try:
        return fetch(f"{BASE}{path}?{urllib.parse.urlencode({'crtfc_key': key, **params})}")
    except Error:
        raise
    except OSError:
        raise Error("network-error") from None


def corp_code(symbol: str, key: str, cache: Path, *, now: datetime, fetch: Fetcher = default_fetcher) -> str | None:
    """The issuer's corp_code for a KR listed share (a preferred share maps through its common share), or None."""
    if not _CODE.fullmatch(symbol):
        return None
    codes = _cached_codes(cache, now)
    if codes is None:
        codes = _download_codes(key, fetch)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"saved_at": now.isoformat(), "codes": codes}), encoding="utf-8")
        except OSError:
            pass    # Public codes: an unwritable cache only means downloading them again next time.
    return codes.get(symbol) or codes.get(symbol[:5] + "0")


def _cached_codes(cache: Path, now: datetime) -> dict[str, str] | None:
    try:
        stored = json.loads(cache.read_text(encoding="utf-8"))
        if now - datetime.fromisoformat(stored["saved_at"]) <= CACHE_AGE and isinstance(stored["codes"], dict):
            return {str(k): str(v) for k, v in stored["codes"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def _download_codes(key: str, fetch: Fetcher) -> dict[str, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(_get(fetch, "corpCode.xml", key))) as archive:
            root = ET.fromstring(archive.read(archive.namelist()[0]))
    except (zipfile.BadZipFile, IndexError, ET.ParseError, KeyError):
        raise Error("bad-response") from None
    codes = {}
    for row in root.iter("list"):
        stock, corp = (row.findtext("stock_code") or "").strip(), (row.findtext("corp_code") or "").strip()
        if _CODE.fullmatch(stock) and corp:
            codes[stock] = corp
    if not codes:
        raise Error("bad-response")
    return codes


def filings(corp: str, key: str, *, since: date, until: date, fetch: Fetcher = default_fetcher) -> list[Filing]:
    """The company's filings received in [since, until], newest first (one page of 100 is plenty for 120 days)."""
    raw = _get(fetch, "list.json", key, corp_code=corp, bgn_de=f"{since:%Y%m%d}", end_de=f"{until:%Y%m%d}",
               page_count="100")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Error("bad-response") from None
    if not isinstance(body, dict):
        raise Error("bad-response")
    status = str(body.get("status", ""))
    if status == "013":
        return []
    if status != "000":
        raise Error(f"status-{status}")
    found = []
    for row in body.get("list", []) if isinstance(body.get("list"), list) else []:
        if not isinstance(row, dict):
            continue
        try:
            filed = datetime.strptime(str(row["rcept_dt"]), "%Y%m%d").date()
        except (KeyError, ValueError):
            continue
        number = str(row.get("rcept_no", ""))
        if re.fullmatch(r"\d{14}", number):
            found.append(Filing(number, filed, " ".join(str(row.get("report_nm", "")).split()),
                                str(row.get("corp_name", ""))))
    return sorted(found, key=lambda f: f.rcept_no, reverse=True)


def document(filing: Filing, key: str, *, fetch: Fetcher = default_fetcher) -> str:
    """The filing's text (HTML-like) from its original document zip."""
    raw = _get(fetch, "document.xml", key, rcept_no=filing.rcept_no)
    if not raw.startswith(b"PK"):
        raise Error("bad-response")   # DART answers an error as XML with status and message instead of a zip.
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            body = archive.read(archive.namelist()[0])
    except (zipfile.BadZipFile, IndexError, KeyError):
        raise Error("bad-response") from None
    for encoding in ("utf-8", "cp949"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise Error("bad-response")


MAX_DECISIONS = 3


def latest_dividend(corp: str, key: str, *, today: date, fetch: Fetcher = default_fetcher) -> Dividend | None:
    """The newest dividend decided in the last LOOKBACK, or None. The newest filing is not always it: a correction of
    an earlier quarter's decision can be filed after this quarter's (review 2026-10-29), so the MAX_DECISIONS newest
    decision filings are read and the one with the latest record date wins; among filings of one dividend (same record
    date) the later filing, its correction, wins."""
    decisions = [f for f in filings(corp, key, since=today - LOOKBACK, until=today, fetch=fetch)
                 if "배당결정" in f.title.replace(" ", "")][:MAX_DECISIONS]
    read = [parse_dividend(document(f, key, fetch=fetch), f) for f in decisions]
    if not read:
        return None
    return max(read, key=lambda d: (d.record_date or date.min, d.decided_on or date.min, d.filing.rcept_no))


def parse_dividend(text: str, filing: Filing) -> Dividend:
    """Read the dividend decision form. Raises Error("bad-response") when no per-share amount is found."""
    label, fields, per_share = "", {}, {}
    for cells in _rows(text):
        head = re.match(r"^(\d+)\.\s*(.+)$", cells[0])
        if head:
            label, rest = head.group(2), cells[1:]
        elif cells[0].startswith(("-", "※")):
            continue
        else:
            rest = cells
        if not rest:
            continue
        if "1주당 배당금" in label and len(rest) >= 2 and rest[0] in ("보통주식", "종류주식"):
            amount = _won(rest[-1])
            if amount is not None:
                per_share["common" if rest[0] == "보통주식" else "preferred"] = amount
        elif label.replace(" ", "") in ("배당구분", "배당종류", "배당기준일", "배당금지급예정일자", "이사회결의일(결정일)"):
            fields.setdefault(label.replace(" ", ""), rest[-1])
    if not per_share:
        raise Error("bad-response")
    # The 종류주식 table lists each preferred class (종류주식명, 구분, 1주당 배당금, ...): with several amounts, or a
    # class without one ("-"), one "preferred" figure would be a guess for some class, so none is given.
    classes = [_won(cells[2]) for cells in _rows(text) if len(cells) == 5 and cells[0] != "종류주식명"]
    if None in classes or len(set(classes)) > 1:
        per_share.pop("preferred", None)
    return Dividend(filing, fields.get("배당구분", ""), fields.get("배당종류", "") == "현금배당", per_share,
                    _iso(fields.get("배당기준일")),
                    _iso(fields.get("배당금지급예정일자")), _iso(fields.get("이사회결의일(결정일)")))


def _rows(text: str) -> list[list[str]]:
    """Table rows as lists of cell texts (tags dropped, entities decoded, blank cells kept out)."""
    found = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S | re.I):
        cells = [" ".join(unescape(re.sub(r"<[^>]+>", " ", cell)).split())
                 for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)]
        cells = [c for c in cells if c]
        if cells:
            found.append(cells)
    return found


def _won(text: str) -> Decimal | None:
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _iso(text: str | None) -> date | None:
    try:
        return date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def cache_path(db: Path) -> Path:
    """Public corp codes, cached beside the ledger (git-ignored data folder)."""
    return db.parent / "dart" / "corp_codes.json"


# --- material filings of a holding (event notes, FR-44) --------------------------------------------------------------

# What a filing title says about the holding, by fixed words in the title (spaces removed). Anything else (periodic
# reports, routine notices) is not an event. Order matters: the first group that matches names the filing.
CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dividend", ("배당결정",)),
    ("dilution", ("유상증자결정", "전환사채권발행결정", "신주인수권부사채권발행결정", "교환사채권발행결정", "감자결정")),
    ("return", ("자기주식취득결정", "자기주식처분결정", "주식소각결정", "무상증자결정", "자기주식취득신탁계약")),
    ("earnings", ("잠정)실적", "매출액또는손익구조")),
    ("risk", ("최대주주변경", "소송등의제기", "소송등의판결", "회생절차", "부도발생", "영업정지", "횡령", "배임",
              "불성실공시법인", "관리종목", "상장폐지", "매매거래정지", "감사의견")),
    ("corporate", ("합병결정", "분할결정", "분할합병결정", "영업양수결정", "영업양도결정", "주식교환", "장래사업")),
)
# The FSS structured API for a filing whose figures code reads (verified 2026-10-29 on live filings).
_STRUCTURED = {"유상증자결정": "piicDecsn.json", "전환사채권발행결정": "cvbdIsDecsn.json", "자기주식취득결정": "tsstkAqDecsn.json"}


def category(title: str) -> str | None:
    plain = title.replace(" ", "")
    return next((name for name, words in CATEGORIES if any(word in plain for word in words)), None)


def structured(filing: Filing, corp: str, key: str, *, fetch: Fetcher = default_fetcher) -> dict[str, str] | None:
    """The filing's own figures from the structured API (유상증자, 전환사채, 자기주식 취득), or None for other filings.

    Raises:
        Error: the call failed or the filing is not in the answer.
    """
    api = next((path for word, path in _STRUCTURED.items() if word in filing.title.replace(" ", "")), None)
    if api is None:
        return None
    day = f"{filing.filed_on:%Y%m%d}"
    try:
        body = json.loads(_get(fetch, api, key, corp_code=corp, bgn_de=day, end_de=day).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Error("bad-response") from None
    if not isinstance(body, dict) or str(body.get("status")) != "000":
        raise Error(f"status-{body.get('status') if isinstance(body, dict) else 'bad'}")
    row = next((r for r in body.get("list") or [] if isinstance(r, dict) and r.get("rcept_no") == filing.rcept_no), None)
    if row is None:
        raise Error("not-listed")
    return {str(k): str(v) for k, v in row.items()} | {"api": api}


def number(text: str | None) -> Decimal | None:
    """A figure as the structured API writes it ("12,200,000,000", "8.12"; "-" when not applicable)."""
    try:
        value = Decimal(str(text or "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None
