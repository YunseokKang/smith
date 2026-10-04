"""Parse and validate manual JSON imports (data contract v1) without touching storage."""
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

from smith.records import (
    ACCOUNT_TYPES, ASSET_CATEGORIES, CURRENCIES, FREQUENCIES, GOAL_CATEGORIES, INFLOW_CATEGORIES,
    LIABILITY_CATEGORIES, LIQUIDITY_CLASSES, OUTFLOW_CATEGORIES, PRIORITIES, RATE_TYPES,
    REPAYMENT_METHODS, TRANSFER_CATEGORIES, VALUATION_METHODS, ChangeType, ImportBatch, ImportRejected,
    Kind, RecordInput, Status,
)

SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# API adapters build their own batches; files may only claim a manual source.
_MANUAL_SOURCE = re.compile(r"manual(-[a-z0-9]+)*")
# Bounded digits keep every value exact within the default 28-digit Decimal context.
_DECIMAL = re.compile(r"-?\d{1,15}(\.\d{1,8})?")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_REASON_MAX = 200

_ENVELOPE_KEYS = frozenset({"schema_version", "import_id", "source", "mode", "as_of", "owners", "records"})
_COMMON_KEYS = frozenset({
    "id", "kind", "owner_id", "effective_at", "revision", "status", "change_type",
    "corrects_revision", "reason",
})


class _Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, path: str, message: str) -> None:
        self.items.append(f"{path}: {message}")


Parser = Callable[[Any, str, _Problems], str | None]


@dataclass(frozen=True)
class _Field:
    parse: Parser
    nullable: bool = False


def _enum(allowed: frozenset[str]) -> Parser:
    def parse(value: Any, path: str, problems: _Problems) -> str | None:
        if isinstance(value, str) and value in allowed:
            return value
        problems.add(path, f"must be one of: {', '.join(sorted(allowed))}")
        return None
    return parse


def _decimal(*, positive: bool = False, below_one: bool = False) -> Parser:
    def parse(value: Any, path: str, problems: _Problems) -> str | None:
        if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
            problems.add(path, 'must be a decimal string such as "1000000" or "0.045"')
            return None
        # A sign is rejected even on zero ("-0") so that the input never states a negative amount.
        if value.startswith("-"):
            problems.add(path, "must not be negative")
            return None
        number = Decimal(value)
        if positive and number == 0:
            problems.add(path, "must be greater than zero")
            return None
        if below_one and number >= 1:
            problems.add(path, "must be a fraction below 1 (0.045 means 4.5%)")
            return None
        return "0" if number.is_zero() else format(number.normalize(), "f")
    return parse


def _currency(value: Any, path: str, problems: _Problems) -> str | None:
    if isinstance(value, str) and value in CURRENCIES:
        return value
    problems.add(path, f"must be a supported currency: {', '.join(sorted(CURRENCIES))}")
    return None


def _reference(value: Any, path: str, problems: _Problems) -> str | None:
    return _identifier(value, path, problems)


def _date(value: Any, path: str, problems: _Problems) -> str | None:
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    problems.add(path, "must be a date in YYYY-MM-DD format")
    return None


_KIND_FIELDS: dict[Kind, dict[str, _Field]] = {
    Kind.ASSET: {
        "category": _Field(_enum(ASSET_CATEGORIES)),
        "account_type": _Field(_enum(ACCOUNT_TYPES)),
        "currency": _Field(_currency),
        "value": _Field(_decimal()),
        "valuation_method": _Field(_enum(VALUATION_METHODS)),
        "liquidity": _Field(_enum(LIQUIDITY_CLASSES)),
    },
    Kind.LIABILITY: {
        "category": _Field(_enum(LIABILITY_CATEGORIES)),
        "currency": _Field(_currency),
        "outstanding_principal": _Field(_decimal()),
        "annual_rate": _Field(_decimal(below_one=True)),
        "rate_type": _Field(_enum(RATE_TYPES)),
        "maturity": _Field(_date, nullable=True),
        "repayment_method": _Field(_enum(REPAYMENT_METHODS)),
        # A credit line's limit is a separate figure from the amount actually drawn.
        "credit_limit": _Field(_decimal(), nullable=True),
        "collateral_record_id": _Field(_reference, nullable=True),
    },
    Kind.CASHFLOW: {
        "direction": _Field(_enum(frozenset({"inflow", "outflow"}))),
        "category": _Field(_enum(INFLOW_CATEGORIES | OUTFLOW_CATEGORIES | TRANSFER_CATEGORIES)),
        "currency": _Field(_currency),
        "amount": _Field(_decimal(positive=True)),
        "frequency": _Field(_enum(FREQUENCIES)),
        "start_date": _Field(_date),
        "end_date": _Field(_date, nullable=True),
        "liability_record_id": _Field(_reference, nullable=True),
    },
    Kind.GOAL: {
        "category": _Field(_enum(GOAL_CATEGORIES)),
        "currency": _Field(_currency),
        "target_amount": _Field(_decimal(positive=True)),
        "target_date": _Field(_date),
        "priority": _Field(_enum(PRIORITIES)),
    },
}


