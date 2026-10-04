"""Deterministic links between the household's exposures and macro evidence.

This module only measures how much of the household is exposed to each kind of change and which
evidence series and feeds bear on it. Explaining the causal effect is left to the adviser (stage 5).
"""
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from smith import ledger
from smith.records import Kind, Status
from smith.summary import BASE_CURRENCY, build_summary

_KR_SHORT_RATES = ("ecos:722Y001/0101000", "ecos:817Y002/010502000")
_KR_BOND_RATES = ("ecos:817Y002/010200000", "ecos:817Y002/010210000")
_US_RATES = ("fred:DFEDTARU", "fred:DGS2", "fred:DGS10")
_BOK_FEEDS = ("bok-press-conference", "bok-mpb-minutes")
_FED_FEEDS = ("fed-monetary",)


@dataclass(frozen=True)
class Exposure:
    key: str
    amount: Decimal  # Base currency.
    share_of_assets: Decimal | None
    series: tuple[str, ...]  # "provider:series_id"
    feeds: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class ExposureResult:
    """Exposures plus the completeness of the data they were measured from.

    When values could not be converted to the base currency, amounts are partial and shares of
    total assets are withheld, because the denominator itself is incomplete.
    """

    exposures: tuple[Exposure, ...]
    complete: bool
    unconverted: dict[str, Decimal]
    warnings: tuple[str, ...]


def exposures(conn: sqlite3.Connection, *, as_of: datetime, known_at: datetime) -> ExposureResult:
    """Measure exposures from one consistent ledger snapshot, the same view `smith summary` uses."""
    with ledger.snapshot(conn):
        summary = build_summary(conn, as_of=as_of, known_at=known_at)
        states = [s.record for s in ledger.record_states(conn, as_of=as_of, known_at=known_at)
                  if s.record.status is Status.ACTIVE]
    rates = {BASE_CURRENCY: Decimal(1), **{f["subject"]: f["value"] for f in summary.fx}}

    def total(kind: Kind, amount_field: str, accept) -> Decimal:
        return sum((Decimal(r.fields[amount_field]) * rates[r.fields["currency"]] for r in states
                    if r.kind is kind and r.fields["currency"] in rates and accept(r.fields)), Decimal(0))

    def share(amount: Decimal) -> Decimal | None:
        return amount / summary.total_assets if summary.total_assets and summary.complete else None

    items = [
        Exposure("variable_rate_debt",
                 total(Kind.LIABILITY, "outstanding_principal", lambda f: f["rate_type"] in ("variable", "mixed")),
                 None, _KR_SHORT_RATES + _KR_BOND_RATES, _BOK_FEEDS,
                 "interest on variable-rate loans resets with Korean market rates"),
        Exposure("krw_cash_and_deposits",
                 total(Kind.ASSET, "value", lambda f: f["currency"] == "KRW" and
                       f["category"] in ("cash", "deposit", "installment_savings")),
                 None, _KR_SHORT_RATES, _BOK_FEEDS, "deposit yields follow Korean short-term rates"),
        Exposure("usd_assets", summary.by_currency.get("USD", Decimal(0)), None,
                 ("ecos:731Y001/0000001",) + _US_RATES, _FED_FEEDS,
                 "KRW value moves with the exchange rate and US rates"),
        Exposure("equities_and_unclassified_securities",
                 sum((summary.by_category.get(c, Decimal(0)) for c in ("stock", "fund", "unclassified")), Decimal(0)),
                 None, _KR_BOND_RATES + _US_RATES, _BOK_FEEDS + _FED_FEEDS,
                 "valuations are sensitive to long-term rates; unclassified holdings have unknown composition"),
        Exposure("real_estate", summary.by_category.get("real_estate", Decimal(0)), None,
                 _KR_SHORT_RATES + _KR_BOND_RATES, _BOK_FEEDS,
                 "housing prices and refinancing costs respond to Korean rates; illiquid for months"),
    ]
    items += _lease_deposits_due(states, rates, as_of.date())
    # Shares are of total assets, so they are given for asset exposures only.
    liabilities = ("variable_rate_debt", "lease_deposit_due")
    found = tuple(Exposure(e.key, e.amount, None if e.key.startswith(liabilities) else share(e.amount),
                           e.series, e.feeds, e.note) for e in items if e.amount > 0)
    warnings = tuple(summary.warnings)
    if not summary.complete:
        warnings += ("exposure amounts are partial and shares are withheld: "
                     f"incomplete areas {', '.join(summary.incomplete_areas)}",)
    return ExposureResult(found, summary.complete, dict(summary.unconverted), warnings)


def _lease_deposits_due(states: list, rates: dict[str, Decimal], today: date) -> list[Exposure]:
    """Deposits the household must return are a dated cash need, so each is listed with its due date."""
    found = []
    for record in states:
        fields = record.fields
        if (record.kind is not Kind.LIABILITY or fields.get("category") != "lease_deposit_obligation"
                or fields["currency"] not in rates or not fields.get("maturity")):
            continue
        due = date.fromisoformat(fields["maturity"])
        months = (due.year - today.year) * 12 + due.month - today.month
        found.append(Exposure(f"lease_deposit_due:{record.record_id}",
                              Decimal(fields["outstanding_principal"]) * rates[fields["currency"]], None,
                              _KR_SHORT_RATES, _BOK_FEEDS, f"must be returned by {due} ({months} months)"))
    return found
