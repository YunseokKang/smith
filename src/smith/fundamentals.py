"""Official quarterly figures of the client's listed holdings, for the markets report's opinions (2026-10-30).

The holding opinions leaned on figures quoted by news found in research. This module reads them from the filings
themselves and keeps them as ledger evidence (provider "fundamentals", bitemporal like every evidence point):

- KR (OpenDART fnlttSinglAcnt, checked live 2026-10-30): the newest periodic report of the year (3Q, half, 1Q, else
  last year's annual report), consolidated (CFS) else separate (OFS). Each income line carries this period and the
  same period a year earlier (thstrm/frmtrm); the balance sheet the period end.
- US (SEC companyfacts XBRL): the newest quarter (a duration of 80 to 100 days) of revenue, operating income and net
  income with the same quarter a year earlier, and liabilities and equity at that quarter's end; us-gaap, else IFRS
  (a foreign issuer), where only full years may exist.

What the report gets is code's: revenue, operating income and net income with their change on a year earlier, the
operating margin and liabilities / equity, each with its period, source and filing date. ETFs have no such figures.
"""
import json
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from smith import dart, holdings, ledger, sec

PROVIDER = "fundamentals"
ATTEMPT = "sync:attempt"
STALE = timedelta(days=6)
RETRY = timedelta(days=1)
MAX_AGE = timedelta(days=400)        # Figures for a period that ended longer ago are not shown as current.
_REPORTS = (("11014", "3Q"), ("11012", "2Q"), ("11013", "1Q"))   # Newest first within a year; 11011 = annual.
_KR_LINES = {"revenue": ("매출액", "수익(매출액)", "영업수익", "매출"), "operating_income": ("영업이익", "영업이익(손실)"),
             "net_income": ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익"),
             "liabilities": ("부채총계",), "equity": ("자본총계",)}
_US_CONCEPTS = {
    "us-gaap": {"revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
                "operating_income": ("OperatingIncomeLoss",), "net_income": ("NetIncomeLoss",),
                "liabilities": ("Liabilities",),
                "equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")},
    "ifrs-full": {"revenue": ("Revenue",), "operating_income": ("ProfitLossFromOperatingActivities",),
                  "net_income": ("ProfitLoss",), "liabilities": ("Liabilities",), "equity": ("Equity",)},
}
COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


# --- sync ---------------------------------------------------------------------------------------------------------

def stale(db: Path, now: datetime) -> bool:
    """A sync is due: none yet, the last is older than STALE, or it had failures and is older than RETRY."""
    with closing(ledger.connect_read_only(db)) as conn:
        row = conn.execute("SELECT retrieved_at, value FROM evidence_points WHERE provider = ? AND series_id = ? "
                           "ORDER BY retrieved_at DESC LIMIT 1", (PROVIDER, ATTEMPT)).fetchone()
    if row is None:
        return True
    return now - datetime.fromisoformat(row[0]) > (RETRY if json.loads(row[1]).get("failures") else STALE)


def sync(db: Path, view: Any, *, now: datetime, today: date, dart_key: str | None, contact: str | None,
         fetch: dart.Fetcher = dart.default_fetcher, sec_fetch: sec.Fetcher = sec.default_fetcher) -> dict[str, Any]:
    """Read and store the holdings' latest official figures; never raises. Returns {"stored", "failures"}."""
    points, failures = [], []
    mine = [p for p in holdings.review(view)["positions"] if not p["hermes"]]
    for p in mine:
        try:
            if p["market"] == "KR" and dart_key and dart._CODE.fullmatch(p["symbol"]):
                found = kr_figures(p["symbol"], key=dart_key, today=today, now=now, cache=dart.cache_path(db), fetch=fetch)
            elif p["market"] == "US" and contact:
                found = us_figures(p["symbol"], contact=contact, now=now, cache=sec.cache_path(db), fetch=sec_fetch)
            else:
                continue
        except (dart.Error, sec.Error) as error:
            failures.append(f"실적 조회 실패({error.code})")   # Never the holding: the run log gets it.
            continue
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:   # A shape we did not expect: said, not raised.
            failures.append(f"실적 형식 오류({type(error).__name__})")
            continue
        if found is not None:
            points.append((PROVIDER, f"{p['market']}:{p['symbol']}", date.fromisoformat(found["period_end"]),
                           json.dumps(found, ensure_ascii=False), date.fromisoformat(found["filed"]) if found["filed"] else None))
    attempt = (PROVIDER, ATTEMPT, today, json.dumps({"at": now.isoformat(), "failures": len(failures)}), None)
    with closing(ledger.connect(db)) as conn:
        stored = ledger.store_evidence(conn, points + [attempt], retrieved_at=now) - 1
    return {"stored": stored, "failures": failures}


def kr_figures(symbol: str, *, key: str, today: date, now: datetime, cache: Path,
               fetch: dart.Fetcher = dart.default_fetcher) -> dict[str, Any] | None:
    """The newest periodic report's figures of the issuer, or None when it has filed none in two years."""
    corp = dart.corp_code(symbol, key, cache, now=now, fetch=fetch)
    if corp is None:
        return None
    tries = [(today.year, code, label) for code, label in _REPORTS] + [(today.year - 1, "11011", "FY")] + \
            [(today.year - 1, code, label) for code, label in _REPORTS]
    for year, code, label in tries:
        rows = _dart_rows(corp, key, year, code, fetch)
        if rows:
            return _kr_read(rows, year, label)
    return None


