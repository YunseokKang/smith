"""Financial record types shared by the importer and the ledger."""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


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

LIABILITY_CATEGORIES = frozenset({
    "mortgage", "jeonse_loan", "credit_loan", "credit_line", "card_balance", "policy_loan",
    "lease_deposit_obligation", "other",
})
RATE_TYPES = frozenset({"fixed", "variable", "mixed"})
REPAYMENT_METHODS = frozenset({"bullet", "equal_payment", "equal_principal", "revolving", "other"})

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
class ImportBatch:
    import_id: str
    source: str
    mode: str
    as_of: datetime
    owner_ids: tuple[str, ...]
    records: tuple[RecordInput, ...]
    payload_sha256: str


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
