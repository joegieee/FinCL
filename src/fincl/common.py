"""Deterministic helpers shared by the CSV and document import services.

These produce persisted identifiers, timestamps, and canonical JSON. Both
services must produce byte-identical output, so the one definition lives here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from .errors import FinCLError

# The closed canonical vocabulary for ``ledger_entries.event_kind``. Both import
# paths normalize into this set; anything unrecognized becomes ``OTHER`` rather
# than leaking a raw source label. ``db.py`` enforces it as a CHECK.
LEDGER_EVENT_KINDS = frozenset(
    {"CARD_PAYMENT", "REFUND", "TOPUP", "TRANSFER", "EXCHANGE", "PAYMENT", "OTHER"}
)

# ---------------------------------------------------------------------------
# Categorization taxonomy (Phase 4 Track A, Confirmed 28; DESIGN-categorization
# §3). The taxonomy is a versioned code constant, never a table: editing it is a
# code change plus a ``TAXONOMY_VERSION`` bump, and a changed version restates
# categorized history on the next ``fincl categorize`` / rebuild, exactly like a
# parser-profile bump. Rules and overrides name a category by its
# ``"Group / Category"`` string.
# ---------------------------------------------------------------------------

TAXONOMY_VERSION = "taxonomy-2026-v1"

# Two levels: group -> ordered categories. Ordering is presentation only; the
# ``"Group / Category"`` string is the identity a rule or override binds to.
CATEGORY_TAXONOMY: dict[str, tuple[str, ...]] = {
    "Food & Dining": ("Groceries", "Dining Out", "Cafes & Drinks", "Hawker & Canteen"),
    "Transport": ("Public Transit", "Taxi & Ride-hail", "Fuel", "Parking & Tolls"),
    "Housing": ("Rent", "Utilities", "Internet & Mobile", "Home Supplies & Maintenance"),
    "Shopping": ("Clothing & Accessories", "Electronics", "Books & Stationery", "General Shopping"),
    "Health": ("Medical & Dental", "Pharmacy", "Fitness"),
    "Personal & Household": ("Personal Care", "Laundry", "Household Services"),
    "Education": ("Tuition & Exam Fees", "Course Materials", "Education Services"),
    "Leisure": ("Entertainment", "Hobbies & Sports"),
    "Travel": ("Transport", "Accommodation", "Activities", "Travel Fees"),
    "Financial & Government": ("Bank & Card Fees", "Interest & Financing", "Taxes & Government"),
    "Giving": ("Charity", "Gifts Given"),
    "Income & Inflow": (
        "Earned Income",
        "Reimbursement",
        "Gift Received",
        "Rewards & Cashback",
        "Other Inflow",
    ),
    "Cash": ("Cash Withdrawal — Purpose Unknown",),
    # ``Internal`` is auto-assigned from confirmed links and inbound
    # classifications; a rule may never assign it (enforced by the rule authoring
    # surface, not here).
    "Internal": ("Transfer", "Card Repayment", "Currency Exchange", "Wallet Top-up"),
    "Unclassified": ("Unclassified",),
}

# Retired ``"Group / Category"`` string -> its replacement (possibly
# ``"Unclassified / Unclassified"``). A rule or override naming a migrated
# category still loads — it resolves to the target and is flagged ``migrated`` so
# the operator can rewrite it — while an unknown string stays a blocking error.
# This is what stops the first category rename from making existing journal rules
# unloadable and deadlocking the upgrade (DESIGN-categorization §3.1).
CATEGORY_MIGRATIONS: dict[str, str] = {}

# The category strings released in the *previous* taxonomy version. On a taxonomy
# bump, snapshot the outgoing ``category_strings()`` here so the CI invariant can
# check that every removed category has a migration entry.
_PREVIOUS_TAXONOMY_STRINGS: frozenset[str] = frozenset()

_CATEGORY_SEPARATOR = " / "
INTERNAL_GROUP = "Internal"
UNCLASSIFIED_CATEGORY = "Unclassified" + _CATEGORY_SEPARATOR + "Unclassified"


def format_category(group: str, category: str) -> str:
    return f"{group}{_CATEGORY_SEPARATOR}{category}"


def category_strings() -> frozenset[str]:
    """Every current ``"Group / Category"`` string."""
    return frozenset(
        format_category(group, category)
        for group, categories in CATEGORY_TAXONOMY.items()
        for category in categories
    )


def resolve_category(name: str) -> tuple[str, str, str | None]:
    """Resolve a ``"Group / Category"`` string to ``(group, category, migrated_from)``.

    ``migrated_from`` is ``None`` for a current category and the original string
    when ``name`` names a category retired via ``CATEGORY_MIGRATIONS``. An
    unknown string raises — a typo is refused, never silently dropped.
    """
    current = category_strings()
    if name in current:
        group, category = name.split(_CATEGORY_SEPARATOR, 1)
        return group, category, None
    target = CATEGORY_MIGRATIONS.get(name)
    if target is not None:
        if target not in current:
            raise FinCLError(
                f"category migration target {target!r} is not a current category"
            )
        group, category = target.split(_CATEGORY_SEPARATOR, 1)
        return group, category, name
    raise FinCLError(f"unknown category {name!r} (not in {TAXONOMY_VERSION} or its migrations)")


def stable_id(prefix: str, *parts: str) -> str:
    """A rebuild-stable id: ``<prefix>_<sha256 of NUL-joined parts>``."""
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def utc_now() -> str:
    """Current UTC instant, ISO-8601 with microsecond precision."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def canonical_json(value: object) -> str:
    """Compact, key-sorted JSON for stored context and evidence blobs."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
