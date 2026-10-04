"""Deterministic portfolio summary computed from the ledger.

All amounts are Decimal. Values that cannot be converted to the base currency are reported
separately and mark the summary incomplete; they are never counted as zero.
"""
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from smith import ledger
from smith.records import (
    ASSET_CATEGORIES, INFLOW_CATEGORIES, OUTFLOW_CATEGORIES, TRANSFER_CATEGORIES, Kind, RecordInput,
    Status, StoredRevision,
)

BASE_CURRENCY = "KRW"
# Old category value -> current value, per the taxonomy change policy in docs/data-contract.md.
CATEGORY_ALIASES: dict[str, str] = {}
# Divide rather than multiply by 1/n so that whole annual amounts stay exact.
_MONTHS_PER_PERIOD = {"monthly": 1, "quarterly": 3, "annual": 12}
_STALE_MANUAL = timedelta(days=90)
_STALE_API = timedelta(days=7)
_STALE_FX = timedelta(days=7)
_UPCOMING = timedelta(days=365)


@dataclass
class Summary:
    as_of: datetime
    known_at: datetime
    base_currency: str = BASE_CURRENCY
    total_assets: Decimal = Decimal(0)
    total_liabilities: Decimal = Decimal(0)
    unconverted: dict[str, Decimal] = field(default_factory=dict)
    by_category: dict[str, Decimal] = field(default_factory=dict)
    by_account_type: dict[str, Decimal] = field(default_factory=dict)
    by_currency: dict[str, Decimal] = field(default_factory=dict)
    by_liquidity: dict[str, Decimal] = field(default_factory=dict)
    # Household totals combine owners; per-owner net worth keeps legal ownership visible.
    net_worth_by_owner: dict[str, Decimal] = field(default_factory=dict)
    monthly_inflow: Decimal = Decimal(0)
    monthly_outflow: Decimal = Decimal(0)
    monthly_transfers: Decimal = Decimal(0)
    monthly_loan_interest_estimate: Decimal = Decimal(0)
    # Liabilities already in the interest estimate; several repayment flows can share one loan.
    interest_estimated_for: list[str] = field(default_factory=list)
    upcoming_once: list[dict[str, Any]] = field(default_factory=list)
    goals: list[dict[str, Any]] = field(default_factory=list)
    fx: list[dict[str, Any]] = field(default_factory=list)
    reference: list[dict[str, Any]] = field(default_factory=list)
    freshness: list[dict[str, Any]] = field(default_factory=list)
    sync: list[dict[str, Any]] = field(default_factory=list)
    # Areas where something was left out of the totals: balance_sheet, cash_flow, goals.
    incomplete_areas: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def net_worth(self) -> Decimal:
        return self.total_assets - self.total_liabilities

    @property
    def monthly_net(self) -> Decimal:
        return self.monthly_inflow - self.monthly_outflow

    @property
    def complete(self) -> bool:
        return not self.incomplete_areas


def build_summary(conn: sqlite3.Connection, *, as_of: datetime, known_at: datetime) -> Summary:
    """Summarize the ledger as it stood at `as_of`, using only data recorded by `known_at`."""
    with ledger.snapshot(conn):
        return _build(conn, Summary(as_of, known_at))


def _build(conn: sqlite3.Connection, summary: Summary) -> Summary:
    as_of, known_at = summary.as_of, summary.known_at
    states = [s for s in ledger.record_states(conn, as_of=as_of, known_at=known_at)
              if s.record.status is Status.ACTIVE]
    rates = _load_observations(conn, summary)
    by_id = {s.record.record_id: s.record for s in states}
    for state in states:
        record = state.record
        if record.kind is Kind.ASSET:
            _add_asset(summary, record, rates)
        elif record.kind is Kind.LIABILITY:
            _add_liability(summary, record, rates)
        elif record.kind is Kind.CASHFLOW:
            _add_cashflow(summary, record, rates, by_id)
        else:
            _add_goal(summary, record, rates)
    _add_freshness(summary, states)
    _add_sync_status(summary, ledger.sync_status(conn, known_at=known_at))
    return summary


