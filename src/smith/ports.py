"""Proposed adapter boundaries; concrete implementations are intentionally absent."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol


@dataclass(frozen=True)
class Position:
    asset_id: str
    owner_id: str
    account_id: str  # Opaque internal ID, not a broker account number.
    currency: str
    value: Decimal


@dataclass(frozen=True)
class Snapshot:
    source: str
    as_of: datetime  # Must be timezone-aware in concrete adapters.
    collected_at: datetime
    status: Literal["complete", "partial", "stale"]
    positions: tuple[Position, ...]


class AssetReader(Protocol):
    def fetch_snapshot(self) -> Snapshot: ...


class EvidenceRetriever(Protocol):
    def retrieve(self, query: str, *, as_of: datetime) -> list[dict]: ...


class Adviser(Protocol):
    def advise(self, question: str, sanitized_context: dict) -> str: ...


class ReportDelivery(Protocol):
    def send(self, report_id: str, body: str) -> str:
        """Use a trusted configured recipient; return provider delivery reference."""
        ...
