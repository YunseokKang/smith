"""Per-person letters (FR-51, report design §42, user decision 2026-11-08): each member's letter is the household's
report seen from that member.

Every rule Smith has was written for the client, ledger owner "self" (the pension credit, the overseas deduction, the
loans, the holdings, the proposals), while household figures add up every owner. Seen from the spouse, the ledger is
read with the two swapped: the spouse becomes "self" and the client "partner", in the records, the sales and the
net worth by owner, and the spouse's own [household] facts take the client's keys. Every personal rule then runs for
the spouse unchanged, and every household figure stays the same. Nothing is written back: the swap lives in one
report build.

The spouse's proposals keep their own history (`PARTNER_SCOPE` on key and identity), so neither letter counts the
other's showings or decisions.
"""
from dataclasses import replace
from typing import Any

from smith.payload import LedgerView, owner_aliases

AUDIENCES = ("self", "partner")
PARTNER_SCOPE = "partner:"
# The spouse's letters' run state in daily_runs (smith.partner_letter), also read for what her letters already said.
PARTNER_PRODUCT = "report-partner"
# A member's own [household] facts, swapped with the client's when the letter is seen from that member (the
# household's facts, such as the retirement target and the reserve, are shared and stay as they are).
_OWN_KEYS = ("birth_year", "birth_month", "annual_gross_salary", "occupational_plan", "occupational_institution",
             "occupational_average_wage", "occupational_service_start")


def as_seen_by(view: LedgerView, audience: str) -> LedgerView:
    """The view with `audience` and the client swapped (records, sales, net worth by owner, owner aliases)."""
    if audience == "self":
        return view
    swap = {audience: "self", "self": audience}
    records = [replace(r, owner_id=swap.get(r.owner_id, r.owner_id)) for r in view.records]
    sales = [replace(s, owner_id=swap.get(s.owner_id, s.owner_id)) for s in view.sales]
    summary = replace(view.summary, net_worth_by_owner={swap.get(k, k): v for k, v in view.summary.net_worth_by_owner.items()})
    seen = replace(view, records=records, sales=sales, summary=summary)
    seen.owner_aliases = owner_aliases(seen)
    return seen


def household_for(household: dict[str, Any] | None, audience: str) -> dict[str, Any] | None:
    """[household] seen from `audience`: the member's own facts (`partner_birth_year`, …) under the client's keys,
    and the client's under the member's. Shared facts are unchanged."""
    if audience == "self" or household is None:
        return household
    prefix = f"{audience}_"
    seen = {k: v for k, v in household.items() if k not in _OWN_KEYS and not (k.startswith(prefix) and k[len(prefix):] in _OWN_KEYS)}
    for key in _OWN_KEYS:
        if household.get(f"{prefix}{key}") is not None:
            seen[key] = household[f"{prefix}{key}"]
        if household.get(key) is not None:
            seen[f"{prefix}{key}"] = household[key]
    return seen


def proposals_for(active: dict[str, dict[str, Any]], audience: str) -> dict[str, dict[str, Any]]:
    """The proposal history one letter reads: the client's own, or the spouse's under PARTNER_SCOPE (unprefixed)."""
    if audience == "self":
        return {identity: item for identity, item in active.items() if not identity.startswith(PARTNER_SCOPE)}
    return {identity[len(PARTNER_SCOPE):]: item for identity, item in active.items() if identity.startswith(PARTNER_SCOPE)}


def scoped(shown: list[dict[str, Any]], audience: str) -> list[dict[str, Any]]:
    """The proposals a letter carried, keyed for the ledger's history: the spouse's under PARTNER_SCOPE, so a
    client's decision on a key never lands on the spouse's instance."""
    if audience == "self":
        return shown
    return [{**item, "key": PARTNER_SCOPE + item["key"], "identity": PARTNER_SCOPE + item["identity"]} for item in shown]
