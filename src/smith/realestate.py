"""Official apartment transactions (MOLIT, via data.go.kr) for the household's properties.

Each property record is matched locally (config `[properties."record-id"]`, ignored by Git) to its
complex name and exclusive floor area. Transactions are fetched per district and month, filtered here,
and only same-complex, same-size deals are stored. Complex names stay on this PC; reports and the
narrative stage see only derived figures (median price, change, jeonse level versus the deposit owed).
"""
import statistics
import urllib.parse
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from smith.net import open_url

_BASE = "https://apis.data.go.kr/1613000"
_PATHS = {"trade": "RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
          "rent": "RTMSDataSvcAptRent/getRTMSDataSvcAptRent"}
_TIMEOUT_SECONDS = 20
_PAGE_ROWS = 1000
AREA_TOLERANCE = Decimal("1.0")   # Square metres; the same floor plan is listed with small differences.
MANWON = Decimal(10_000)          # MOLIT amounts are in units of 10,000 won.
MONTHS = 13                       # Two half-years back from today need 13 calendar months.
MIN_BASIS = 3                     # Fewer deals than this make a median "for reference only".

Getter = Callable[[str], tuple[int, bytes]]


class RealEstateError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def urllib_get(url: str) -> tuple[int, bytes]:
    with open_url(url, timeout=_TIMEOUT_SECONDS) as response:
        return response.status, response.read()


def fetch_month(kind: str, lawd_cd: str, year_month: str, api_key: str, get: Getter = urllib_get) -> list[dict[str, Any]]:
    """All transactions of one district and month, parsed. The key is passed as the portal issued it
    (already percent-encoded keys are not encoded twice) and never logged."""
    key = api_key if "%" in api_key else urllib.parse.quote(api_key, safe="")
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"LAWD_CD": lawd_cd, "DEAL_YMD": year_month, "pageNo": page,
                                        "numOfRows": _PAGE_ROWS})
        try:
            status, body = get(f"{_BASE}/{_PATHS[kind]}?serviceKey={key}&{query}")
        except OSError:
            raise RealEstateError("network-error") from None
        if status != 200:
            raise RealEstateError(f"http-{status}")
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            raise RealEstateError("invalid-response") from None
        code = (root.findtext(".//resultCode") or "").strip()
        if code not in ("000", "00"):
            raise RealEstateError(f"api-{code or 'unknown'}")
        items = root.findall(".//item")
        rows.extend(_parse(kind, item) for item in items)
        total = int((root.findtext(".//totalCount") or "0").strip() or 0)
        if page * _PAGE_ROWS >= total or not items:
            return [row for row in rows if row is not None]
        page += 1


def _parse(kind: str, item: ElementTree.Element) -> dict[str, Any] | None:
    text = lambda tag: (item.findtext(tag) or "").strip()  # noqa: E731
    try:
        deal = date(int(text("dealYear")), int(text("dealMonth")), int(text("dealDay")))
        area = Decimal(text("excluUseAr"))
        amount = Decimal((text("dealAmount") if kind == "trade" else text("deposit")).replace(",", "")) * MANWON
        rent = Decimal((text("monthlyRent") or "0").replace(",", "")) * MANWON if kind == "rent" else Decimal(0)
    except (ValueError, InvalidOperation):
        return None
    return {"kind": kind, "apt_name": text("aptNm"), "dong": text("umdNm"), "deal_date": deal, "area": area,
            "price": amount, "monthly_rent": rent, "floor": text("floor"),
            "contract_type": text("contractType") or None, "cancelled": text("cdealType") == "O"}


def months_back(today: date, count: int) -> list[str]:
    found = []
    year, month = today.year, today.month
    for _ in range(count):
        found.append(f"{year}{month:02d}")
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return found


def validate_properties(properties: Any) -> dict[str, dict[str, Any]]:
    """The `[properties]` config, checked: a bad entry is an error naming the field, never a crash later.

    Raises:
        RealEstateError: code "bad-config:<record>.<field>".
    """
    if not isinstance(properties, dict):
        raise RealEstateError("bad-config:properties")
    checked = {}
    for ref, spec in properties.items():
        if not isinstance(spec, dict):
            raise RealEstateError(f"bad-config:{ref}")
        for field, ok in (("lawd_cd", lambda v: isinstance(v, str) and v.isdigit() and len(v) == 5),
                          ("apt_name", lambda v: isinstance(v, str) and 0 < len(v) <= 60),
                          ("dong", lambda v: v is None or (isinstance(v, str) and 0 < len(v) <= 20)),
                          ("exclusive_area_m2", lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 1000),
                          ("acquired_year", lambda v: v is None or (isinstance(v, int) and 1950 <= v <= 2100)),
                          ("acquired_price", lambda v: v is None or (isinstance(v, int) and v > 0))):
            if not ok(spec.get(field)):
                raise RealEstateError(f"bad-config:{ref}.{field}")
        checked[ref] = spec
    return checked


