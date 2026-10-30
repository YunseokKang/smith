"""Material filings of the client's KR and US holdings as an event note (FR-44, report design §29).

At each event check (smith.events) the client's own KR listed holdings are looked up in OpenDART (smith.dart): the
filings of the last SCAN_DAYS days that no earlier note carried and whose title names a material matter
(dart.CATEGORIES: a dividend, a dilution, a buyback, earnings, a risk, a corporate change). One note carries all of
them. Everything is code: no model reads or writes a filing.

- A dividend decision is read from the form (dart.latest_dividend's parser) and turned into what this household
  receives, per owner and account, with qa_lookup.estimates (shares on the record date, 14% + 10% local withholding in
  a general account).
- A paid-in capital increase (유상증자) gives the new common shares and the shares before: code computes the dilution,
  new / (before + new). A convertible bond gives the shares on conversion and the filing's own ratio to the shares
  outstanding. A buyback gives the shares and the amount. Other filings go out with their title and link.
- US-listed holdings are looked up in SEC EDGAR (smith.sec, user request 2026-10-29: "미국쪽 공시정보도 중요해"): an
  8-K by its item codes (results, a material agreement, a dilutive issue, an impairment, a delisting notice, ...), a
  foreign issuer's 6-K and a 13D. Their text is English and not read: the note names what the items mean and links the
  filing. ETFs are funds, not SEC company filers, and are not looked up.
- Hermes-managed holdings are left out (Smith gives no opinion on them). Only codes, names and dates travel to DART and
  the SEC (with the contact address the SEC requires in the request header).
"""
import hashlib
import re
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smith import dart, holdings, ledger, qa_lookup, sec

SCAN_DAYS = 3            # A weekend and a missed check: older filings belong to the regular report.
SEEN_NOTES = 200         # Notes looked back at for filings already carried.
MAX_FILINGS = 12         # One note, a dozen lines at most.


def held_companies(view: Any) -> dict[str, list[dict[str, Any]]]:
    """The client's own KR listed holdings by issuer key (the first five digits of the code: common and preferred
    shares of one company are one issuer)."""
    found: dict[str, list[dict[str, Any]]] = {}
    for p in holdings.review(view)["positions"]:
        if not p["hermes"] and p["market"] == "KR" and dart._CODE.fullmatch(p["symbol"]):
            found.setdefault(p["symbol"][:5], []).append(p)
    return found


def seen(conn: Any) -> set[str]:
    """Filings (receipt numbers) an earlier disclosure note carried or is still trying to carry."""
    return {f["rcept_no"] for row in ledger.event_notes(conn, limit=SEEN_NOTES) if row["kind"] == "disclosure"
            for f in (row["details"] or {}).get("filings", [])}


def scan(view: Any, *, key: str, today: date, now: datetime, cache: Path, already: set[str],
         fetch: dart.Fetcher = dart.default_fetcher, days: int = SCAN_DAYS) -> tuple[list[dict[str, Any]], list[str]]:
    """(new material filings of held companies, newest first, as plain data; failures). A failure says what failed,
    never which holding (the failures reach the run log); the company is looked at again at the next check."""
    filings, failures = [], []
    for issuer, positions in sorted(held_companies(view).items()):
        name = positions[0]["public_name"]
        try:
            corp = dart.corp_code(positions[0]["symbol"], key, cache, now=now, fetch=fetch)
            if corp is None:
                failures.append("전자공시에서 회사를 찾지 못함")
                continue
            listed = dart.filings(corp, key, since=today - timedelta(days=days), until=today, fetch=fetch)
        except dart.Error as error:
            failures.append(f"전자공시 조회 실패({error.code})")
            continue
        for f in listed:
            kind = dart.category(f.title)
            if kind is not None and f.rcept_no not in already:
                filings.append({"rcept_no": f.rcept_no, "filed_on": f.filed_on.isoformat(), "title": f.title,
                                "corp_name": f.corp_name or name, "corp_code": corp, "category": kind,
                                "symbols": [p["symbol"] for p in positions]})
    return newest_first(filings)[:MAX_FILINGS], failures


def slot(filings: list[dict[str, Any]]) -> str:
    """The note's key slot: a digest of the newest filing's receipt number. The key is written to the run log, and a
    receipt number names the company's filing (review 2026-10-30)."""
    return hashlib.sha256(filings[0]["rcept_no"].encode("utf-8")).hexdigest()[:12]


