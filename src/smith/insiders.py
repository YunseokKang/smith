"""Executives' and major holders' dealings in the client's holdings, as a weak signal for the markets report.

Read by code from the filings (verified live 2026-10-30), totals only (no person's name leaves this module):

- KR (OpenDART elestock, 임원ㆍ주요주주 소유보고): each report's change in shares (sp_stock_lmp_irds_cnt), with the
  reporter's role and whether a major holder. A report does not say why the count changed (an on-market trade, a
  share award, an inheritance), so the KR figure is "ownership change", not buying or selling.
- US (SEC Form 4 XML): non-derivative transactions by code: P (open-market purchase) and S (open-market sale) only;
  awards (A), tax withholding (F), option exercises (M) and gifts (G) are not trading. A sale under a 10b5-1 plan
  (set in advance, aff10b5One) says less about the insider's view and is counted apart.

Window: WINDOW_DAYS. Stored as ledger evidence (provider "insiders", weekly) so a report reads what was known then.
"""
import json
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from smith import dart, holdings, ledger, sec

PROVIDER = "insiders"
ATTEMPT = "sync:attempt"
WINDOW_DAYS = 90
MAX_FORMS = 40            # Form 4 filings read per company and sync (newest first).
STALE = timedelta(days=6)
RETRY = timedelta(days=1)


def stale(db: Path, now: datetime) -> bool:
    with closing(ledger.connect_read_only(db)) as conn:
        row = conn.execute("SELECT retrieved_at, value FROM evidence_points WHERE provider = ? AND series_id = ? "
                           "ORDER BY retrieved_at DESC LIMIT 1", (PROVIDER, ATTEMPT)).fetchone()
    if row is None:
        return True
    return now - datetime.fromisoformat(row[0]) > (RETRY if json.loads(row[1]).get("failures") else STALE)


def sync(db: Path, view: Any, *, now: datetime, today: date, dart_key: str | None, contact: str | None,
         fetch: dart.Fetcher = dart.default_fetcher, sec_fetch: sec.Fetcher = sec.default_fetcher) -> dict[str, Any]:
    """Read and store each held company's dealings over the window; never raises. Returns {"stored", "failures"}."""
    points, failures, done = [], [], {}
    since = today - timedelta(days=WINDOW_DAYS)
    for p in holdings.review(view)["positions"]:
        if p["hermes"]:
            continue
        try:
            if p["market"] == "KR" and dart_key and dart._CODE.fullmatch(p["symbol"]):
                issuer = p["symbol"][:5]
                if issuer not in done:
                    corp = dart.corp_code(p["symbol"], dart_key, dart.cache_path(db), now=now, fetch=fetch)
                    done[issuer] = None if corp is None else kr_summary(corp, key=dart_key, since=since, fetch=fetch)
                found = done[issuer]
            elif p["market"] == "US" and contact:
                company = sec.cik(p["symbol"], contact, sec.cache_path(db), now=now, fetch=sec_fetch)
                found = None if company is None else us_summary(company, contact=contact, since=since, fetch=sec_fetch)
            else:
                continue
        except (dart.Error, sec.Error) as error:
            failures.append(f"내부자 거래 조회 실패({error.code})")   # Never the holding: the run log gets it.
            continue
        if found is not None:
            points.append((PROVIDER, f"{p['market']}:{p['symbol']}", today, json.dumps(found), None))
    attempt = (PROVIDER, ATTEMPT, today, json.dumps({"at": now.isoformat(), "failures": len(failures)}), None)
    with closing(ledger.connect(db)) as conn:
        stored = ledger.store_evidence(conn, points + [attempt], retrieved_at=now) - 1
    return {"stored": stored, "failures": failures}