def load_import_file(path: Path) -> ImportBatch:
    """Read a UTF-8 JSON import file; a byte order mark from Windows editors is accepted."""
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportRejected(["file: must be UTF-8 encoded JSON"]) from None
    return parse_import(text)


def parse_import(text: str) -> ImportBatch:
    """Validate an import document and return a batch, or raise ImportRejected with every problem."""
    try:
        document = json.loads(text, parse_float=_reject_float, parse_constant=_reject_constant,
                              object_pairs_hook=_unique_keys)
    except ValueError as error:
        raise ImportRejected([f"file: invalid JSON ({error})"]) from None
    if not isinstance(document, dict):
        raise ImportRejected(["file: top level must be a JSON object"])
    problems = _Problems()
    for key in sorted(document.keys() - _ENVELOPE_KEYS):
        problems.add(key, "unknown field")
    if type(document.get("schema_version")) is not int or document["schema_version"] != SCHEMA_VERSION:
        problems.add("schema_version", f"must be {SCHEMA_VERSION}")
    import_id = _identifier(document.get("import_id"), "import_id", problems)
    source = document.get("source")
    if not isinstance(source, str) or not _MANUAL_SOURCE.fullmatch(source):
        problems.add("source", 'must be "manual" or start with "manual-" (lowercase letters and digits)')
    mode = document.get("mode")
    if mode == "snapshot":
        problems.add("mode", "snapshot mode is not supported yet; use patch")
    elif mode != "patch":
        problems.add("mode", "must be patch")
    as_of = _timestamp(document.get("as_of"), "as_of", problems)
    owner_ids = _owners(document.get("owners"), problems)
    records = _records(document.get("records"), set(owner_ids), problems)
    if problems.items:
        raise ImportRejected(problems.items)
    return ImportBatch(import_id, source, mode, as_of, tuple(owner_ids), tuple(records), _sha256(document))


def _reject_float(literal: str) -> NoReturn:
    raise ValueError("numbers with a fraction or exponent are not allowed; use decimal strings")


