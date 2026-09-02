"""Phase 4 Track A — the categorization engine and run manifest.

``fincl categorize`` recomputes ``merchant_observations``, then assigns every
entry in the selected set exactly one ``(group, category)`` by the §13.4
precedence ladder, and records a ``categorization_runs`` manifest row whose id is
a hash of every input version — so two runs with the same id produced the same
categories (§5). Nothing is ever left without a category: a direction conflict,
an ambiguous tier, or a NULL description falls through to ``Unclassified`` and is
flagged for review.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .categorization import canonical_match
from .common import (
    TAXONOMY_VERSION,
    UNCLASSIFIED_CATEGORY,
    canonical_json,
    utc_now,
)
from .decisions import replay_journal
from .merchant import (
    MERCHANT_NORM_VERSION,
    PROJECTION_VERSION,
    recompute_merchant_observations,
)
from .money import ExactMoney, compare
from .selection import recompute_selection

ENGINE_VERSION = "category-engine-2026-v1"
PRECEDENCE_POLICY = "precedence-2026-v1"
LINK_PURPOSE_VERSION = "link-purpose-2026-v1"
DIRECTION_POLICY_VERSION = "direction-policy-2026-v1"

# Account kinds the link-purpose derivation keys on when a confirmed
# SAME_CURRENCY_TRANSFER carries no explicit purpose (§6.2).
_CREDIT_CARD_ACCOUNTS = frozenset({"mari-credit-card"})
_WALLET_ACCOUNTS = frozenset({"dbs-paylah-wallet", "acct_revolut_current"})

_INBOUND_CATEGORY: dict[str, tuple[str, str]] = {
    "income": ("Income & Inflow", "Earned Income"),
    "reimbursement": ("Income & Inflow", "Reimbursement"),
    "gift": ("Income & Inflow", "Gift Received"),
    "family_transfer": ("Income & Inflow", "Gift Received"),
    "other": ("Income & Inflow", "Other Inflow"),
    "self_transfer": ("Internal", "Transfer"),
}
_INTERNAL_CATEGORY: dict[str, tuple[str, str]] = {
    "transfer": ("Internal", "Transfer"),
    "card_repayment": ("Internal", "Card Repayment"),
    "wallet_topup": ("Internal", "Wallet Top-up"),
    "currency_exchange": ("Internal", "Currency Exchange"),
}

_UNCLASSIFIED = tuple(UNCLASSIFIED_CATEGORY.split(" / ", 1))


def direction_compatible(group_name: str, economic_direction: str) -> bool:
    """The one real constraint (§6.4): an inflow category on an outflow entry."""
    return not (group_name == "Income & Inflow" and economic_direction == "OUT")


# --------------------------------------------------------------------------- #
# Amount bands
# --------------------------------------------------------------------------- #


def _band_holds(band: dict, currency: str, amount: ExactMoney) -> bool:
    if band["currency"] != currency:
        return False
    magnitude = ExactMoney(abs(amount.coefficient), amount.scale, amount.text)
    if "min" in band:
        lo = ExactMoney(band["min"]["coefficient"], band["min"]["scale"], "")
        if compare(lo, magnitude) > 0:
            return False
    if "max" in band:
        hi = ExactMoney(band["max"]["coefficient"], band["max"]["scale"], "")
        if compare(magnitude, hi) > 0:
            return False
    return True


def _band_width_key(band: dict | None) -> tuple[int, int]:
    """Sort key: banded before unbanded, then narrower before wider."""
    if band is None:
        return (1, 0)  # unbanded ranks last
    if "min" not in band or "max" not in band:
        return (0, 1 << 62)  # an open end is wider than any closed band
    scale = max(band["min"]["scale"], band["max"]["scale"])
    lo = band["min"]["coefficient"] * 10 ** (scale - band["min"]["scale"])
    hi = band["max"]["coefficient"] * 10 ** (scale - band["max"]["scale"])
    return (0, hi - lo)


# --------------------------------------------------------------------------- #
# Rule ranking within the winning tier (§6.3)
# --------------------------------------------------------------------------- #

_PREDICATE_RANK = {"paynow_to": 0, "description_prefix": 1, "description_contains": 2}


def _rule_sort_key(rule: _Rule) -> tuple:
    match = rule.match
    predicate = next(k for k in match if k in _PREDICATE_RANK)
    return (
        _PREDICATE_RANK[predicate],           # recipient > prefix > contains
        -len(str(match[predicate])),           # longer matched literal first
        0 if "account" in match else 1,        # account-qualified first
        _band_width_key(match.get("amount_band")),
        rule.rule_id,                          # final deterministic tiebreak
    )


def _merchant_rule_sort_key(rule: _Rule) -> tuple:
    return (_band_width_key(rule.match.get("amount_band")), rule.rule_id)


class _Rule:
    __slots__ = ("rule_id", "match", "group_name", "category")

    def __init__(self, rule_id: str, match_json: str, group_name: str, category: str) -> None:
        self.rule_id = rule_id
        self.match = canonical_match(json.loads(match_json))
        self.group_name = group_name
        self.category = category


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


def categorize(connection: sqlite3.Connection, *, journal_path: Path | None = None) -> str:
    """Recompute the categorization projection; return the ``categorization_runs`` id."""
    replay_journal(connection, journal_path)
    recompute_selection(connection)
    recompute_merchant_observations(connection)

    rules = [
        _Rule(row["rule_id"], row["match_json"], row["group_name"], row["category"])
        for row in connection.execute(
            "SELECT rule_id, match_json, group_name, category FROM category_rules"
        )
    ]
    merchant_rules = [r for r in rules if "merchant_canonical" in r.match]
    tier5_rules = [r for r in rules if "merchant_canonical" not in r.match]

    overrides = {
        row["entry_anchor"]: row
        for row in connection.execute(
            "SELECT * FROM category_overrides WHERE bound = 1"
        )
    }
    inbound = {
        row["anchor"]: row["classification"]
        for row in connection.execute(
            "SELECT anchor, classification FROM inbound_classifications WHERE bound = 1"
        )
    }
    link_category = _confirmed_link_categories(connection)

    entries = connection.execute(
        """
        SELECT e.id, e.natural_anchor, e.account_id, e.currency_pocket,
               e.amount_text, e.amount_coefficient, e.amount_scale,
               e.economic_direction, e.effective_at,
               o.description_text, o.paynow_to, o.merchant_normalized,
               o.merchant_canonical, o.alias_applied
        FROM ledger_entries e
        JOIN extraction_runs x
          ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED'
        JOIN merchant_observations o ON o.entry_id = e.id
        ORDER BY e.id
        """
    ).fetchall()

    run_id, manifest = _build_manifest(connection, entries)
    now = utc_now()
    with connection:
        connection.execute("DELETE FROM entry_categories")
        connection.execute("DELETE FROM categorization_runs WHERE id = ?", (run_id,))
        connection.execute(
            """
            INSERT INTO categorization_runs(
                id, engine_version, precedence_policy, rule_set_hash, override_set_hash,
                link_purpose_version, direction_policy_version, taxonomy_version,
                merchant_norm_version, projection_version, journal_seq, eligible_cutoff,
                eligible_entry_count, eligible_set_hash, profile_set_json,
                started_at, completed_at
            ) VALUES (:id, :engine_version, :precedence_policy, :rule_set_hash,
                      :override_set_hash, :link_purpose_version, :direction_policy_version,
                      :taxonomy_version, :merchant_norm_version, :projection_version,
                      :journal_seq, :eligible_cutoff, :eligible_entry_count,
                      :eligible_set_hash, :profile_set_json, :started_at, :completed_at)
            """,
            {**manifest, "id": run_id, "started_at": now, "completed_at": now},
        )
        for entry in entries:
            group, category, tier, basis, ambiguous, conflict = _classify_entry(
                entry, overrides, inbound, link_category, merchant_rules, tier5_rules
            )
            connection.execute(
                """
                INSERT INTO entry_categories(
                    entry_id, group_name, category, precedence_tier, basis,
                    ambiguous, direction_conflict, run_id, categorized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (entry["id"], group, category, tier, basis,
                 int(ambiguous), int(conflict), run_id, now),
            )
    return run_id


