"""Financial record types shared by the importer and the ledger."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum


def canonical_decimal(text: str) -> str:
    """Return a decimal in canonical text form (no exponent, no trailing zeros, no negative zero).

    Raises:
        ValueError: `text` is not a finite decimal.
    """
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError("not a decimal") from None
    if not number.is_finite():
        raise ValueError("not a finite decimal")
    return "0" if number.is_zero() else format(number.normalize(), "f")


class Kind(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    CASHFLOW = "cashflow"
    GOAL = "goal"


class Status(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class ChangeType(StrEnum):
    UPDATE = "update"
    CORRECTION = "correction"


class Action(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    CORRECTED = "corrected"
    CLOSED = "closed"
    UNCHANGED = "unchanged"


# Currencies Smith can value; extend together with an FX source.
CURRENCIES = frozenset({"KRW", "USD", "EUR", "JPY", "CNY"})

# Economic asset type. The account wrapper (tax treatment, withdrawal limits) is a separate axis.
ASSET_CATEGORIES = frozenset({
    "cash", "deposit", "installment_savings", "stock", "fund", "bond", "real_estate", "lease_deposit",
    "insurance_surrender_value", "crypto", "unclassified", "other",
})
ACCOUNT_TYPES = frozenset({
    "bank", "brokerage", "isa", "pension_savings", "irp", "dc", "insurance", "crypto_exchange", "none", "other",
})
VALUATION_METHODS = frozenset({"manual", "statement", "market", "appraisal", "api"})
LIQUIDITY_CLASSES = frozenset({"immediate", "days", "months", "restricted"})
MARKETS = frozenset({"KR", "US", "other"})
OCCUPANCY = frozenset({"owner_occupied", "leased_out", "vacant", "other"})
# Who operates the holding. Hermes-managed assets are run by a separate agent and are not Smith's to sell.
MANAGERS = frozenset({"self", "hermes", "other"})

LIABILITY_CATEGORIES = frozenset({
    "mortgage", "jeonse_loan", "credit_loan", "credit_line", "card_balance", "policy_loan",
    "lease_deposit_obligation", "other",
})
# "unknown" records a loan whose terms are not yet confirmed instead of leaving it out.
RATE_TYPES = frozenset({"fixed", "variable", "mixed", "unknown"})
REPAYMENT_METHODS = frozenset({"bullet", "equal_payment", "equal_principal", "revolving", "other", "unknown"})

INFLOW_CATEGORIES = frozenset({
    "salary", "business_income", "rental_income", "pension_income", "investment_income", "other_income",
})
OUTFLOW_CATEGORIES = frozenset({
    "living_expense", "housing_cost", "loan_payment", "insurance_premium", "tax", "education",
    "other_expense",
})
# Moving money between the user's own accounts is neither income nor spending.
TRANSFER_CATEGORIES = frozenset({"internal_transfer"})
FREQUENCIES = frozenset({"once", "monthly", "quarterly", "annual"})
# Whether an outflow can be stopped at will (an extra loan prepayment) or is contractual.
COMMITMENTS = frozenset({"fixed", "discretionary"})

GOAL_CATEGORIES = frozenset({
    "home", "emergency_fund", "retirement", "education", "major_purchase", "debt_repayment", "other",
})
PRIORITIES = frozenset({"high", "medium", "low"})

# Fields that point at another record, with the kind they must point at.
REFERENCE_FIELDS = {"collateral_record_id": Kind.ASSET, "liability_record_id": Kind.LIABILITY}


@dataclass(frozen=True)
class RecordInput:
    """One validated revision of a record as submitted by an import.

    `fields` holds the kind-specific values in canonical text form: decimals without
    exponent or trailing zeros, dates as YYYY-MM-DD, and None for absent optional values.
    """

    record_id: str
    kind: Kind
    owner_id: str
    effective_at: datetime
    revision: int
    status: Status
    change_type: ChangeType
    corrects_revision: int | None
    reason: str | None
    fields: dict[str, str | None]


@dataclass(frozen=True)
class Observation:
    """A reference measurement that is not part of net worth, such as broker buying power or an FX rate.

    For an FX rate, `subject` is the base currency and `currency` the quote currency:
    1 `subject` = `value` `currency`.
    """

    subject: str
    metric: str
    currency: str
    value: str
    observed_at: datetime


@dataclass(frozen=True)
class ImportBatch:
    """Records to apply atomically. In snapshot mode the ledger assigns revisions itself and
    treats the batch as the complete set of active records for its owners and source."""

    import_id: str
    source: str
    mode: str
    as_of: datetime
    owner_ids: tuple[str, ...]
    records: tuple[RecordInput, ...]
    payload_sha256: str
    observations: tuple[Observation, ...] = ()


@dataclass(frozen=True)
class StoredRevision:
    record: RecordInput
    source: str
    recorded_at: datetime
    import_id: str


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    outcome: str  # "applied", "dry_run" or "already_imported"
    actions: tuple[tuple[str, int, Action], ...]


class ImportRejected(Exception):
    """An import failed validation; nothing was written."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{len(problems)} problem(s)")
        self.problems = problems
