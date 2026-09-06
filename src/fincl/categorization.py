"""Phase 4 Track A — categorization rule/alias/override payload schemas.

Categorization rules are decision-journal kinds, not a table or a file
(Confirmed 28). This module holds the *pure* payload contract for the five
kinds — validation, the ``rule_id`` derivation, and amount-band
canonicalization — so both the authoring CLI and journal replay enforce the
same shape. The projection application lives in ``decisions.py`` (it needs
anchor resolution); the engine that consumes the projection is separate.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .common import canonical_json, resolve_category
from .errors import FinCLError

# Decision kinds this track introduces. ``CATEGORY_OVERRIDE`` keeps its
# REBUILD_STATE.md shape; the other four are new.
MERCHANT_ALIAS_SET = "MERCHANT_ALIAS_SET"
MERCHANT_ALIAS_RETIRE = "MERCHANT_ALIAS_RETIRE"
CATEGORY_RULE_SET = "CATEGORY_RULE_SET"
CATEGORY_RULE_RETIRE = "CATEGORY_RULE_RETIRE"
CATEGORY_OVERRIDE = "CATEGORY_OVERRIDE"

CATEGORIZATION_DECISION_KINDS = frozenset(
    {
        MERCHANT_ALIAS_SET,
        MERCHANT_ALIAS_RETIRE,
        CATEGORY_RULE_SET,
        CATEGORY_RULE_RETIRE,
        CATEGORY_OVERRIDE,
    }
)

# A short predicate/alias literal is the same hazard as matching a NULL
# description (§4.1): one rule quietly swallowing an unbounded share of the
# corpus. ``exact`` literals are exempt — they match one whole string.
MIN_LITERAL_LENGTH = 6

_MATCH_KEYS = frozenset(
    {"merchant_canonical", "description_prefix", "description_contains", "paynow_to"}
)


def _require(condition: object, message: str) -> None:
    if not condition:
        raise FinCLError(f"invalid categorization payload: {message}")


def _nonblank_str(value: Any, field: str) -> str:
    _require(isinstance(value, str) and value.strip() != "", f"{field} must be a non-empty string")
    return str(value)


def _is_normalized_literal(value: str) -> bool:
    """Uppercased and whitespace-collapsed — the shape ``merchant_normalized`` has."""
    return value == value.upper() and value == " ".join(value.split())


# --------------------------------------------------------------------------- #
# Amount bands (§2.3)
# --------------------------------------------------------------------------- #


def _canonical_amount(part: Any, field: str) -> dict[str, int]:
    _require(isinstance(part, dict), f"{field} must be an object")
    coefficient, scale = part.get("coefficient"), part.get("scale")
    _require(isinstance(coefficient, int) and not isinstance(coefficient, bool),
             f"{field}.coefficient must be an integer")
    _require(isinstance(scale, int) and not isinstance(scale, bool) and scale >= 0,
             f"{field}.scale must be a non-negative integer")
    _require(coefficient >= 0, f"{field}.coefficient must not be negative (bands do not encode direction)")
    # Reduce so 10, 10.0 and "10.00" all produce one rule_id.
    while scale > 0 and coefficient % 10 == 0:
        coefficient //= 10
        scale -= 1
    return {"coefficient": coefficient, "scale": scale}


def canonical_amount_band(band: Any) -> dict[str, Any]:
    """Validate and reduce an ``amount_band`` to its canonical, hashable form."""
    _require(isinstance(band, dict), "amount_band must be an object")
    currency = band.get("currency")
    _require(isinstance(currency, str) and len(currency) == 3 and currency.isupper(),
             "amount_band.currency is required and must be a 3-letter uppercase code")
    has_min, has_max = "min" in band, "max" in band
    _require(has_min or has_max, "amount_band needs at least one of min / max")
    out: dict[str, Any] = {"currency": currency}
    if has_min:
        out["min"] = _canonical_amount(band["min"], "amount_band.min")
    if has_max:
        out["max"] = _canonical_amount(band["max"], "amount_band.max")
    if has_min and has_max:
        lo, hi = out["min"], out["max"]
        scale = max(lo["scale"], hi["scale"])
        lo_c = lo["coefficient"] * 10 ** (scale - lo["scale"])
        hi_c = hi["coefficient"] * 10 ** (scale - hi["scale"])
        _require(lo_c <= hi_c, "amount_band min must not exceed max")
    return out


# --------------------------------------------------------------------------- #
# Category rule predicates (§2.2) and rule_id (§2)
# --------------------------------------------------------------------------- #


def canonical_match(match: Any) -> dict[str, Any]:
    """Validate a ``<match>`` predicate and return it in canonical form."""
    _require(isinstance(match, dict) and match, "match predicate must be a non-empty object")
    band = match.get("amount_band")
    keys = _MATCH_KEYS & set(match)
    _require(len(keys) == 1, f"match must carry exactly one of {sorted(_MATCH_KEYS)}")
    key = next(iter(keys))
    allowed = {key, "amount_band"} | ({"account"} if key == "description_contains" else set())
    _require(set(match) <= allowed, f"match for {key} may only also carry {sorted(allowed - {key})}")

    out: dict[str, Any] = {}
    literal = _nonblank_str(match[key], key)
    if key in {"merchant_canonical", "paynow_to"}:
        out[key] = literal
    elif key == "description_prefix":
        _require(len(literal) >= MIN_LITERAL_LENGTH,
                 f"description_prefix must be at least {MIN_LITERAL_LENGTH} characters")
        out[key] = literal
    else:  # description_contains
        _require(len(literal) >= 1, "description_contains must be non-empty")
        out[key] = literal
        if "account" in match:
            out["account"] = _nonblank_str(match["account"], "account")
    if band is not None:
        out["amount_band"] = canonical_amount_band(band)
    return out


def rule_id_for(match: Any) -> str:
    """``blake2s(canonical_json(<canonical match>))[:16]`` (Confirmed 28, §2)."""
    payload = canonical_json(canonical_match(match))
    return hashlib.blake2s(payload.encode("utf-8"), digest_size=8).hexdigest()


# --------------------------------------------------------------------------- #
# Merchant alias patterns (§2.1)
# --------------------------------------------------------------------------- #


def canonical_alias_pattern(pattern: Any) -> dict[str, str]:
    _require(isinstance(pattern, dict) and len(pattern) == 1,
             "each alias pattern is an object with exactly one of exact / prefix / regex")
    (kind, value), = pattern.items()
    _require(kind in {"exact", "prefix", "regex"}, f"unknown alias pattern kind {kind!r}")
    literal = _nonblank_str(value, f"{kind} literal")
    if kind in {"exact", "prefix"}:
        _require(_is_normalized_literal(literal),
                 f"{kind} literal must already be uppercased and whitespace-collapsed")
    if kind == "prefix":
        _require(len(literal) >= MIN_LITERAL_LENGTH,
                 f"prefix literal must be at least {MIN_LITERAL_LENGTH} characters")
    if kind == "regex":
        try:
            import re

            re.compile(literal)
        except re.error as exc:  # pragma: no cover - message varies
            raise FinCLError(f"invalid regex alias pattern: {exc}") from exc
    return {kind: literal}


def validate_alias_patterns(patterns: Any) -> list[dict[str, str]]:
    _require(isinstance(patterns, list) and len(patterns) >= 1,
             "alias payload needs a non-empty patterns list")
    canonical = [canonical_alias_pattern(pattern) for pattern in patterns]
    seen = {canonical_json(pattern) for pattern in canonical}
    _require(len(seen) == len(canonical), "alias patterns must be distinct")
    return canonical


# --------------------------------------------------------------------------- #
# Whole-payload validation, one per kind
# --------------------------------------------------------------------------- #


def validate_payload(decision_kind: str, anchors: dict[str, str], payload: dict[str, Any], custom_categories: frozenset[str] = frozenset()) -> None:
    """Raise ``FinCLError`` unless ``(anchors, payload)`` matches the kind's contract.

    Called both when the CLI authors a record and defensively on replay, so a
    record this build cannot make sense of fails closed rather than projecting a
    confident wrong result.
    """
    if decision_kind == MERCHANT_ALIAS_SET:
        _nonblank_str(anchors.get("canonical"), "anchors.canonical")
        validate_alias_patterns(payload.get("patterns"))
        _nonblank_str(payload.get("reason"), "reason")
        if payload.get("display_name") is not None:
            _nonblank_str(payload["display_name"], "display_name")
    elif decision_kind == MERCHANT_ALIAS_RETIRE:
        _nonblank_str(anchors.get("canonical"), "anchors.canonical")
        _nonblank_str(payload.get("reason"), "reason")
    elif decision_kind == CATEGORY_RULE_SET:
        canonical = canonical_match(payload.get("match"))
        expected = rule_id_for(canonical)
        _require(anchors.get("rule_id") == expected,
                 f"anchors.rule_id must be {expected} for this match predicate")
        resolve_category(_nonblank_str(payload.get("category"), "category"), custom_categories)
        _nonblank_str(payload.get("reason"), "reason")
    elif decision_kind == CATEGORY_RULE_RETIRE:
        _nonblank_str(anchors.get("rule_id"), "anchors.rule_id")
        _nonblank_str(payload.get("reason"), "reason")
    elif decision_kind == CATEGORY_OVERRIDE:
        _nonblank_str(anchors.get("entry"), "anchors.entry")
        resolve_category(_nonblank_str(payload.get("category"), "category"), custom_categories)
        _nonblank_str(payload.get("reason"), "reason")
    else:  # pragma: no cover - guarded by the caller's known-kind check
        raise FinCLError(f"{decision_kind} is not a categorization decision kind")
