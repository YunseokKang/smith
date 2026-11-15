"""Official apartment prices (공동주택 공시가격) for the holding tax (requirement FR-53).

The price each home's 재산세 and 종합부동산세 are levied on is published once a year (1 January, announced end of
April). The documented official API is the Ministry's V-World service "공동주택가격속성조회" (getApartHousingPriceAttr,
a V-World key; data.go.kr's entry 15124003 only links there). It is asked by lot number (PNU, 19 digits), which the
MOLIT trade records already carry (시군구·읍면동·대지구분·본번·부번), so the client is never asked for an address.

Building and unit numbers are not kept (the local config deliberately stores neither), so a home's price is the
median of the same complex's units of the same floor area on that lot, with the count beside it. Prices are read
once a year and kept in `data/official-prices.json` (ignored by Git); a failure leaves the home without an official
price and the holding tax falls back to an estimate it labels as such, never to zero.

The response layout was taken from the V-World documentation, not from a live call: the parser looks for the rows
by their field names and fails visibly ("no-rows") when it finds none.
"""
import json
import os
import sqlite3
import statistics
import urllib.parse
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from smith.net import open_url
from smith.realestate import AREA_TOLERANCE, pnu  # noqa: F401 - pnu is part of this module's surface.

ENDPOINT = "https://api.vworld.kr/ned/data/getApartHousingPriceAttr"
STORE = "official-prices.json"
PAGE_ROWS = 1000
MAX_PAGES = 30   # 30,000 rows: a lot of a 5,678-unit complex answered 11,356 rows for 2026 (two per unit).
_TIMEOUT_SECONDS = 20

Getter = Callable[[str], tuple[int, bytes]]


class OfficialPriceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def urllib_get(url: str) -> tuple[int, bytes]:
    with open_url(url, timeout=_TIMEOUT_SECONDS) as response:
        return response.status, response.read()


def fetch(lot: str, year: int, api_key: str, *, domain: str | None = None, get: Getter = urllib_get) -> list[dict[str, Any]]:
    """Every unit row of `lot` for `year`: {dong, ho, area, price, year}. The key is never logged.

    The answer must be whole: every page up to the stated total, every row with its price, area and year. A page
    that stops early, a row missing a field, or more pages than MAX_PAGES fail the lot (Codex review 2026-11-15: a
    partial first page was stored as the lot's median), so the store keeps the price it had.

    Raises:
        OfficialPriceError: network-error, http-<status>, invalid-response, api-<code>, invalid-row, incomplete,
            no-rows.
    """
    if not (lot.isdigit() and len(lot) == 19):
        raise OfficialPriceError("bad-pnu")
    rows: list[dict[str, Any]] = []
    total: int | None = None
    for page in range(1, MAX_PAGES + 1):
        query = {"key": api_key, "pnu": lot, "stdrYear": str(year), "format": "json", "numOfRows": PAGE_ROWS,
                 "pageNo": page}
        if domain:
            query["domain"] = domain
        try:
            status, body = get(f"{ENDPOINT}?{urllib.parse.urlencode(query)}")
        except OSError:
            raise OfficialPriceError("network-error") from None
        if status != 200:
            raise OfficialPriceError(f"http-{status}")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OfficialPriceError("invalid-response") from None
        error = _error_code(document)
        if error:
            raise OfficialPriceError(f"api-{error}")
        items = _items(document)
        parsed = [_row(item) for item in items]
        if any(row is None for row in parsed):
            raise OfficialPriceError("invalid-row")
        rows.extend(parsed)
        stated = _total(document)
        total = stated if stated is not None else total
        if total is not None:
            if len(rows) >= total:
                break
            if not items:
                raise OfficialPriceError("incomplete")
        elif len(items) < PAGE_ROWS:
            break   # No total stated: a short page is the last one.
    else:
        raise OfficialPriceError("incomplete")
    if total is not None and len(rows) != total:
        raise OfficialPriceError("incomplete")
    if not rows:
        raise OfficialPriceError("no-rows")
    return rows


def _items(document: Any) -> list[dict[str, Any]]:
    """The unit rows wherever the document nests them: any list of objects carrying a published price."""
    if isinstance(document, list):
        if document and all(isinstance(i, dict) for i in document) and any("pblntfPc" in i for i in document):
            return document
        return [row for part in document for row in _items(part)]
    if isinstance(document, dict):
        if "pblntfPc" in document:
            return [document]
        return [row for value in document.values() for row in _items(value)]
    return []


def _error_code(document: Any) -> str | None:
    if isinstance(document, dict):
        for key in ("error", "errorCode", "resultCode"):
            value = document.get(key)
            if isinstance(value, dict):
                return str(value.get("code") or value.get("text") or "error")[:40]
            if isinstance(value, str) and value and value not in ("0", "00", "000", "OK", "INFO-000"):
                return value[:40]
        status = document.get("status")
        if isinstance(status, str) and status.upper() == "ERROR":
            return "error"
        for value in document.values():
            found = _error_code(value)
            if found:
                return found
    return None


def _total(document: Any) -> int | None:
    if isinstance(document, dict):
        for key, value in document.items():
            if key == "totalCount":
                try:
                    return int(str(value))
                except ValueError:
                    return None
            found = _total(value)
            if found is not None:
                return found
    return None


