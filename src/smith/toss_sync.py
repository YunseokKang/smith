"""Build a ledger snapshot batch from the read-only Toss client.

Every request must succeed and every response must have the documented shape: a partial or
malformed snapshot would make missing positions look sold, so any doubt aborts the whole sync.
Values that cannot be valued safely (unsupported currency, missing amount, non-positive FX rate)
abort; values that can be kept without changing amounts (an unknown market) are stored as `other`.
Account numbers are dropped; accounts are keyed by an opaque hash.

Quotes for stocks held only in hand-imported accounts are the exception: they refine those lots' prices and
cannot make a position look sold, so failing to read them leaves the snapshot as it is.
"""
import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime
from typing import Any

from smith.records import (
    CURRENCIES, QUOTE_METRIC, ChangeType, ImportBatch, Kind, Observation, RecordInput, Status, canonical_decimal,
)
from smith.toss import TossClient, TossError

logger = logging.getLogger(__name__)

SOURCE = "toss"
_BUYING_POWER_CURRENCIES = ("KRW", "USD")
_SYMBOL = re.compile(r"[A-Za-z0-9.\-]{1,20}")
_QUOTE_CURRENCY = {"KR": "KRW", "US": "USD"}   # The markets `GET /api/v1/prices` quotes, in their currency.
_QUOTE_BATCH = 200                              # Symbols per prices request (spec v1.2.28).


class SyncError(Exception):
    """The provider response is malformed or outside Smith's contract; nothing should be stored."""


def collect_snapshot(client: TossClient, *, owner_id: str, collected_at: datetime,
                     quote_for: Iterable[tuple[str, str, str]] = ()) -> ImportBatch:
    """Query every allowlisted endpoint and return a snapshot batch. Holdings carry no valuation
    time, so the collection time is used as the effective time. `quote_for` names the instruments
    (market, symbol, currency) of hand-imported lots; those no Toss holding prices get a quote observation.

    Raises:
        TossError: a request failed.
        SyncError: a response is malformed or contains values Smith cannot represent.
    """
    records: list[RecordInput] = []
    observations: list[Observation] = []
    for account in _list(client.accounts(), "accounts"):
        account_seq = _field(account, "accountSeq", "accounts[]")
        if type(account_seq) is not int:
            raise SyncError("accounts[].accountSeq is not an integer; sync aborted")
        label = account_label(account_seq)
        items = _list(_field(client.holdings(account_seq), "items", "holdings"), "holdings.items")
        positions = [_position(item, label, owner_id, collected_at) for item in items]
        if len({p.record_id for p in positions}) != len(positions):
            raise SyncError("holdings.items contains a duplicate symbol; sync aborted")
        records.extend(positions)
        for currency in _BUYING_POWER_CURRENCIES:
            power = _field(client.buying_power(account_seq, currency), "cashBuyingPower", "buying-power")
            observations.append(Observation(label, "cash_buying_power", currency,
                                            _amount(power, "cashBuyingPower"), collected_at))
    observations.append(_fx_observation(client.exchange_rate("USD", "KRW")))
    held = {(r.fields["market"], r.fields["symbol"], r.fields["currency"]) for r in records}
    observations.extend(_quotes(client, set(quote_for) - held, collected_at))
    import_id = f"toss-{collected_at.strftime('%Y%m%dT%H%M%S%f')}"
    content = {"records": [_canonical(asdict(r)) for r in records],
               "observations": [_canonical(asdict(o)) for o in observations]}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode("utf-8")).hexdigest()
    return ImportBatch(import_id, SOURCE, "snapshot", collected_at, (owner_id,), tuple(records), digest,
                       tuple(observations))


def account_label(account_seq: int) -> str:
    """Stable opaque account key; the broker's account number is never stored."""
    return "toss-" + hashlib.sha256(f"toss-account:{account_seq}".encode("ascii")).hexdigest()[:8]