def _load_observations(conn: sqlite3.Connection, summary: Summary) -> dict[str, Decimal]:
    rates = {BASE_CURRENCY: Decimal(1)}
    observations = ledger.latest_observations(conn, as_of=summary.as_of, known_at=summary.known_at)
    for (subject, metric, currency), (observation, source) in sorted(observations.items()):
        entry = {"subject": subject, "metric": metric, "currency": currency, "value": Decimal(observation.value),
                 "observed_at": observation.observed_at, "source": source}
        stale = summary.as_of - observation.observed_at > _STALE_FX
        if metric == "fx_mid_rate" and currency == BASE_CURRENCY:
            if entry["value"] <= 0:
                # A zero rate would value foreign holdings at nothing; treat the rate as missing.
                summary.warnings.append(f"FX {subject}/{currency} is not positive and was ignored")
                continue
            rates[subject] = entry["value"]
            summary.fx.append(entry)
        else:
            summary.reference.append(entry)
        if stale:
            summary.warnings.append(f"{subject} {metric} {currency} is older than {_STALE_FX.days} days")
    return rates


def _mark_incomplete(summary: Summary, area: str) -> None:
    if area not in summary.incomplete_areas:
        summary.incomplete_areas.append(area)


def _convert(amount: Decimal, currency: str, rates: dict[str, Decimal]) -> Decimal | None:
    return amount * rates[currency] if currency in rates else None


def _add_asset(summary: Summary, record: RecordInput, rates: dict[str, Decimal]) -> None:
    fields = record.fields
    amount, currency = Decimal(fields["value"]), fields["currency"]
    base = _convert(amount, currency, rates)
    if base is None:
        summary.unconverted[currency] = summary.unconverted.get(currency, Decimal(0)) + amount
        _mark_incomplete(summary, "balance_sheet")
        return
    category = CATEGORY_ALIASES.get(fields["category"], fields["category"])
    if category not in ASSET_CATEGORIES:
        summary.warnings.append(f"{record.record_id}: unknown category {category!r} counted as other")
        category = "other"
    summary.total_assets += base
    _add_owner(summary, record.owner_id, base)
    for groups, key in ((summary.by_category, category), (summary.by_account_type, fields.get("account_type")),
                        (summary.by_currency, currency), (summary.by_liquidity, fields["liquidity"])):
        key = key or "unknown"
        groups[key] = groups.get(key, Decimal(0)) + base


def _add_liability(summary: Summary, record: RecordInput, rates: dict[str, Decimal]) -> None:
    amount, currency = Decimal(record.fields["outstanding_principal"]), record.fields["currency"]
    base = _convert(amount, currency, rates)
    if base is None:
        summary.unconverted[currency] = summary.unconverted.get(currency, Decimal(0)) - amount
        _mark_incomplete(summary, "balance_sheet")
        return
    summary.total_liabilities += base
    _add_owner(summary, record.owner_id, -base)


def _add_owner(summary: Summary, owner_id: str, amount: Decimal) -> None:
    summary.net_worth_by_owner[owner_id] = summary.net_worth_by_owner.get(owner_id, Decimal(0)) + amount


def _add_cashflow(summary: Summary, record: RecordInput, rates: dict[str, Decimal],
                  by_id: dict[str, RecordInput]) -> None:
    fields = record.fields
    base = _convert(Decimal(fields["amount"]), fields["currency"], rates)
    if base is None:
        summary.warnings.append(f"{record.record_id}: no FX rate for {fields['currency']}; cash flow excluded")
        _mark_incomplete(summary, "cash_flow")
        return
    today = summary.as_of.date()
    start = date.fromisoformat(fields["start_date"])
    if fields["frequency"] == "once":
        if today < start <= today + _UPCOMING:
            summary.upcoming_once.append({"record_id": record.record_id, "date": start, "category": fields["category"],
                                          "direction": fields["direction"], "amount": base})
        return
    if not recurring_active(fields, today):
        return
    monthly = base / _MONTHS_PER_PERIOD[fields["frequency"]]
    category = fields["category"]
    if category in TRANSFER_CATEGORIES:
        summary.monthly_transfers += monthly
    elif category in INFLOW_CATEGORIES:
        summary.monthly_inflow += monthly
    elif category in OUTFLOW_CATEGORIES:
        summary.monthly_outflow += monthly
        if category == "loan_payment":
            _add_loan_interest(summary, record, by_id, rates)