def _row(item: dict[str, Any]) -> dict[str, Any] | None:
    try:
        price = Decimal(str(item.get("pblntfPc")).replace(",", ""))
        area = Decimal(str(item.get("prvuseAr")))
        year = int(str(item.get("stdrYear")))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if price <= 0 or area <= 0:
        return None
    return {"dong": str(item.get("dongNm") or ""), "ho": str(item.get("hoNm") or ""), "area": area, "price": price,
            "year": year, "updated": str(item.get("lastUpdtDt") or "")}


def median_for(rows: list[dict[str, Any]], *, area: Decimal, year: int) -> tuple[Decimal | None, int]:
    """(median price of the same-area units of `year`, how many units), (None, 0) when none match.

    V-World lists a unit more than once (2026-11-15: every unit of two large complexes came twice, same month and
    price), so a unit (dong, ho) counts once, its most recently updated row."""
    units: dict[tuple[str, str], dict[str, Any]] = {}
    unnamed = []
    for row in rows:
        if row["year"] != year or abs(row["area"] - area) > AREA_TOLERANCE:
            continue
        if not (row["dong"] or row["ho"]):
            unnamed.append(row)
            continue
        key = (row["dong"], row["ho"])
        if key not in units or row.get("updated", "") > units[key].get("updated", ""):
            units[key] = row
    prices = [row["price"] for row in [*units.values(), *unnamed]]
    if not prices:
        return None, 0
    return Decimal(statistics.median(prices)), len(prices)


def load(folder: Path) -> dict[str, Any]:
    """The stored prices and lot numbers: {"lots": {ref: pnu}, "prices": {ref: {year, price, units, read_at}}}."""
    try:
        found = json.loads((folder / STORE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"lots": {}, "prices": {}}
    if not isinstance(found, dict):
        return {"lots": {}, "prices": {}}
    return {"lots": found.get("lots") if isinstance(found.get("lots"), dict) else {},
            "prices": found.get("prices") if isinstance(found.get("prices"), dict) else {}}


def store_for(conn: Any) -> dict[str, Any]:
    """The store kept beside the ledger file `conn` is open on; empty for an in-memory ledger."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return {"lots": {}, "prices": {}}
    path = next((row[2] for row in rows if row[1] == "main"), "")
    return load(Path(path).parent) if path else {"lots": {}, "prices": {}}


def save(folder: Path, store: dict[str, Any]) -> None:
    """Write the whole store through a temporary file of this process's own (two writers never share one)."""
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / f"{STORE}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(store, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        os.replace(temporary, folder / STORE)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _updating(folder: Path) -> Iterator[dict[str, Any]]:
    """The latest store, read and written back under an OS file lock: the official-price sync and the trade sync's
    lot numbers may run at once, and each merges its change into what the other wrote (Codex review 2026-11-15)."""
    from smith.memory import _locked   # The ledger-side folder gets the same lock the client memory uses.

    folder.mkdir(parents=True, exist_ok=True)
    with _locked(folder):
        store = load(folder)
        yield store
        save(folder, store)


def price_of(store: dict[str, Any], ref: str, year: int) -> tuple[Decimal | None, int]:
    """(official price, units it is the median of) stored for `ref` and `year`, or (None, 0)."""
    entry = (store.get("prices") or {}).get(ref)
    if not isinstance(entry, dict) or entry.get("year") != year:
        return None, 0
    try:
        return Decimal(str(entry["price"])), int(entry.get("units") or 0)
    except (InvalidOperation, KeyError, ValueError):
        return None, 0


def sync(folder: Path, specs: dict[str, dict[str, Any]], api_key: str, *, year: int, now: datetime,
         domain: str | None = None, get: Getter = urllib_get) -> dict[str, str]:
    """Read `year`'s price of every ref with a known lot number and store it. Returns ref -> outcome ("ok",
    "no-lot", or an error code); a failed ref keeps whatever was stored before. The calls run outside the lock."""
    lots = load(folder)["lots"]
    outcomes, found = {}, {}
    for ref, spec in specs.items():
        lot = lots.get(ref)
        if not lot:
            outcomes[ref] = "no-lot"
            continue
        try:
            rows = fetch(lot, year, api_key, domain=domain, get=get)
        except OfficialPriceError as error:
            outcomes[ref] = error.code
            continue
        price, units = median_for(rows, area=Decimal(str(spec["exclusive_area_m2"])), year=year)
        if price is None:
            outcomes[ref] = "no-same-area"
            continue
        found[ref] = {"year": year, "price": str(price), "units": units, "read_at": now.isoformat(), "pnu": lot}
        outcomes[ref] = "ok"
    if found:
        with _updating(folder) as store:
            store["prices"].update(found)
    return outcomes


def remember_lots(folder: Path, deals: list[dict[str, Any]]) -> int:
    """Keep the lot number of each ref from its newest trade that carries one; returns how many refs changed."""
    newest: dict[str, str] = {}
    for deal in sorted(deals, key=lambda d: d.get("deal_date") or ""):
        if deal.get("pnu"):
            newest[deal["property_ref"]] = deal["pnu"]
    if not newest:
        return 0
    with _updating(folder) as store:
        changed = sum(1 for ref, lot in newest.items() if store["lots"].get(ref) != lot)
        store["lots"].update(newest)
    return changed
