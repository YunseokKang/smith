"""This year's sales of listed stocks, read from the ledger's history (FR-17 tax view).

A position synced from a broker (ledger.MARKET_SOURCES) whose quantity fell between two snapshots, or that was
closed, was sold: the shares that left, at the average cost of the snapshot before. The broker's fill price is not
stored, so the sale price is that snapshot's quote (`price_basis` "last_quote") and the gain is an estimate (client
reply 2026-11-03: Smith asked for the purchase price of a stock it had just recorded as sold).

Manually imported lots are left out: a statement re-import can close one record and open another without any sale.
A sale and a buy-back of the same quantity between two snapshots leave the quantity unchanged and are not seen.
"""
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from smith import ledger
from smith.records import Kind, RecordInput, Status, StoredRevision

BASE_CURRENCY = "KRW"


@dataclass(frozen=True)
class Sale:
    record_id: str
    owner_id: str
    symbol: str
    name: str
    market: str
    currency: str
    quantity: Decimal
    average_cost: Decimal
    price: Decimal
    price_basis: str            # "last_quote": the quote of the last snapshot that still held the shares.
    held_at: datetime           # The last snapshot that still held the shares.
    seen_at: datetime           # The first snapshot without them: the sale happened in between.
    gain: Decimal | None        # (price - average cost) x quantity, in `currency`; None when the sale is unclear.
    gain_won: Decimal | None    # At the FX rate observed when the sale was seen; None without one or without a gain.
    unclear: str | None = None  # Why the gain is unknown: "moved", "cost-changed" or "pending" (see `sales`).


# A move between accounts can show as shares leaving one position and arriving in another a few snapshots later.
MOVE_WINDOW = timedelta(days=7)
# The account part of a broker position's record id (toss_sync: "<account label>-<symbol>").
_ACCOUNT = re.compile(r"(toss-[0-9a-f]{8})-")


def sales(conn: sqlite3.Connection, *, as_of: datetime, known_at: datetime) -> list[Sale]:
    """The sales seen in `as_of`'s calendar year (in `as_of`'s own time zone) up to `as_of`, oldest first.

    A fall in shares is not always a sale (review 2026-11-03). When the same stock grew in another broker position of
    the owner from the last snapshot that held the shares until MOVE_WINDOW after the fall, the shares may have moved
    between accounts or been bought back elsewhere (`unclear` "moved"); when a position shrank and its average cost
    changed, a reverse split or another trade is mixed in (`unclear` "cost-changed"); when the owner has another
    broker account, a fall younger than MOVE_WINDOW may still arrive there (`unclear` "pending"; with one account,
    as Toss exposes today, a fall is a sale at once). Their gain is unknown, never
    guessed or dropped. The year counts the snapshot that saw the sale; a sale on the year's last trading days can be
    seen only in January (Korean tax counts it on its settlement day), which this estimate does not resolve."""
    start = as_of.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    found: list[Sale] = []
    grew: list[tuple[str, str, str, str, datetime]] = []   # (owner, market, symbol, record id, snapshot).
    accounts: dict[str, set[str]] = {}                       # owner -> broker accounts seen.
    for history in ledger.record_histories(conn, known_at=known_at):
        live = [r for r in ledger.effective_history(history) if r.source in ledger.MARKET_SOURCES
                and r.record.effective_at <= as_of]
        if live and (account := _ACCOUNT.match(live[0].record.record_id)):
            accounts.setdefault(live[0].record.owner_id, set()).add(account.group(1))
        for before, after in zip([None, *live], live):
            if _grew(before, after):
                fields = after.record.fields
                grew.append((after.record.owner_id, fields.get("market") or "other", fields["symbol"],
                             after.record.record_id, after.record.effective_at))
            elif before is not None and after.record.effective_at >= start and (sale := _sale(before, after)):
                found.append(sale)
    rates: dict[datetime, dict[str, Decimal]] = {}
    priced = []
    for sale in sorted(found, key=lambda s: (s.seen_at, s.record_id)):
        if any((owner, market, symbol) == (sale.owner_id, sale.market, sale.symbol) and record_id != sale.record_id
               and sale.held_at <= at <= sale.seen_at + MOVE_WINDOW for owner, market, symbol, record_id, at in grew):
            sale = replace(sale, gain=None, unclear="moved")
        elif len(accounts.get(sale.owner_id, ())) > 1 and as_of < sale.seen_at + MOVE_WINDOW:
            # With another broker account, the shares may still arrive there: not a sale yet (review 2026-11-03).
            sale = replace(sale, gain=None, unclear="pending")
        if sale.gain is not None:
            if sale.seen_at not in rates:
                rates[sale.seen_at] = _rates(conn, sale.seen_at, known_at)
            rate = rates[sale.seen_at].get(sale.currency)
            sale = sale if rate is None else replace(sale, gain_won=sale.gain * rate)
        priced.append(sale)
    return priced


def _grew(before: StoredRevision | None, after: StoredRevision) -> bool:
    """Whether a position opened or gained shares in a snapshot."""
    new = after.record
    if new.kind is not Kind.ASSET or new.status is not Status.ACTIVE or not new.fields.get("symbol"):
        return False
    now = _quantity(new)
    was = Decimal(0) if before is None else _quantity(before.record)
    return now is not None and was is not None and now > was


def covered_since(conn: sqlite3.Connection, *, known_at: datetime) -> datetime | None:
    """The first broker snapshot: sales before it are not in the ledger, so `sales` cannot see them."""
    return ledger.first_effective(conn, sources=ledger.MARKET_SOURCES, known_at=known_at)


def _sale(before: StoredRevision, after: StoredRevision) -> Sale | None:
    """The shares that left between two snapshots of one position, or None when none did or an input is missing."""
    old, new = before.record, after.record
    fields = old.fields
    if (old.kind is not Kind.ASSET or old.status is not Status.ACTIVE
            or any(fields.get(k) is None for k in ("symbol", "quantity", "average_cost", "unit_price", "currency"))):
        return None
    left = _quantity(new)
    if left is None:
        return None
    sold = Decimal(fields["quantity"]) - left
    if sold <= 0:
        return None
    cost, price = Decimal(fields["average_cost"]), Decimal(fields["unit_price"])
    # A sale leaves the moving average cost of the shares kept unchanged; a changed one means a split or a buy too.
    changed = new.status is Status.ACTIVE and new.fields.get("average_cost") not in (None, fields["average_cost"])
    return Sale(old.record_id, old.owner_id, fields["symbol"], fields.get("instrument_name") or fields["symbol"],
                fields.get("market") or "other", fields["currency"], sold, cost, price, "last_quote",
                old.effective_at, new.effective_at, None if changed else (price - cost) * sold, None,
                "cost-changed" if changed else None)


def _quantity(record: RecordInput) -> Decimal | None:
    if record.status is Status.CLOSED:
        return Decimal(0)
    raw = record.fields.get("quantity")
    return None if raw is None else Decimal(raw)


def _rates(conn: sqlite3.Connection, at: datetime, known_at: datetime) -> dict[str, Decimal]:
    observed = ledger.latest_observations(conn, as_of=at, known_at=known_at)
    rates = {BASE_CURRENCY: Decimal(1)}
    for (subject, metric, currency), (observation, _) in observed.items():
        if metric == "fx_mid_rate" and currency == BASE_CURRENCY and Decimal(observation.value) > 0:
            rates[subject] = Decimal(observation.value)
    return rates