def newest_first(filings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(filings, key=lambda f: (f["filed_on"], f["rcept_no"]), reverse=True)


def held_us(view: Any) -> dict[str, dict[str, Any]]:
    """The client's own US-listed holdings by ticker."""
    return {p["symbol"]: p for p in holdings.review(view)["positions"] if not p["hermes"] and p["market"] == "US"}


def scan_us(view: Any, *, contact: str, today: date, now: datetime, cache: Path, already: set[str],
            fetch: sec.Fetcher = sec.default_fetcher, days: int = SCAN_DAYS) -> tuple[list[dict[str, Any]], list[str]]:
    """`scan` for the US holdings in SEC EDGAR: material 8-K items, 6-K and 13D, by form and item code."""
    filings, failures = [], []
    companies: dict[int, list[str]] = {}    # One lookup per company: GOOG and GOOGL share a CIK (review 2026-10-29).
    for ticker in sorted(held_us(view)):
        try:
            company = sec.cik(ticker, contact, cache, now=now, fetch=fetch)
        except sec.Error as error:
            failures.append(f"SEC 조회 실패({error.code})")
            continue
        if company is not None:             # A fund (VOO, SCHD, ...) is not a company filer.
            companies.setdefault(company, []).append(ticker)
    held = held_us(view)
    for company, tickers in sorted(companies.items()):
        p = held[tickers[0]]
        try:
            listed = sec.filings(company, contact, since=today - timedelta(days=days), fetch=fetch)
        except sec.Error as error:
            failures.append(f"SEC 조회 실패({error.code})")
            continue
        for f in listed:
            found = sec.meaning(f)
            if found is not None and f.accession not in already:
                kind, labels = found
                filings.append({"rcept_no": f.accession, "filed_on": f.filed_on.isoformat(),
                                "title": f"{f.form} · {' · '.join(labels)}", "corp_name": p["public_name"] or ticker,
                                "corp_code": f"CIK{company}", "category": kind, "symbols": tickers, "source": "sec",
                                "url": f.url})
    return newest_first(filings)[:MAX_FILINGS], failures


def enrich(db: Path, view: Any, as_of: datetime, filings: list[dict[str, Any]], *, key: str, known_at: datetime,
           fetch: dart.Fetcher = dart.default_fetcher) -> list[dict[str, Any]]:
    """Each filing with its figures read by code ("figures") and what it means for this household ("household"), or
    "figures_failed" with the reason when the filing could not be read (the title and link still go out)."""
    # The client's weight in each company (share of the securities Smith opines on): code's, never the model's input.
    weights = {p["symbol"]: p["share"] for p in holdings.review(view)["positions"] if not p["hermes"]}
    read = []
    for raw in filings:
        held = [weights.get(s) for s in raw.get("symbols", [])]
        share = None if not held or None in held else str(sum(held))
        if raw.get("source") == "sec":     # Named by its items and linked; the English text is not read by code.
            read.append({**raw, "figures": None, "household": None, "figures_failed": None, "held_share": share})
            continue
        f = dart.Filing(raw["rcept_no"], date.fromisoformat(raw["filed_on"]), raw["title"], raw["corp_name"])
        item = {**raw, "url": f.url, "figures": None, "household": None, "figures_failed": None, "held_share": share}
        try:
            if raw["category"] == "dividend":
                item.update(_dividend(db, view, as_of, f, raw["symbols"], key=key, known_at=known_at, fetch=fetch))
            else:
                figures = dart.structured(f, raw["corp_code"], key, fetch=fetch)
                if figures is not None:
                    item.update(_structured(figures))
        except dart.Error as error:
            item["figures_failed"] = error.code
        read.append(item)
    return _supersede(read)


_CORRECTION = re.compile(r"^\[(?:기재|첨부|발행조건)?정정\]\s*")


def _supersede(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A correction ("[기재정정]…") replaces the nearest earlier filing of the same company and title in the note
    (review of a real note 2026-10-29: a dividend and its correction were both counted). The replaced one is marked
    `superseded` and loses its household figures, so no total counts it twice. Only the same matter (`_same_matter`):
    a correction of an earlier quarter's dividend must not remove this quarter's (review 2026-10-29)."""
    by_age = sorted(items, key=lambda i: i["rcept_no"])
    for n, item in enumerate(by_age):
        if not _CORRECTION.match(item["title"]):
            continue
        base = _CORRECTION.sub("", item["title"])
        earlier = next((o for o in reversed(by_age[:n]) if o["corp_code"] == item["corp_code"]
                        and _CORRECTION.sub("", o["title"]) == base and not o.get("superseded")
                        and _same_matter(o, item)), None)
        if earlier is not None:
            earlier.update(superseded=item["filed_on"], household=None)
    return items


CORRECTION_WINDOW = timedelta(days=7)


DATE_FIX = timedelta(days=31)   # A corrected record date moves days, not a quarter.


def _same_matter(original: dict[str, Any], correction: dict[str, Any]) -> bool:
    """Whether a correction is of `original`. Any filing: filed within CORRECTION_WINDOW before the correction. A
    dividend also: the record dates are equal, or close enough to be a corrected date (DATE_FIX), or the original had
    none yet (review 2026-10-30: 9/30 corrected to 10/1 was counted twice); a quarter apart is another dividend."""
    gap = date.fromisoformat(correction["filed_on"]) - date.fromisoformat(original["filed_on"])
    if not timedelta(0) <= gap <= CORRECTION_WINDOW:
        return original["category"] == "dividend" and _record(original) is not None and _record(original) == _record(correction)
    if original["category"] != "dividend":
        return True
    before, after = _record(original), _record(correction)
    return before is None or (after is not None and abs(before - after) <= DATE_FIX)


def _record(item: dict[str, Any]) -> date | None:
    text = (item.get("figures") or {}).get("record_date")
    return None if not text else date.fromisoformat(text)


def owned(conn: Any, key: str, filings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The filings this note may carry: those no note claimed earlier also holds (review 2026-10-29: two overlapping
    checks, a manual one beside the scheduled one, bundled the same filing under different keys and both sent it).
    The owner is the note claimed first, by the order its row was inserted (a run that started earlier but claimed
    later does not win, review 2026-10-30), skipped notes aside, so exactly one note keeps each filing."""
    owners: dict[str, str] = {}
    for event_key, status, details in ledger.disclosure_claims(conn, limit=SEEN_NOTES):
        if status == "skipped":
            continue
        for f in (details or {}).get("filings", []):
            owners.setdefault(f["rcept_no"], event_key)
    return [f for f in filings if owners.get(f["rcept_no"], key) == key]


def _dividend(db: Path, view: Any, as_of: datetime, filing: dart.Filing, symbols: list[str], *, key: str,
              known_at: datetime, fetch: dart.Fetcher) -> dict[str, Any]:
    decision = dart.parse_dividend(dart.document(filing, key, fetch=fetch), filing)
    figures = {"kind": decision.kind, "cash": decision.cash,
               "per_share": {k: str(v) for k, v in decision.per_share.items()},
               "record_date": None if decision.record_date is None else decision.record_date.isoformat(),
               "pay_date": None if decision.pay_date is None else decision.pay_date.isoformat()}
    if not decision.cash:
        return {"figures": figures, "household": None}
    declared, unproven = [], []
    for symbol in symbols:
        amount = decision.per_share.get("common" if symbol.endswith("0") else "preferred")
        if amount is None:
            unproven.append(symbol)
            continue
        declared.append({"symbol": symbol, "per_share": amount, "currency": "KRW", "kind": decision.kind[:12],
                         "record_date": decision.record_date, "pay_date": decision.pay_date, "ref": filing.rcept_no,
                         "checked": True})
    found = qa_lookup.estimates(db, {"view": view, "as_of": as_of}, declared, known_at=known_at, unproven=unproven)
    return {"figures": figures, "household": found}


def _structured(figures: dict[str, str]) -> dict[str, Any]:
    """The figures that matter from a structured filing, with the dilution computed by code where the filing gives the
    share counts: the client's stake in the company shrinks by that share (what it does to the price is not code's)."""
    api = figures["api"]
    if api == "piicDecsn.json":
        new, before = dart.number(figures.get("nstk_ostk_cnt")), dart.number(figures.get("bfic_tisstk_ostk"))
        ratio = new / (before + new) if new and before else None
        return {"figures": {"type": "유상증자", "new_shares": figures.get("nstk_ostk_cnt"),
                            "shares_before": figures.get("bfic_tisstk_ostk"), "method": figures.get("ic_mthn"),
                            "dilution": None if ratio is None else str(ratio)}, "household": None}
    if api == "cvbdIsDecsn.json":
        ratio = dart.number(figures.get("cvisstk_tisstk_vs"))
        return {"figures": {"type": "전환사채", "amount": figures.get("bd_fta"), "conversion_price": figures.get("cv_prc"),
                            "shares_on_conversion": figures.get("cvisstk_cnt"),
                            "ratio_to_shares": None if ratio is None else str(ratio / 100),
                            "conversion_from": figures.get("cvrqpd_bgd"), "method": figures.get("bdis_mthn")},
                "household": None}
    return {"figures": {"type": "자기주식 취득", "shares": figures.get("aqpln_stk_ostk"),
                        "amount": figures.get("aqpln_prc_ostk"), "from": figures.get("aqexpd_bgd"),
                        "until": figures.get("aqexpd_edd"), "method": figures.get("aq_mth")},
            "household": None}


def scan_due(db: Path, view: Any, *, key: str | None, today: date, now: datetime,
             fetch: dart.Fetcher = dart.default_fetcher, contact: str | None = None,
             sec_fetch: sec.Fetcher = sec.default_fetcher) -> tuple[list[dict[str, Any]], list[str]]:
    """The KR (with a DART key) and US (with an SEC contact) filings not carried yet, newest first, and failures."""
    if not key and not contact:
        return [], []
    with closing(ledger.connect_read_only(db)) as conn:
        already = seen(conn)
    found: list[dict[str, Any]] = []
    failures: list[str] = []
    # Each source on its own: one failing never throws away what the other found (review 2026-10-29).
    for wanted, label, run in ((key, "전자공시", lambda: scan(view, key=key, today=today, now=now, cache=dart.cache_path(db),
                                                             already=already, fetch=fetch)),
                               (contact, "SEC", lambda: scan_us(view, contact=contact, today=today, now=now,
                                                                cache=sec.cache_path(db), already=already, fetch=sec_fetch))):
        if not wanted:
            continue
        try:
            got, missed = run()
        except Exception as error:  # noqa: BLE001 - the other source's filings still go out; this one is scanned again.
            got, missed = [], [f"{label} 확인 실패({type(error).__name__})"]
        found, failures = found + got, failures + missed
    return newest_first(found)[:MAX_FILINGS], failures
