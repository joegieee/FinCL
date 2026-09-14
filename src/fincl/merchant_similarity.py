"""Merchant duplicate-suggestion heuristic (PLAN-categorization-workflow.md Step 4).

Suggestion-only: the caller always shows these as checkboxes for a human to
confirm through the existing merge preview/batch path (§4). Nothing here ever
merges a merchant on its own.

Two independent heuristics; either is sufficient:

- **Shared leading token.** Both keys reduce to the same first whitespace
  token, at least ``MIN_LEADING_TOKEN`` characters long, once anything that
  is not a letter (asterisks, digits, punctuation) is turned into a space.
  This catches point-of-sale suffixes and store-number noise: "GRAB*RIDE" and
  "GRAB FOOD SG" both reduce to a leading token of "GRAB".
- **Levenshtein ratio.** ``1 - edit_distance / max(len(a), len(b))`` on the
  same reduced strings, at or above ``MIN_RATIO``. Catches near-duplicate
  spellings that do not share a leading token (transposed words, a missing
  branch suffix).

``MIN_LEADING_TOKEN = 4`` and ``MIN_RATIO = 0.85`` were tuned against the
fixtures in ``tests/test_merchant_similarity.py``: 4 is short enough to catch
common short brand prefixes ("GRAB", "KOPI") while a 3-character token would
start matching unrelated merchants that merely share a common English word
fragment. 0.85 is high enough that "GRAB" (4 chars) does not spuriously match
"GRABBER HOLDINGS" (16 chars) through the ratio path alone.
"""
from __future__ import annotations

import re

MIN_LEADING_TOKEN = 4
MIN_RATIO = 0.85

_NON_LETTER = re.compile(r"[^A-Za-z\s]")


def _normalize(value: str) -> str:
    return " ".join(_NON_LETTER.sub(" ", value).upper().split())


def _leading_token(normalized: str) -> str:
    parts = normalized.split()
    return parts[0] if parts else ""


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, char_b in enumerate(b, 1):
            cost = 0 if char_a == char_b else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[-1]


def levenshtein_ratio(a: str, b: str) -> float:
    """1.0 for identical strings, 0.0 for a maximally different pair."""
    if not a and not b:
        return 1.0
    return 1 - _levenshtein(a, b) / max(len(a), len(b))


def merchants_similar(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` look like statement-name variants of one merchant.

    Suggestion-only (see module docstring). Two identical keys are not
    "similar" here -- the caller already excludes a merchant from its own
    suggestion list.
    """
    if a == b:
        return False
    norm_a, norm_b = _normalize(a), _normalize(b)
    token_a, token_b = _leading_token(norm_a), _leading_token(norm_b)
    if token_a and token_a == token_b and len(token_a) >= MIN_LEADING_TOKEN:
        return True
    return levenshtein_ratio(norm_a, norm_b) >= MIN_RATIO