def _dart_rows(corp: str, key: str, year: int, code: str, fetch: dart.Fetcher) -> list[dict[str, Any]]:
    raw = dart._get(fetch, "fnlttSinglAcnt.json", key, corp_code=corp, bsns_year=str(year), reprt_code=code)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise dart.Error("bad-response") from None
    if not isinstance(body, dict):
        raise dart.Error("bad-response")
    if str(body.get("status")) == "013":
        return []
    if str(body.get("status")) != "000":
        raise dart.Error(f"status-{body.get('status')}")
    rows = [r for r in body.get("list") or [] if isinstance(r, dict)]
    consolidated = [r for r in rows if r.get("fs_div") == "CFS"]
    return consolidated or [r for r in rows if r.get("fs_div") == "OFS"]


def _kr_read(rows: list[dict[str, Any]], year: int, label: str) -> dict[str, Any]:
    def line(names: tuple[str, ...], field: str) -> str | None:
        row = next((r for name in names for r in rows if (r.get("account_nm") or "").strip() == name), None)
        value = None if row is None else _number(row.get(field))
        return None if value is None else str(value)
    end = {"1Q": f"{year}-03-31", "2Q": f"{year}-06-30", "3Q": f"{year}-09-30", "FY": f"{year}-12-31"}[label]
    receipt = next((str(r.get("rcept_no")) for r in rows if r.get("rcept_no")), "")
    figures = {key: line(names, "thstrm_amount") for key, names in _KR_LINES.items()}
    figures |= {f"{key}_prior": line(names, "frmtrm_amount") for key, names in _KR_LINES.items()
                if key in ("revenue", "operating_income", "net_income")}
    start = {"1Q": f"{year}-01-01", "2Q": f"{year}-04-01", "3Q": f"{year}-07-01", "FY": f"{year}-01-01"}[label]
    return {**figures, "period": period_label(start, end), "period_end": end, "currency": "KRW", "basis": "DART",
            "filed": f"{receipt[:4]}-{receipt[4:6]}-{receipt[6:8]}" if len(receipt) >= 8 else "",
            "source": dart.VIEWER + receipt if receipt else ""}