def recurring_active(fields: dict[str, Any], today: date) -> bool:
    """A recurring cash flow counts on `today` (a local calendar date) between its start and end dates."""
    if fields["frequency"] == "once":
        return False
    end = date.fromisoformat(fields["end_date"]) if fields.get("end_date") else None
    return date.fromisoformat(fields["start_date"]) <= today and (end is None or end >= today)


def _add_loan_interest(summary: Summary, payment: RecordInput, by_id: dict[str, RecordInput],
                       rates: dict[str, Decimal]) -> None:
    """Estimate the interest part of a repayment as outstanding principal x annual rate / 12."""
    loan = by_id.get(payment.fields.get("liability_record_id") or "")
    if loan is None:
        summary.warnings.append(f"{payment.record_id}: linked liability is not active; interest not estimated")
        return
    if loan.record_id in summary.interest_estimated_for:
        return
    principal = _convert(Decimal(loan.fields["outstanding_principal"]), loan.fields["currency"], rates)
    if principal is not None:
        summary.interest_estimated_for.append(loan.record_id)
        summary.monthly_loan_interest_estimate += principal * Decimal(loan.fields["annual_rate"]) / 12


def _add_goal(summary: Summary, record: RecordInput, rates: dict[str, Decimal]) -> None:
    fields = record.fields
    target = _convert(Decimal(fields["target_amount"]), fields["currency"], rates)
    target_date = date.fromisoformat(fields["target_date"])
    today = summary.as_of.date()
    months_left = (target_date.year - today.year) * 12 + target_date.month - today.month
    summary.goals.append({"record_id": record.record_id, "category": fields["category"],
                          "priority": fields["priority"], "target_date": target_date,
                          "months_left": months_left, "target": target, "currency": fields["currency"]})
    if target is None:
        summary.warnings.append(f"{record.record_id}: no FX rate for {fields['currency']}; target not converted")
        _mark_incomplete(summary, "goals")


def _add_freshness(summary: Summary, states: list[StoredRevision]) -> None:
    by_source: dict[str, list[datetime]] = defaultdict(list)
    for state in states:
        by_source[state.source].append(state.record.effective_at)
    for source, times in sorted(by_source.items()):
        limit = _STALE_MANUAL if source.startswith("manual") else _STALE_API
        stale = sum(summary.as_of - time > limit for time in times)
        summary.freshness.append({"source": source, "records": len(times), "oldest": min(times),
                                  "newest": max(times), "stale": stale})
        if stale:
            summary.warnings.append(f"{source}: {stale} record(s) older than {limit.days} days")


def _add_sync_status(summary: Summary, status: dict[str, dict[str, Any]]) -> None:
    """A failed refresh after the last success means the stored data may be out of date."""
    for source, entry in sorted(status.items()):
        summary.sync.append({"source": source, **entry})
        failure, success = entry["last_failure"], entry["last_success"]
        if failure is not None and (success is None or failure > success):
            since = success.isoformat(timespec="minutes") if success else "never"
            summary.warnings.append(f"{source}: last sync failed at {failure.isoformat(timespec='minutes')} "
                                    f"({entry['last_error']}); last success {since}")


