"""Connectivity check for the read-only Toss client.

By default the report shows structure only (counts, currencies, timestamps) so it can be shared
with a development agent. Amounts and symbols appear only with `show_values`. Account numbers are
never printed: accounts are numbered by position.
"""
from collections.abc import Callable

from smith.toss import TossClient, TossError

_HINTS = {
    401: "client id/secret rejected or client inactive; re-run `smith toss login`",
    403: "this machine's public IP is not allowed; add it in Toss WTS > Settings > Open API > allowed IPs",
    429: "rate limited; wait a few seconds and retry",
}


def run_check(client: TossClient, *, show_values: bool, emit: Callable[[str], None] = print) -> int:
    """Call each allowlisted endpoint once and report the outcome. Returns a process exit code."""
    step = "token/accounts"  # The first call also issues the token.
    try:
        accounts = client.accounts()
        emit(f"token          ok (expires_in={client.token_expires_in}s)")
        types = sorted({str(account.get("accountType")) for account in accounts})
        emit(f"accounts       ok: {len(accounts)} account(s), types: {', '.join(types) or '-'}")
        for index, account in enumerate(accounts, start=1):
            step = f"account {index}"
            _check_account(client, index, account["accountSeq"], show_values, emit)
        step = "exchange-rate"
        rate = client.exchange_rate("USD", "KRW")
        detail = f", midRate {rate['midRate']}" if show_values else ""
        emit(f"exchange-rate  USD/KRW ok, valid from {rate['validFrom']}{detail}")
    except (KeyError, TypeError, AttributeError):
        emit(f"{step}: failed: invalid-response (unexpected response shape)")
        return 1
    except TossError as error:
        emit(f"{step}: failed: {error}")
        if error.status in _HINTS:
            emit(f"  hint: {_HINTS[error.status]}")
        return 1
    if not show_values:
        emit("Values hidden. Run with --show-values in your own terminal to compare amounts with the app.")
    return 0


def _check_account(client: TossClient, index: int, account_seq: int, show_values: bool,
                   emit: Callable[[str], None]) -> None:
    holdings = client.holdings(account_seq)
    items = holdings.get("items", [])
    currencies = sorted({str(item.get("currency")) for item in items})
    emit(f"account {index}      holdings ok: {len(items)} item(s), currencies: {', '.join(currencies) or '-'}")
    if show_values:
        amount = holdings["marketValue"]["amount"]
        emit(f"  market value  KRW {amount.get('krw')}  USD {amount.get('usd')}")
        for item in items:
            emit(f"  {item['symbol']:<10} {item['name']:<20} qty {item['quantity']:<12} "
                 f"{item['currency']} {item['marketValue']['amount']}")
    for currency in ("KRW", "USD"):
        power = client.buying_power(account_seq, currency)
        detail = f" {power['cashBuyingPower']}" if show_values else ""
        emit(f"  buying-power  {currency} ok{detail}")
