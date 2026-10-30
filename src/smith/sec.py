"""SEC EDGAR: filings of the client's US-listed holdings read by code (FR-44).

Verified against sec.gov (EDGAR APIs, fair-access policy) and live responses on 2026-10-29:
- No key, but every request carries a User-Agent naming a contact e-mail, at most 10 requests a second. The contact is
  the configured [mail] recipient (user decision 2026-10-29); it goes to the SEC only, in that header.
- www.sec.gov/files/company_tickers.json maps a ticker to its CIK (companies only: ETFs such as VOO are not in it).
- data.sec.gov/submissions/CIK##########.json lists recent filings as parallel arrays under filings.recent:
  accessionNumber, filingDate, form, items ("2.02,9.01" for an 8-K, empty otherwise), primaryDocument,
  primaryDocDescription. A foreign issuer files 6-K (no items) instead of 8-K.

What a filing means comes from its form and 8-K item codes (fixed SEC numbering), never from reading its English text.
Redirects are refused (smith.net); responses are bounded; the ticker map is cached beside the ledger for a week.
"""
import gzip
import http.client
import io
import json
import zlib
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smith.net import open_url

TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{document}"
TIMEOUT_SECONDS = 30
MAX_BYTES = 20_000_000
CACHE_AGE = timedelta(days=7)
SPACING_SECONDS = 0.15          # Under the SEC's 10 requests a second.

# 8-K items (SEC Form 8-K General Instructions) by what they mean for a holder, in Korean, and their category.
ITEMS: dict[str, tuple[str, str]] = {
    "1.01": ("중요 계약 체결", "corporate"), "1.02": ("중요 계약 해지", "corporate"), "1.03": ("파산·회생 절차", "risk"),
    "1.05": ("중대한 사이버 보안 사고", "risk"), "2.01": ("인수·자산 매각 완료", "corporate"), "2.02": ("실적 발표", "earnings"),
    "2.04": ("채무 조기 상환 사유 발생", "risk"), "2.05": ("구조조정 비용", "risk"), "2.06": ("자산 손상 인식", "risk"),
    "3.01": ("상장 유지 기준 미달·상장폐지 통지", "risk"), "3.02": ("미등록 주식 발행", "dilution"),
    "3.03": ("주주 권리 변경", "dilution"), "4.01": ("회계감사인 변경", "risk"), "4.02": ("과거 재무제표 신뢰 불가", "risk"),
    "5.01": ("지배권 변경", "corporate"), "5.02": ("경영진·이사 변경", "corporate"),
}
FORMS: dict[str, tuple[str, str]] = {
    "6-K": ("해외 발행사 수시 보고", "corporate"),
    "SC 13D": ("5% 이상 지분 보유자의 경영 참여 목적 보고", "corporate"),
    "SCHEDULE 13D": ("5% 이상 지분 보유자의 경영 참여 목적 보고", "corporate"),
}
_ORDER = ("dividend", "dilution", "risk", "return", "earnings", "corporate")

Fetcher = Callable[[str, str], bytes]   # (url, contact) -> body


class Error(Exception):
    """A failed SEC call. `code`: network-error, http-<status>, bad-response, no-contact."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Filing:
    accession: str
    filed_on: date
    form: str
    items: tuple[str, ...]
    url: str


def default_fetcher(url: str, contact: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"Smith personal adviser {contact}",
                                                   "Accept-Encoding": "gzip"})
    try:
        with open_url(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_BYTES + 1)
            zipped = response.headers.get("Content-Encoding") == "gzip"
    except urllib.error.HTTPError as error:
        error.close()
        raise Error(f"http-{error.code}") from None
    except (OSError, ValueError, http.client.HTTPException):   # IncompleteRead is an HTTPException, not an OSError.
        raise Error("network-error") from None
    finally:
        time.sleep(SPACING_SECONDS)
    if len(body) > MAX_BYTES:
        raise Error("bad-response")
    if not zipped:
        return body
    try:
        # Bounded after decompression too: a small gzip can expand to any size (review 2026-10-29).
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            plain = stream.read(MAX_BYTES + 1)
    except (OSError, EOFError, zlib.error):
        raise Error("bad-response") from None
    if len(plain) > MAX_BYTES:
        raise Error("bad-response")
    return plain


def _json(fetch: Fetcher, url: str, contact: str) -> Any:
    if not contact:
        raise Error("no-contact")
    try:
        return json.loads(fetch(url, contact).decode("utf-8"))
    except Error:
        raise
    except (UnicodeDecodeError, ValueError):
        raise Error("bad-response") from None
    except OSError:
        raise Error("network-error") from None


def cik(ticker: str, contact: str, cache: Path, *, now: datetime, fetch: Fetcher = default_fetcher) -> int | None:
    """The company's CIK for a US ticker, or None (a fund, or not a filer)."""
    codes = _cached(cache, now)
    if codes is None:
        body = _json(fetch, TICKERS, contact)
        if not isinstance(body, dict):
            raise Error("bad-response")
        codes = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in body.values()
                 if isinstance(v, dict) and v.get("ticker") and str(v.get("cik_str", "")).isdigit()}
        if not codes:
            raise Error("bad-response")
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"saved_at": now.isoformat(), "codes": codes}), encoding="utf-8")
        except OSError:
            pass    # Public data: an unwritable cache only means downloading it again next time.
    return codes.get(ticker.upper())


def _cached(cache: Path, now: datetime) -> dict[str, int] | None:
    try:
        stored = json.loads(cache.read_text(encoding="utf-8"))
        if now - datetime.fromisoformat(stored["saved_at"]) <= CACHE_AGE and isinstance(stored["codes"], dict):
            return {str(k): int(v) for k, v in stored["codes"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def filings(company: int, contact: str, *, since: date, fetch: Fetcher = default_fetcher) -> list[Filing]:
    """The company's recent filings filed on or after `since`, newest first."""
    body = _json(fetch, SUBMISSIONS.format(cik=company), contact)
    recent = ((body.get("filings") or {}).get("recent") or {}) if isinstance(body, dict) else {}
    columns = ("accessionNumber", "filingDate", "form", "items", "primaryDocument")
    if not isinstance(recent, dict) or not all(isinstance(recent.get(c), list) for c in columns):
        raise Error("bad-response")
    found = []
    for accession, filed, form, items, document in zip(*(recent[c] for c in columns)):
        try:
            day = date.fromisoformat(str(filed))
        except ValueError:
            continue
        if day < since:
            continue
        folder = str(accession).replace("-", "")
        found.append(Filing(str(accession), day, str(form), tuple(i.strip() for i in str(items or "").split(",") if i.strip()),
                            ARCHIVE.format(cik=company, folder=folder, document=document) if document and folder.isdigit()
                            else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={company}"))
    return sorted(found, key=lambda f: (f.filed_on, f.accession), reverse=True)


def meaning(filing: Filing) -> tuple[str, list[str]] | None:
    """(category, Korean labels) of a material filing, the most consequential category first; None when routine
    (periodic reports, insider forms, exhibits only)."""
    if filing.form in FORMS:
        label, kind = FORMS[filing.form]
        return kind, [label]
    if filing.form not in ("8-K", "8-K/A"):
        return None
    hits = [ITEMS[i] for i in filing.items if i in ITEMS]
    if not hits:
        return None
    kind = min((k for _, k in hits), key=_ORDER.index)
    labels = [label for label, _ in hits] + (["정정 보고"] if filing.form == "8-K/A" else [])
    return kind, labels


def cache_path(db: Path) -> Path:
    """Public ticker map, cached beside the ledger (git-ignored data folder)."""
    return db.parent / "sec" / "company_tickers.json"