def us_figures(ticker: str, *, contact: str, now: datetime, cache: Path,
               fetch: sec.Fetcher = sec.default_fetcher) -> dict[str, Any] | None:
    """The newest quarter (else full year) of a US company's XBRL facts, or None (a fund, or no facts)."""
    company = sec.cik(ticker, contact, cache, now=now, fetch=fetch)
    if company is None:
        return None
    body = sec._json(fetch, COMPANYFACTS.format(cik=company), contact)
    facts = body.get("facts") if isinstance(body, dict) else None
    if not isinstance(facts, dict):
        raise sec.Error("bad-response")
    for taxonomy, concepts in _US_CONCEPTS.items():
        if taxonomy in facts:
            found = _us_read(facts[taxonomy], concepts)
            if found is not None:
                return {**found, "basis": "SEC",
                        "source": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={company}"}
    return None


def _us_read(taxonomy: dict[str, Any], concepts: dict[str, tuple[str, ...]]) -> dict[str, Any] | None:
    """The newest period of revenue (a quarter, else a year), the other lines for the same period and a year before."""
    def facts(names: tuple[str, ...], currency: str | None = None) -> tuple[list[dict[str, Any]], str]:
        """The candidate tag with the newest data: a company may move from one tag to another over the years
        (review of real data 2026-10-30: the first tag present held only old periods)."""
        found: list[tuple[str, list[dict[str, Any]], str]] = []
        for name in names:
            units = ((taxonomy.get(name) or {}).get("units") or {})
            # The revenue's currency for every other line (review 2026-10-30: lines in two currencies made a margin).
            unit = (currency if currency in units else None) if currency else \
                next((u for u in units if len(u) == 3 and u.isupper()), None)   # USD, EUR, ...
            rows = [] if unit is None else [f for f in units[unit] if isinstance(f, dict) and f.get("end")]
            if rows:
                found.append((max(r["end"] for r in rows), rows, unit))
        if not found:
            return [], ""
        _, rows, unit = max(found, key=lambda item: item[0])
        return rows, unit
    revenue, currency = facts(concepts["revenue"])
    for span in ((80, 100), (350, 380)):
        period = _newest(revenue, span)
        if period is not None:
            break
    else:
        return None
    start, end = period["start"], period["end"]
    found = {"period_end": end, "currency": currency, "filed": period.get("filed", ""), "period": period_label(start, end)}
    for key in ("revenue", "operating_income", "net_income"):
        rows, _ = facts(concepts[key], currency)
        found[key] = _value(rows, start, end)
        prior_end = (date.fromisoformat(end) - timedelta(days=365)).isoformat()
        found[f"{key}_prior"] = _value(rows, None, prior_end, duration=_days(start, end))
    for key in ("liabilities", "equity"):
        rows, _ = facts(concepts[key], currency)
        found[key] = _value(rows, None, end, instant=True)
    return found


def period_label(start: str, end: str) -> str:
    """A period in calendar months, whatever the company's fiscal year (ARM's first quarter is April to June):
    "2026년 4~6월", or "2025년" for a calendar year."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last.day < 15:   # A fiscal period ending early in a month (NVIDIA's 26 July is July; 2 August is July).
        last = last.replace(day=1) - timedelta(days=1)
    if first.day > 15:
        first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    if (first.month, last.month) == (1, 12) and first.year == last.year:
        return f"{last.year}년"
    return f"{first.year}년 {first.month}~{last.month}월" if first.year == last.year else         f"{first.year}년 {first.month}월~{last.year}년 {last.month}월"


def _newest(rows: list[dict[str, Any]], span: tuple[int, int]) -> dict[str, Any] | None:
    timed = [r for r in rows if r.get("start") and span[0] <= _days(r["start"], r["end"]) <= span[1]]
    return max(timed, key=lambda r: (r["end"], r.get("filed", ""))) if timed else None


def _value(rows: list[dict[str, Any]], start: str | None, end: str, *, duration: int | None = None,
           instant: bool = False) -> str | None:
    """The value for the period (exact start and end), or a period of about `duration` days ending within 15 days of
    `end`, or the instant at `end`; the latest filing's figure (an amended one wins)."""
    def fits(r: dict[str, Any]) -> bool:
        if instant:
            return not r.get("start") and r["end"] == end
        if start is not None:
            return r.get("start") == start and r["end"] == end
        return (bool(r.get("start")) and abs((date.fromisoformat(r["end"]) - date.fromisoformat(end)).days) <= 15
                and abs(_days(r["start"], r["end"]) - (duration or 0)) <= 15)
    hits = sorted((r for r in rows if fits(r)), key=lambda r: r.get("filed", ""))
    return None if not hits else str(Decimal(str(hits[-1]["val"])))


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _number(text: Any) -> Decimal | None:
    try:
        value = Decimal(str(text or "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


# --- what the report reads ---------------------------------------------------------------------------------------

def load(conn: Any, known_at: datetime) -> dict[str, dict[str, Any]]:
    """{series ("KR:005930", "US:NVDA"): the newest period's figures} as known at `known_at`."""
    found = {}
    for (provider, series), points in ledger.load_evidence(conn, known_at=known_at).items():
        if provider == PROVIDER and series != ATTEMPT and points:
            found[series] = json.loads(points[max(points)][0])
    return found


def summary(figures: dict[str, Any] | None, today: date) -> dict[str, Any] | None:
    """The report's view of one holding's figures, all computed here: the amounts as a reader writes them, the change
    on a year earlier, the operating margin and liabilities / equity (fractions). None without figures or when the
    period ended more than MAX_AGE ago."""
    if not figures or today - date.fromisoformat(figures["period_end"]) > MAX_AGE:
        return None
    get = lambda key: _number(figures.get(key))   # noqa: E731 - local shorthand
    revenue, operating, net = get("revenue"), get("operating_income"), get("net_income")

    def change(key: str) -> str | None:
        now, before = get(key), get(f"{key}_prior")
        return None if now is None or not before or before < 0 else f"{(now / before - 1):.4f}"
    liabilities, equity = get("liabilities"), get("equity")
    unit = figures["currency"]
    return {"period": figures["period"], "period_end": figures["period_end"], "source": figures["basis"],
            "filed": figures.get("filed"), "currency": unit,
            "revenue": readable(revenue, unit), "operating_income": readable(operating, unit), "net_income": readable(net, unit),
            "revenue_yoy": change("revenue"), "operating_income_yoy": change("operating_income"),
            "operating_margin": _margin(operating, revenue),
            "debt_ratio": None if liabilities is None or not equity or equity < 0 else f"{liabilities / equity:.4f}"}


def _margin(operating: Decimal | None, revenue: Decimal | None) -> str | None:
    """Operating income / revenue, or None when revenue is too small for the ratio to mean anything (beyond ±1000%)."""
    if operating is None or not revenue or revenue < 0 or abs(operating / revenue) > 10:
        return None
    return f"{operating / revenue:.4f}"


def readable(amount: Decimal | None, currency: str) -> str | None:
    """A large amount as a reader writes it: 171.5조 원, 962억 달러, -8,000억 원."""
    if amount is None:
        return None
    unit = "원" if currency == "KRW" else "달러" if currency == "USD" else currency
    value, sign = abs(amount), "-" if amount < 0 else ""
    if value >= 10 ** 12:
        text = f"{value / 10 ** 12:,.1f}조"
    elif value >= 10 ** 8:
        text = f"{value / 10 ** 8:,.0f}억"
    elif value >= 10 ** 4:
        text = f"{value / 10 ** 4:,.0f}만"
    else:
        text = f"{value:,.0f}"
    return f"{sign}{text.replace('.0조', '조')} {unit}"