def sync(properties: dict[str, dict[str, Any]], api_key: str, *, today: date, months: int = MONTHS,
         get: Getter = urllib_get) -> tuple[list[dict[str, Any]], set[tuple[str, str, str]]]:
    """Fetch trades and rents for each configured property and keep same-complex, same-size deals.
    Returns (matched deals, every (property, kind, YYYYMM) fetched) so the store can replace those months."""
    properties = validate_properties(properties)
    cache: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    matched, fetched = [], set()
    for ref, spec in properties.items():
        area = Decimal(str(spec["exclusive_area_m2"]))
        for kind in ("trade", "rent"):
            for year_month in months_back(today, months):
                fetched.add((ref, kind, year_month))
                key = (kind, spec["lawd_cd"], year_month)
                if key not in cache:
                    cache[key] = fetch_month(kind, spec["lawd_cd"], year_month, api_key, get)
                for row in cache[key]:
                    if row["apt_name"] == spec["apt_name"] and row["dong"] == spec.get("dong", row["dong"]) \
                            and abs(row["area"] - area) <= AREA_TOLERANCE:
                        matched.append({"property_ref": ref, "kind": kind, "deal_date": row["deal_date"].isoformat(),
                                        "area": str(row["area"]), "price": str(row["price"]),
                                        "monthly_rent": str(row["monthly_rent"]), "floor": row["floor"],
                                        "contract_type": row["contract_type"], "cancelled": row["cancelled"]})
    return matched, fetched


def analyze(deals: list[dict[str, Any]], *, today: date) -> dict[str, dict[str, Any]]:
    """Per property: market price estimate from recent trades and the jeonse level from recent pure
    jeonse contracts (no monthly rent). Cancelled trades are excluded."""
    result: dict[str, dict[str, Any]] = {}
    for ref in sorted({d["property_ref"] for d in deals}):
        mine = [d for d in deals if d["property_ref"] == ref]
        # Each stored row stands for `active_count` identical, not cancelled deals.
        trades = sorted((date.fromisoformat(d["deal_date"]), Decimal(d["price"])) for d in mine if d["kind"] == "trade"
                        for _ in range(int(d["active_count"])))
        jeonse = sorted((date.fromisoformat(d["deal_date"]), Decimal(d["price"])) for d in mine
                        if d["kind"] == "rent" and Decimal(d["monthly_rent"]) == 0 for _ in range(int(d["active_count"])))
        trades = [(day, p) for day, p in trades if (today - day).days <= 366]
        recent = [p for day, p in trades if (today - day).days <= 183]
        older = [p for day, p in trades if 183 < (today - day).days]
        recent_jeonse = [p for day, p in jeonse if (today - day).days <= 183]
        comparable = len(recent) >= MIN_BASIS and len(older) >= MIN_BASIS
        result[ref] = {
            "trades_12m": len(trades), "latest_trade": trades[-1] if trades else None,
            "estimate": _median(recent), "estimate_basis": len(recent), "previous_basis": len(older),
            "change_6m": _median(recent) / _median(older) - 1 if comparable else None,
            "jeonse": _median(recent_jeonse), "jeonse_basis": len(recent_jeonse),
            "jeonse_range": (min(recent_jeonse), max(recent_jeonse)) if recent_jeonse else None,
            "monthly": _monthly_medians(trades),
        }
    return result


def _median(values: list[Decimal]) -> Decimal | None:
    return None if not values else Decimal(statistics.median(values))


def _monthly_medians(trades: list[tuple[date, Decimal]]) -> list[tuple[str, Decimal, int]]:
    by_month: dict[str, list[Decimal]] = {}
    for day, price in trades:
        by_month.setdefault(f"{day:%Y-%m}", []).append(price)
    return [(month, Decimal(statistics.median(prices)), len(prices)) for month, prices in sorted(by_month.items())]