def _position(item: Any, label: str, owner_id: str, collected_at: datetime) -> RecordInput:
    currency = _field(item, "currency", "holdings.items[]")
    if currency not in CURRENCIES:
        raise SyncError(f"holding currency {currency!r} is not supported; sync aborted")
    symbol = _field(item, "symbol", "holdings.items[]")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise SyncError("holdings.items[].symbol is malformed; sync aborted")
    market_value = _field(item, "marketValue", "holdings.items[]")
    market = item.get("marketCountry")
    name = item.get("name")
    fields = {
        "category": "stock", "account_type": "brokerage", "currency": currency,
        "value": _amount(_field(market_value, "amount", "marketValue"), "marketValue.amount"),
        "valuation_method": "api", "liquidity": "days",
        "symbol": symbol, "instrument_name": name[:100] if isinstance(name, str) and name.strip() else symbol,
        "market": market if market in ("KR", "US") else "other",
        "quantity": _amount(_field(item, "quantity", "holdings.items[]"), "quantity"),
        "unit_price": _amount(_field(item, "lastPrice", "holdings.items[]"), "lastPrice"),
        "average_cost": _amount(_field(item, "averagePurchasePrice", "holdings.items[]"), "averagePurchasePrice"),
        "value_after_costs": _amount(_field(market_value, "amountAfterCost", "marketValue"),
                                     "marketValue.amountAfterCost"),
    }
    return RecordInput(f"{label}-{symbol.lower()}", Kind.ASSET, owner_id, collected_at, 0, Status.ACTIVE,
                       ChangeType.UPDATE, None, None, fields)


def _quotes(client: TossClient, wanted: set[tuple[str, str, str]], collected_at: datetime) -> list[Observation]:
    """Toss's last price of each instrument in `wanted` that it can quote (a KR or US stock in that market's
    currency), as `QUOTE_METRIC` observations (report design §37). A failed request stores no quote and the sync
    goes on: the lots keep their statement price and the report names them as not repriced. A symbol Toss does
    not know is left out of its answer, not an error (checked 2026-11-17)."""
    symbols = {symbol: (market, currency) for market, symbol, currency in wanted
               if _QUOTE_CURRENCY.get(market) == currency and _SYMBOL.fullmatch(symbol)}
    names = sorted(symbols)
    items: list[Any] = []
    try:
        for start in range(0, len(names), _QUOTE_BATCH):
            items += _list(client.prices(names[start:start + _QUOTE_BATCH]), "prices")
    except (TossError, SyncError) as error:
        logger.warning("Toss quotes not stored (%s); %d hand-imported instrument(s) keep their statement price",
                       error.code if isinstance(error, TossError) else "invalid-response", len(names))
        return []
    quotes = {quote.subject: quote for quote in (_quote(item, symbols, collected_at) for item in items) if quote}
    if len(quotes) < len(names):
        logger.warning("Toss quoted %d of %d hand-imported instrument(s)", len(quotes), len(names))
    return [quotes[subject] for subject in sorted(quotes)]


def _quote(item: Any, wanted: dict[str, tuple[str, str]], collected_at: datetime) -> Observation | None:
    """One price item as an observation; None for one that cannot be trusted (a symbol not asked for, another
    currency, no positive price, no time: Toss sends none when nothing has traded)."""
    if not isinstance(item, dict) or not isinstance(item.get("symbol"), str) or item["symbol"] not in wanted:
        return None
    market, currency = wanted[item["symbol"]]
    try:
        price = _amount(item.get("lastPrice"), "lastPrice")
        stamp = datetime.fromisoformat(item.get("timestamp"))
    except (SyncError, TypeError, ValueError):
        return None
    if item.get("currency") != currency or price == "0" or stamp.tzinfo is None:
        return None
    # Toss's clock read about 20 s ahead of this host's (2026-11-17): a quote is never dated after its collection.
    return Observation(f"{market}:{item['symbol']}", QUOTE_METRIC, currency, price, min(stamp, collected_at))


def _fx_observation(rate: Any) -> Observation:
    """A rate of zero would silently value foreign assets at nothing, so it must be positive."""
    mid = _amount(_field(rate, "midRate", "exchange-rate"), "midRate")
    if mid == "0":
        raise SyncError("exchange-rate.midRate is not positive; sync aborted")
    valid_from = _field(rate, "validFrom", "exchange-rate")
    try:
        observed_at = datetime.fromisoformat(valid_from)
    except (TypeError, ValueError):
        raise SyncError("exchange-rate.validFrom is not a timestamp; sync aborted") from None
    if observed_at.tzinfo is None:
        raise SyncError("exchange-rate.validFrom has no UTC offset; sync aborted")
    return Observation("USD", "fx_mid_rate", "KRW", mid, observed_at)


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise SyncError(f"{name} is not a list; sync aborted")
    return value


def _field(container: Any, key: str, name: str) -> Any:
    if not isinstance(container, dict) or container.get(key) is None:
        raise SyncError(f"{name}.{key} is missing; sync aborted")
    return container[key]


def _amount(value: Any, name: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise SyncError(f"{name} is not a decimal; sync aborted")
    try:
        text = canonical_decimal(str(value))
    except ValueError:
        raise SyncError(f"{name} is not a decimal; sync aborted") from None
    if text.startswith("-"):
        raise SyncError(f"{name} is negative; sync aborted")
    return text


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, datetime):
        return value.isoformat()
    return value