def _classify_entry(
    entry: sqlite3.Row,
    overrides: dict[str, sqlite3.Row],
    inbound: dict[str, str],
    link_category: dict[str, tuple[str, str, str]],
    merchant_rules: list[_Rule],
    tier5_rules: list[_Rule],
) -> tuple[str, str, int, str, bool, bool]:
    direction = entry["economic_direction"]
    amount = ExactMoney(entry["amount_coefficient"], entry["amount_scale"], entry["amount_text"])
    currency = entry["currency_pocket"]
    conflict = False

    # Tier 1 — manual override. Wins regardless of direction, but flags a cross.
    override = overrides.get(entry["natural_anchor"])
    if override is not None:
        group, category = override["group_name"], override["category"]
        return group, category, 1, "override", False, not direction_compatible(group, direction)

    # Tier 2 — confirmed relationship, then bound inbound classification.
    link = link_category.get(entry["id"])
    if link is not None:
        group, category, purpose = link
        return group, category, 2, f"link:{purpose}", False, False
    classification = inbound.get(entry["natural_anchor"])
    if classification is not None and classification in _INBOUND_CATEGORY:
        group, category = _INBOUND_CATEGORY[classification]
        return group, category, 2, f"inbound:{classification}", False, not direction_compatible(
            group, direction
        )

    # Tiers 3 / 4 — direct then alias-resolved merchant rules.
    for tier, want_alias, key_field in ((3, 0, "merchant_normalized"), (4, 1, "merchant_canonical")):
        target = entry[key_field]
        if target is None:
            continue
        matches = [
            rule
            for rule in merchant_rules
            if rule.match["merchant_canonical"] == target
            and int(entry["alias_applied"]) == want_alias
            and ("amount_band" not in rule.match
                 or _band_holds(rule.match["amount_band"], currency, amount))
        ]
        if not matches:
            continue
        compatible = [r for r in matches if direction_compatible(r.group_name, direction)]
        if not compatible:
            conflict = True
            continue
        compatible.sort(key=_merchant_rule_sort_key)
        winner = compatible[0]
        return (winner.group_name, winner.category, tier,
                f"rule:{winner.rule_id}" if tier == 3
                else f"alias:{entry['merchant_canonical']}:{winner.rule_id}",
                len(compatible) > 1, conflict)

    # Tier 5 — description / recipient rules.
    description = entry["description_text"]
    recipient = entry["paynow_to"]
    matches = []
    for rule in tier5_rules:
        if "amount_band" in rule.match and not _band_holds(
            rule.match["amount_band"], currency, amount
        ):
            continue
        if "paynow_to" in rule.match:
            if recipient is not None and recipient == rule.match["paynow_to"]:
                matches.append(rule)
        elif "description_prefix" in rule.match:
            if description is not None and description.startswith(rule.match["description_prefix"]):
                matches.append(rule)
        elif "description_contains" in rule.match:
            account_ok = (
                "account" not in rule.match or rule.match["account"] == entry["account_id"]
            )
            if (
                description is not None
                and rule.match["description_contains"] in description
                and account_ok
            ):
                matches.append(rule)
    if matches:
        compatible = [r for r in matches if direction_compatible(r.group_name, direction)]
        if compatible:
            compatible.sort(key=_rule_sort_key)
            winner = compatible[0]
            return (winner.group_name, winner.category, 5, f"rule:{winner.rule_id}",
                    len(compatible) > 1, conflict)
        conflict = True

    # Tier 6 — unclassified.
    return _UNCLASSIFIED[0], _UNCLASSIFIED[1], 6, "unclassified", False, conflict