def kr_summary(corp: str, *, key: str, since: date, fetch: dart.Fetcher = dart.default_fetcher) -> dict[str, Any]:
    """Ownership changes reported since `since`: reporters up and down, net shares, the largest change by role."""
    raw = dart._get(fetch, "elestock.json", key, corp_code=corp)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise dart.Error("bad-response") from None
    if not isinstance(body, dict) or str(body.get("status")) not in ("000", "013"):
        raise dart.Error(f"status-{body.get('status') if isinstance(body, dict) else 'bad'}")
    up = down = 0
    net = Decimal(0)
    largest: dict[str, Any] | None = None
    for row in body.get("list") or []:
        if not isinstance(row, dict) or str(row.get("rcept_dt", "")) < since.isoformat():
            continue
        change = _number(row.get("sp_stock_lmp_irds_cnt"))
        if not change:
            continue
        up, down, net = up + (change > 0), down + (change < 0), net + change
        if largest is None or abs(change) > abs(Decimal(largest["change"])):
            holder = str(row.get("isu_main_shrholdr") or "-")
            largest = {"role": str(row.get("isu_exctv_ofcps") or "-") + ("" if holder == "-" else f"({holder})"),
                       "change": str(change)}
    return {"basis": "kr-ownership", "window_days": WINDOW_DAYS, "reporters_up": up, "reporters_down": down,
            "net_shares": str(net), "largest": largest}


def us_summary(company: int, *, contact: str, since: date, fetch: sec.Fetcher = sec.default_fetcher) -> dict[str, Any]:
    """Open-market purchases and sales in the Form 4 filings since `since` (the newest MAX_FORMS)."""
    forms = [f for f in sec.filings(company, contact, since=since, fetch=fetch) if f.form == "4"][:MAX_FORMS]
    totals = {"buys": 0, "buy_shares": Decimal(0), "buy_value": Decimal(0), "sells": 0, "sell_shares": Decimal(0),
              "sell_value": Decimal(0), "planned_sells": 0, "unpriced": 0}
    unread = 0
    for f in forms:
        try:
            body = fetch(raw_xml(f.url), contact)
            _add(totals, ET.fromstring(body))
        except (sec.Error, ET.ParseError):
            unread += 1
    return {"basis": "us-form4", "window_days": WINDOW_DAYS, "forms": len(forms), "unread": unread,
            **{k: str(v) for k, v in totals.items()}}


def raw_xml(url: str) -> str:
    """The submissions list names the styled view (".../xslF345X06/form4.xml"); the raw XML has the same name in the
    filing's folder. Any other path is the raw document already."""
    folder, styled, name = url.rsplit("/", 2)
    return f"{folder}/{name}" if styled.startswith("xsl") else url


def _add(totals: dict[str, Any], root: ET.Element) -> None:
    planned = (root.findtext(".//aff10b5One") or "").strip().lower() in ("1", "true")
    for t in root.iter("nonDerivativeTransaction"):
        code = (t.findtext("transactionCoding/transactionCode") or "").strip()
        if code not in ("P", "S"):
            continue
        shares = _number(t.findtext("transactionAmounts/transactionShares/value"))
        price = _number(t.findtext("transactionAmounts/transactionPricePerShare/value"))
        side = "buy" if code == "P" else "sell"
        totals[f"{side}s"] += 1
        totals["planned_sells"] += planned and code == "S"
        if shares is None or price is None:
            totals["unpriced"] += 1     # Counted, never valued at zero (review 2026-10-30: a footnoted price).
            continue
        totals[f"{side}_shares"] += shares
        totals[f"{side}_value"] += shares * price


def _number(text: Any) -> Decimal | None:
    try:
        value = Decimal(str(text or "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def load(conn: Any, known_at: datetime) -> dict[str, dict[str, Any]]:
    """{series: the newest summary with its date ("as_of")} as known at `known_at`."""
    found = {}
    for (provider, series), points in ledger.load_evidence(conn, known_at=known_at).items():
        if provider == PROVIDER and series != ATTEMPT and points:
            day = max(points)
            found[series] = {**json.loads(points[day][0]), "as_of": day.isoformat()}
    return found


def for_report(found: dict[str, Any] | None, today: date) -> dict[str, Any] | None:
    """The summary the markets report reads (US values in whole dollars), or None without one or when it was read more
    than a month ago (a weekly sync that keeps failing must not pass an old picture as this quarter's)."""
    if not found or today - date.fromisoformat(found["as_of"]) > timedelta(days=30):
        return None
    if found["basis"] == "us-form4":
        return {**found, "buy_value": str(round(Decimal(found["buy_value"]))),
                "sell_value": str(round(Decimal(found["sell_value"])))}
    return dict(found)