def to_dict(summary: Summary) -> dict[str, Any]:
    """JSON-ready form: Decimals and times become strings so no float ever appears."""
    def plain(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, list):
            return [plain(item) for item in value]
        if isinstance(value, Decimal):
            return format(value.quantize(Decimal("0.0001")).normalize(), "f")
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value

    data = {name: getattr(summary, name) for name in summary.__dataclass_fields__}
    data.update(net_worth=summary.net_worth, monthly_net=summary.monthly_net, complete=summary.complete)
    return plain(data)


def render(summary: Summary) -> list[str]:
    """Human-readable report. Amounts are rounded to whole base-currency units for display only."""
    lines = [f"Summary at {summary.as_of.isoformat(timespec='seconds')} "
             f"(known at {summary.known_at.isoformat(timespec='seconds')}), base {summary.base_currency}",
             f"Net worth      {_money(summary.net_worth):>18}  "
             f"(assets {_money(summary.total_assets)} - liabilities {_money(summary.total_liabilities)})"]
    if len(summary.net_worth_by_owner) > 1:
        lines.append("  by owner     " + ", ".join(f"{owner} {_money(amount)}"
                                                for owner, amount in sorted(summary.net_worth_by_owner.items())))
    if not summary.complete:
        lines.append(f"INCOMPLETE     {', '.join(summary.incomplete_areas)}")
    if summary.unconverted:
        missing = ", ".join(f"{c} {_money(a)}" for c, a in sorted(summary.unconverted.items()))
        lines.append(f"               not converted (no FX rate): {missing}")
    lines += [f"FX             {f['subject']}/{f['currency']} {f['value']} ({f['source']}, "
              f"{f['observed_at'].isoformat(timespec='minutes')})" for f in summary.fx]
    for title, groups in (("category", summary.by_category), ("account type", summary.by_account_type),
                          ("currency", summary.by_currency), ("liquidity", summary.by_liquidity)):
        lines.append(f"\nBy {title}")
        for key, amount in sorted(groups.items(), key=lambda item: -item[1]):
            share = amount / summary.total_assets * 100 if summary.total_assets else Decimal(0)
            lines.append(f"  {key:<26} {_money(amount):>18} {share:6.1f}%")
    lines += ["\nMonthly cash flow (recurring)",
              f"  inflow {_money(summary.monthly_inflow)}  outflow {_money(summary.monthly_outflow)}  "
              f"net {_money(summary.monthly_net)}  internal transfers {_money(summary.monthly_transfers)}",
              f"  loan interest (estimate) {_money(summary.monthly_loan_interest_estimate)}"]
    lines += [f"  one-time {u['date']} {u['direction']} {u['category']} {_money(u['amount'])}"
              for u in summary.upcoming_once]
    if summary.goals:
        lines.append("\nGoals")
        lines += [f"  {g['category']:<16} {g['priority']:<6} {g['target_date']} ({g['months_left']} months) "
                  f"{_money(g['target']) if g['target'] is not None else g['currency'] + ' (not converted)'}"
                  for g in summary.goals]
    if summary.reference:
        lines.append("\nReference only (not in net worth)")
        lines += [f"  {r['subject']} {r['metric']} {r['currency']} {r['value']} ({r['source']}, "
                  f"{r['observed_at'].isoformat(timespec='minutes')})" for r in summary.reference]
    lines.append("\nData freshness")
    lines += [f"  {f['source']:<16} {f['records']} record(s), oldest {f['oldest'].date()}, "
              f"newest {f['newest'].date()}, stale {f['stale']}" for f in summary.freshness]
    for entry in summary.sync:
        success, failure = entry["last_success"], entry["last_failure"]
        lines.append(f"  sync {entry['source']:<11} last success "
                     f"{success.isoformat(timespec='minutes') if success else 'never'}, last failure "
                     f"{failure.isoformat(timespec='minutes') if failure else 'none'}")
    if summary.warnings:
        lines.append("\nWarnings")
        lines += [f"  - {warning}" for warning in summary.warnings]
    return lines


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal(1), rounding=ROUND_HALF_UP):,}"