def _reject_constant(literal: str) -> NoReturn:
    raise ValueError(f"{literal} is not allowed")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _sha256(document: dict[str, Any]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _identifier(value: Any, path: str, problems: _Problems) -> str | None:
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
        return value
    problems.add(path, "must be 1-64 ASCII letters, digits, '.', '_' or '-', starting with a letter or digit")
    return None


def _timestamp(value: Any, path: str, problems: _Problems) -> datetime | None:
    if isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed
    problems.add(path, "must be an ISO 8601 timestamp with a UTC offset, e.g. 2026-09-27T09:00:00+09:00")
    return None


def _positive_int(value: Any, path: str, problems: _Problems) -> int | None:
    if type(value) is int and value >= 1:
        return value
    problems.add(path, "must be a positive integer")
    return None


def _owners(value: Any, problems: _Problems) -> list[str]:
    if not isinstance(value, list) or not value:
        problems.add("owners", "must be a non-empty list")
        return []
    owner_ids: list[str] = []
    for index, owner in enumerate(value):
        path = f"owners[{index}]"
        if not isinstance(owner, dict) or owner.keys() != {"id"}:
            problems.add(path, 'must be an object with only "id"')
            continue
        owner_id = _identifier(owner["id"], f"{path}.id", problems)
        if owner_id in owner_ids:
            problems.add(f"{path}.id", "duplicate owner id")
        elif owner_id:
            owner_ids.append(owner_id)
    return owner_ids


def _records(value: Any, owner_ids: set[str], problems: _Problems) -> list[RecordInput]:
    if not isinstance(value, list) or not value:
        problems.add("records", "must contain at least one record")
        return []
    records: list[RecordInput] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        record = _record(raw, f"records[{index}]", owner_ids, problems)
        if record is None:
            continue
        if record.record_id in seen:
            problems.add(f"records[{index}].id", "duplicate record id in this import")
        seen.add(record.record_id)
        records.append(record)
    return records


def _record(raw: Any, path: str, owner_ids: set[str], problems: _Problems) -> RecordInput | None:
    if not isinstance(raw, dict):
        problems.add(path, "must be an object")
        return None
    before = len(problems.items)
    try:
        kind: Kind | None = Kind(raw.get("kind"))
    except (TypeError, ValueError):
        kind = None
        problems.add(f"{path}.kind", f"must be one of: {', '.join(k.value for k in Kind)}")
    record_id = _identifier(raw.get("id"), f"{path}.id", problems)
    owner_id = raw.get("owner_id")
    if not isinstance(owner_id, str) or owner_id not in owner_ids:
        problems.add(f"{path}.owner_id", "must match an id declared in owners")
    effective_at = _timestamp(raw.get("effective_at"), f"{path}.effective_at", problems)
    revision = _positive_int(raw.get("revision"), f"{path}.revision", problems)
    status = _enum(frozenset(Status))(raw.get("status"), f"{path}.status", problems)
    change_type = _enum(frozenset(ChangeType))(raw.get("change_type", "update"), f"{path}.change_type", problems)
    corrects, reason = _change_details(raw, path, change_type, revision, problems)
    # Without a valid kind the field set is unknown, but common-field problems are still reported.
    fields: dict[str, str | None] = {}
    if kind is not None:
        specs = _KIND_FIELDS[kind]
        for key in sorted(raw.keys() - _COMMON_KEYS - specs.keys()):
            problems.add(f"{path}.{key}", "unknown field")
        fields = _kind_fields(raw, specs, path, closing=status == Status.CLOSED, problems=problems)
        if kind is Kind.CASHFLOW:
            _check_cashflow(fields, path, problems)
    if len(problems.items) > before:
        return None
    return RecordInput(record_id, kind, owner_id, effective_at, revision, Status(status),
                       ChangeType(change_type), corrects, reason, fields)


def _kind_fields(raw: dict[str, Any], specs: dict[str, _Field], path: str, *, closing: bool,
                 problems: _Problems) -> dict[str, str | None]:
    """Parse kind-specific fields. A closing revision may omit them; values present are still checked."""
    fields: dict[str, str | None] = {}
    for name, spec in specs.items():
        value = raw.get(name)
        if value is None:
            if name in raw and not spec.nullable:
                problems.add(f"{path}.{name}", "must not be null")
            elif not spec.nullable and not closing:
                problems.add(f"{path}.{name}", "is required")
            elif spec.nullable:
                fields[name] = None
            continue
        fields[name] = spec.parse(value, f"{path}.{name}", problems)
    return fields


def _change_details(raw: dict[str, Any], path: str, change_type: str | None, revision: int | None,
                    problems: _Problems) -> tuple[int | None, str | None]:
    reason = raw.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_MAX):
        problems.add(f"{path}.reason", f"must be non-empty text of at most {_REASON_MAX} characters")
    if change_type != ChangeType.CORRECTION:
        if "corrects_revision" in raw:
            problems.add(f"{path}.corrects_revision", "is only allowed when change_type is correction")
        return None, reason
    if reason is None:
        problems.add(f"{path}.reason", "is required for a correction")
    corrects = _positive_int(raw.get("corrects_revision"), f"{path}.corrects_revision", problems)
    if corrects is not None and revision is not None and corrects >= revision:
        problems.add(f"{path}.corrects_revision", "must refer to an earlier revision")
    return corrects, reason


def _check_cashflow(fields: dict[str, str | None], path: str, problems: _Problems) -> None:
    direction, category = fields.get("direction"), fields.get("category")
    if category in INFLOW_CATEGORIES and direction == "outflow":
        problems.add(f"{path}.direction", f"must be inflow for category {category}")
    if category in OUTFLOW_CATEGORIES and direction == "inflow":
        problems.add(f"{path}.direction", f"must be outflow for category {category}")
    # Linking repayments to their loan lets later stages split principal from interest.
    linked = fields.get("liability_record_id") is not None
    if category == "loan_payment" and not linked:
        problems.add(f"{path}.liability_record_id", "is required for category loan_payment")
    elif category is not None and category != "loan_payment" and linked:
        problems.add(f"{path}.liability_record_id", "is only allowed for category loan_payment")
    start, end = fields.get("start_date"), fields.get("end_date")
    if fields.get("frequency") == "once" and end is not None:
        problems.add(f"{path}.end_date", "must be omitted for a one-time cash flow")
    elif start and end and end < start:
        problems.add(f"{path}.end_date", "must not be before start_date")