def _confirmed_link_categories(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, str, str]]:
    """entry_id -> (group, category, purpose_label) for both legs of every confirmed link."""
    out: dict[str, tuple[str, str, str]] = {}
    rows = connection.execute(
        """
        SELECT tl.left_entry_id, tl.right_entry_id, tl.link_kind,
               lel.economic_direction AS left_dir, lel.account_id AS left_acct,
               ler.economic_direction AS right_dir, ler.account_id AS right_acct,
               ld.link_purpose
        FROM transfer_links tl
        JOIN ledger_entries lel ON lel.id = tl.left_entry_id
        JOIN ledger_entries ler ON ler.id = tl.right_entry_id
        LEFT JOIN link_decisions ld
          ON ld.left_anchor = lel.natural_anchor
         AND ld.right_anchor = ler.natural_anchor
         AND ld.decision = 'CONFIRM'
        WHERE tl.status = 'CONFIRMED'
        """
    ).fetchall()
    for row in rows:
        if row["link_kind"] == "CURRENCY_EXCHANGE":
            purpose = "currency_exchange"
        elif row["link_purpose"] is not None:
            purpose = row["link_purpose"]
        else:
            receiving_acct = (
                row["right_acct"] if row["right_dir"] == "IN" else row["left_acct"]
            )
            if receiving_acct in _CREDIT_CARD_ACCOUNTS:
                purpose = "card_repayment"
            elif receiving_acct in _WALLET_ACCOUNTS:
                purpose = "wallet_topup"
            else:
                purpose = "transfer"
        group, category = _INTERNAL_CATEGORY[purpose]
        out[row["left_entry_id"]] = (group, category, purpose)
        out[row["right_entry_id"]] = (group, category, purpose)
    return out


