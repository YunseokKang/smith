"""Readable Korean money and rate formats shared by the report and its proposals."""
from decimal import Decimal


def short_won(amount: Decimal | None) -> str:
    """Readable won: 27.3억 원, 4,120만 원, 85,000원. Unknown amounts read 미상, never 0."""
    if amount is None:
        return "미상"
    value = abs(amount)
    sign = "-" if amount < 0 else ""
    if value >= 100_000_000:
        return f"{sign}{value / 100_000_000:,.1f}억 원"
    if value >= 10_000:
        return f"{sign}{value / 10_000:,.0f}만 원"
    return f"{sign}{value:,.0f}원"


def percent(rate: Decimal | None, places: int = 2) -> str:
    """A fraction as a percentage: 0.0361 -> 3.61%."""
    return "미상" if rate is None else f"{rate * 100:.{places}f}%"
