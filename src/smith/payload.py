"""Sanitized adviser context: an allowlisted, de-identified view of the ledger and evidence.

Record ids are replaced by opaque aliases (A1, L1, C1, G1), free text the user typed into the
ledger (`reason`) is never included, and the final payload and question are scanned for personal
identifiers and stored secrets before anything leaves the process.
"""
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from smith import evidence, ledger
from smith.announcements import Announcement
from smith.records import Kind, RecordInput, Status
from smith.relevance import ExposureResult, exposures
from smith.summary import BASE_CURRENCY, Summary, build_summary

_ALIAS_PREFIX = {Kind.ASSET: "A", Kind.LIABILITY: "L", Kind.CASHFLOW: "C", Kind.GOAL: "G"}
MAX_PAYLOAD_BYTES = 500_000
# Patterns for identifiers that must never reach the model. ISO dates (4-2-2) do not match the
# account pattern because its last group needs four or more digits.
_PII_PATTERNS = {
    "account-number": re.compile(r"(?<![\d-])\d{3,6}-\d{2,6}-\d{4,8}(?![\d-])"),
    "card-number": re.compile(r"(?<!\d)(?:\d{4}[- ]){3}\d{4}(?!\d)"),
    "resident-registration-number": re.compile(r"(?<!\d)\d{6}-[1-8]\d{6}(?!\d)"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone": re.compile(r"(?<!\d)01[016789]-?\d{3,4}-?\d{4}(?!\d)"),
    "labelled-name": re.compile(r"(?:성명|이름|name)\s*[:：=]\s*[^\s,;]{2,}", re.IGNORECASE),
    "labelled-address": re.compile(r"(?:주소|address)\s*[:：=]\s*[^\n,;]{4,}", re.IGNORECASE),
    "korean-address": re.compile(
        r"(?:서울(?:특별시|시)?|부산(?:광역시|시)?|대구(?:광역시|시)?|인천(?:광역시|시)?|"
        r"광주(?:광역시|시)?|대전(?:광역시|시)?|울산(?:광역시|시)?|세종(?:특별자치시|시)?|"
        r"[가-힣]{2,}(?:특별자치도|특별자치시|광역시|도))\s+(?:[가-힣0-9]+시\s+)?"
        r"[가-힣0-9]+(?:구|군)\s+[^\s,;]+(?:로|길|동|읍|면|리)\s*\d+(?:-\d+)?"
    ),
    "english-address": re.compile(
        r"(?<!\w)\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){0,5}"
        r"(?:Street|St|Road|Rd|Avenue|Ave|Boulevard|Blvd|Lane|Ln)\b",
        re.IGNORECASE,
    ),
}


class PayloadRejected(Exception):
    """The payload or question contains something that must not be sent; nothing was sent."""

    def __init__(self, rules: list[str]) -> None:
        super().__init__(", ".join(rules))
        self.rules = rules


@dataclass
class LedgerView:
    """Everything the adviser may use, read from one ledger snapshot."""

    as_of: datetime
    summary: Summary
    records: list[RecordInput]
    rates: dict[str, Decimal]
    exposures: ExposureResult
    evidence: list[dict[str, Any]]
    announcements: list[Announcement]
    aliases: dict[str, str] = field(default_factory=dict)  # record id -> alias; stays local.
    owner_aliases: dict[str, str] = field(default_factory=dict)  # owner id -> role-preserving alias.
    source_aliases: dict[str, str] = field(default_factory=dict)  # source id -> alias for warnings.


def load_view(conn: sqlite3.Connection, *, as_of: datetime, known_at: datetime) -> LedgerView:
    with ledger.snapshot(conn):
        summary = build_summary(conn, as_of=as_of, known_at=known_at)
        records = [s.record for s in ledger.record_states(conn, as_of=as_of, known_at=known_at)
                   if s.record.status is Status.ACTIVE]
        links = exposures(conn, as_of=as_of, known_at=known_at)
        series = evidence.describe(ledger.load_evidence(conn, known_at=known_at), as_of=as_of.date())
        news = ledger.load_announcements(conn, since=as_of - timedelta(days=90), until=as_of, known_at=known_at)
    rates = {BASE_CURRENCY: Decimal(1), **{f["subject"]: f["value"] for f in summary.fx}}
    view = LedgerView(as_of, summary, records, rates, links, series, news)
    view.aliases = _aliases(view)
    view.owner_aliases = _owner_aliases(view)
    sources = {row["source"] for row in summary.freshness} | {row["source"] for row in summary.sync}
    view.source_aliases = {source: f"S{i}" for i, source in enumerate(sorted(sources), 1)}
    return view


def _aliases(view: LedgerView) -> dict[str, str]:
    """Stable within one payload: records are ordered by kind, then by base value descending."""
    counters: dict[Kind, int] = {}
    aliases = {}
    for record in sorted(view.records, key=lambda r: (list(Kind).index(r.kind), -(base_amount(view, r) or 0),
                                                      r.record_id)):
        counters[record.kind] = counters.get(record.kind, 0) + 1
        aliases[record.record_id] = f"{_ALIAS_PREFIX[record.kind]}{counters[record.kind]}"
    return aliases


def _owner_aliases(view: LedgerView) -> dict[str, str]:
    owners = sorted({record.owner_id for record in view.records} | set(view.summary.net_worth_by_owner))
    aliases = {"self": "self"} if "self" in owners else {}
    others = (owner for owner in owners if owner != "self")
    aliases.update({owner: f"household_member_{i}" for i, owner in enumerate(others, 1)})
    return aliases


def owner_alias(view: LedgerView, owner_id: str) -> str:
    """Return the de-identified household role used in outbound data."""
    return view.owner_aliases[owner_id]


def base_amount(view: LedgerView, record: RecordInput) -> Decimal | None:
    """The record's main amount in the base currency, or None without an FX rate."""
    amount_field = {Kind.ASSET: "value", Kind.LIABILITY: "outstanding_principal", Kind.CASHFLOW: "amount",
                    Kind.GOAL: "target_amount"}[record.kind]
    raw, currency = record.fields.get(amount_field), record.fields.get("currency")
    if raw is None or currency not in view.rates:
        return None
    return Decimal(raw) * view.rates[currency]


def won(value: Decimal | None) -> str | None:
    """Whole base-currency units as a string; the model never receives binary floats."""
    return None if value is None else str(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def build_context(view: LedgerView, case_facts: dict[str, Any], *, include_positions: bool) -> dict[str, Any]:
    """Assemble the allowlisted context. Only fields named here are ever sent."""
    summary = view.summary
    warnings = list(dict.fromkeys(redact(view, warning)
                                  for warning in summary.warnings + list(view.exposures.warnings)))
    return {
        "as_of": view.as_of.date().isoformat(),
        "base_currency": BASE_CURRENCY,
        "completeness": {"complete": summary.complete, "incomplete_areas": summary.incomplete_areas,
                         "unconverted": {c: str(a) for c, a in summary.unconverted.items()},
                         "warnings": warnings},
        "balance_sheet": {"total_assets": won(summary.total_assets),
                          "total_liabilities": won(summary.total_liabilities), "net_worth": won(summary.net_worth),
                          "net_worth_by_owner": {owner_alias(view, o): won(a)
                                                 for o, a in summary.net_worth_by_owner.items()},
                          "by_category": _shares(summary.by_category, summary),
                          "by_account_type": _shares(summary.by_account_type, summary),
                          "by_currency": _shares(summary.by_currency, summary),
                          "by_liquidity": _shares(summary.by_liquidity, summary)},
        "assets": [_asset(view, r, include_positions) for r in _sorted(view, Kind.ASSET)],
        "liabilities": [_liability(view, r) for r in _sorted(view, Kind.LIABILITY)],
        "cash_flow": {"monthly_inflow": won(summary.monthly_inflow), "monthly_outflow": won(summary.monthly_outflow),
                      "monthly_net": won(summary.monthly_net), "monthly_internal_transfers": won(summary.monthly_transfers),
                      "monthly_loan_interest_estimate": won(summary.monthly_loan_interest_estimate),
                      "items": [_cashflow(view, r) for r in _sorted(view, Kind.CASHFLOW)]},
        "goals": [_goal(view, goal) for goal in summary.goals],
        "exposures": [{"ref": f"X{i}", "key": redact(view, e.key), "amount": won(e.amount),
                       "share_of_assets": None if e.share_of_assets is None else f"{e.share_of_assets:.4f}",
                       "evidence": [_evidence_ref(s) for s in e.series], "feeds": list(e.feeds), "note": redact(view, e.note)}
                      for i, e in enumerate(view.exposures.exposures, 1)],
        "evidence": [_series(row) for row in view.evidence],
        "announcements": [{"ref": f"N{i}", "feed": a.feed_id, "published_on": a.published_at.date().isoformat(),
                           "title_untrusted": a.title, "link": a.link} for i, a in enumerate(view.announcements, 1)],
        "case": case_facts,
    }


def _sorted(view: LedgerView, kind: Kind) -> list[RecordInput]:
    return sorted((r for r in view.records if r.kind is kind), key=lambda r: view.aliases[r.record_id][1:].zfill(4))


def _shares(groups: dict[str, Decimal], summary: Summary) -> dict[str, dict[str, str | None]]:
    total = summary.total_assets
    return {key: {"amount": won(amount), "share": f"{amount / total:.4f}" if total and summary.complete else None}
            for key, amount in sorted(groups.items(), key=lambda item: -item[1])}


def _asset(view: LedgerView, record: RecordInput, include_positions: bool) -> dict[str, Any]:
    f = record.fields
    item = {"ref": view.aliases[record.record_id], "owner": owner_alias(view, record.owner_id),
            "category": f["category"],
            "account_type": f.get("account_type"), "currency": f["currency"], "value": won(base_amount(view, record)),
            "liquidity": f["liquidity"], "valuation_method": f["valuation_method"],
            "valued_on": record.effective_at.date().isoformat(), "occupancy": f.get("occupancy"),
            "managed_by": f.get("managed_by"), "region": f.get("region")}
    if include_positions and f.get("symbol"):
        item.update(symbol=f["symbol"], market=f.get("market"))
    return item


def _liability(view: LedgerView, record: RecordInput) -> dict[str, Any]:
    f = record.fields
    collateral = f.get("collateral_record_id")
    return {"ref": view.aliases[record.record_id], "owner": owner_alias(view, record.owner_id),
            "category": f["category"],
            "currency": f["currency"], "outstanding": won(base_amount(view, record)), "annual_rate": f["annual_rate"],
            "rate_type": f["rate_type"], "maturity": f.get("maturity"), "repayment_method": f["repayment_method"],
            "credit_limit": f.get("credit_limit"), "collateral_ref": view.aliases.get(collateral) if collateral else None}


def _cashflow(view: LedgerView, record: RecordInput) -> dict[str, Any]:
    f = record.fields
    loan = f.get("liability_record_id")
    return {"ref": view.aliases[record.record_id], "owner": owner_alias(view, record.owner_id),
            "direction": f["direction"], "category": f["category"],
            "amount": won(base_amount(view, record)), "frequency": f["frequency"], "start_date": f["start_date"],
            "end_date": f.get("end_date"), "commitment": f.get("commitment"),
            "liability_ref": view.aliases.get(loan) if loan else None}


def _goal(view: LedgerView, goal: dict[str, Any]) -> dict[str, Any]:
    record = next(record for record in view.records if record.record_id == goal["record_id"])
    return {"ref": view.aliases[record.record_id], "owner": owner_alias(view, record.owner_id),
            "category": goal["category"], "priority": goal["priority"],
            "target_date": goal["target_date"].isoformat(), "months_left": goal["months_left"],
            "target": won(goal["target"])}


def _evidence_ref(key: str) -> str:
    return key.replace("/", "-")


def _series(row: dict[str, Any]) -> dict[str, Any]:
    spec = row["spec"]
    as_text = lambda v: None if v is None else str(v)
    return {"ref": _evidence_ref(f"{spec.provider}:{spec.series_id}"), "label": spec.label, "unit": spec.unit,
            "value": as_text(row["value"]), "observed_on": row["observed_on"].isoformat() if row["observed_on"] else None,
            "change_3m": as_text(row["change_3m"]), "change_12m": as_text(row["change_12m"]),
            "stale": row["stale"], "source": spec.source_url}


def redact(view: LedgerView, text: str) -> str:
    """Replace internal record, owner and source ids in generated or user-supplied text."""
    replacements = {**view.aliases, **view.owner_aliases, **view.source_aliases}
    for private, alias in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        token = rf"(?<![A-Za-z0-9._-]){re.escape(private)}(?![A-Za-z0-9._-])"
        text = re.sub(token, lambda _: alias, text)
    return text


def mask_identifiers(text: str) -> str:
    """Replace anything matching an identifier rule (for example a press contact's phone number in a
    web page) so that external text can later pass `check_outbound`."""
    for pattern in _PII_PATTERNS.values():
        text = pattern.sub("[식별정보 제거]", text)
    return text


def check_outbound(text: str, secrets: list[str]) -> None:
    """Block identifiers and stored secrets. Raises PayloadRejected naming only the rules hit."""
    if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise PayloadRejected(["payload-too-large"])
    rules = [name for name, pattern in _PII_PATTERNS.items() if pattern.search(text)]
    if any(secret and len(secret) >= 8 and secret in text for secret in secrets):
        rules.append("stored-secret")
    if rules:
        raise PayloadRejected(rules)