def _build_manifest(
    connection: sqlite3.Connection, entries: list[sqlite3.Row]
) -> tuple[str, dict[str, object]]:
    rule_rows = sorted(
        tuple(r)
        for r in connection.execute(
            "SELECT canonical AS anchor, record_hash FROM merchant_aliases "
            "UNION ALL SELECT rule_id, record_hash FROM category_rules"
        )
    )
    rule_set_hash = _blake(canonical_json(rule_rows))
    override_rows = sorted(
        tuple(r)
        for r in connection.execute(
            "SELECT entry_anchor, journal_seq, record_hash FROM category_overrides"
        )
    )
    override_set_hash = _blake(canonical_json(override_rows))

    eligible = sorted((row["id"], row["natural_anchor"]) for row in entries)
    eligible_set_hash = _blake(canonical_json(eligible))
    cutoff = max((row["effective_at"] for row in entries), default="")
    watermark = connection.execute(
        "SELECT applied_seq FROM decision_watermark WHERE id = 1"
    ).fetchone()[0]
    profile_pairs = sorted(
        (row["source_profile_version"], row["parser_version"])
        for row in connection.execute(
            "SELECT DISTINCT source_profile_version, parser_version FROM extraction_runs "
            "WHERE selection_state = 'SELECTED'"
        )
    )

    manifest: dict[str, object] = {
        "engine_version": ENGINE_VERSION,
        "precedence_policy": PRECEDENCE_POLICY,
        "rule_set_hash": rule_set_hash,
        "override_set_hash": override_set_hash,
        "link_purpose_version": LINK_PURPOSE_VERSION,
        "direction_policy_version": DIRECTION_POLICY_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "merchant_norm_version": MERCHANT_NORM_VERSION,
        "projection_version": PROJECTION_VERSION,
        "journal_seq": int(watermark),
        "eligible_cutoff": cutoff,
        "eligible_entry_count": len(entries),
        "eligible_set_hash": eligible_set_hash,
        "profile_set_json": canonical_json(profile_pairs),
    }
    run_id = "cat_" + _blake(canonical_json(manifest))
    return run_id, manifest


def _blake(text: str) -> str:
    return hashlib.blake2s(text.encode("utf-8"), digest_size=16).hexdigest()


def summarize_run(connection: sqlite3.Connection, run_id: str) -> dict[str, object]:
    """Counts for the ``fincl categorize`` summary and the report warnings (§7)."""
    by_group = {
        row["group_name"]: row["n"]
        for row in connection.execute(
            "SELECT group_name, count(*) AS n FROM entry_categories GROUP BY group_name"
        )
    }
    flags = connection.execute(
        """
        SELECT
          coalesce(sum(precedence_tier = 6), 0) AS unclassified,
          coalesce(sum(ambiguous = 1), 0) AS ambiguous,
          coalesce(sum(direction_conflict = 1), 0) AS direction_conflict
        FROM entry_categories
        """
    ).fetchone()
    migrated = connection.execute(
        "SELECT count(*) FROM entry_categories ec "
        "JOIN category_rules r ON ('rule:' || r.rule_id) = ec.basis "
        "WHERE r.migrated_from IS NOT NULL"
    ).fetchone()[0]
    migrated += connection.execute(
        "SELECT count(*) FROM category_overrides WHERE migrated_from IS NOT NULL AND bound = 1"
    ).fetchone()[0]
    missing_description = connection.execute(
        "SELECT count(*) FROM merchant_observations WHERE description_text IS NULL"
    ).fetchone()[0]
    alias_ambiguous = connection.execute(
        "SELECT count(*) FROM merchant_observations WHERE alias_ambiguous = 1"
    ).fetchone()[0]
    return {
        "run_id": run_id,
        "by_group": by_group,
        "unclassified": int(flags["unclassified"]),
        "ambiguous": int(flags["ambiguous"]),
        "direction_conflict": int(flags["direction_conflict"]),
        "migrated": int(migrated),
        "missing_description": int(missing_description),
        "alias_ambiguous": int(alias_ambiguous),
    }
