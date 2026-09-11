"""Opaque, tamper-evident contributor handles.

A handle names *which* components sit behind a displayed total: the generation it
was minted for, the lens, the currency, the analysis window, the buckets, and an
optional category or merchant filter. It never carries SQL — only enum-checked
and parameterized values — and it is bound to one generation, so a stale handle
is refused rather than silently answered against fresh data (application contract
§4).

The payload is signed with a per-process key: a handle does not survive a server
restart, which is the same contract as the rest of the read surface (no historic
snapshot service — the client reloads Analysis and gets fresh handles).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
from hashlib import sha256
from typing import Any

from ..errors import FinCLError

_SIGNING_KEY = secrets.token_bytes(32)

# Buckets a drill-down may select. `net` is the signed net-flow set (everything
# that nets); the rest are the individual economic-flow buckets.
_SELECTABLE_BUCKETS = frozenset(
    {
        "spending",
        "income",
        "refunds",
        "reimbursements",
        "non_income_inflow",
        "pending_review",
        "other",
        "fees",
        "net",
    }
)
_SORTS = frozenset({"amount", "date"})


def _sign(payload: bytes) -> str:
    return hmac.new(_SIGNING_KEY, payload, sha256).hexdigest()[:32]


def encode(
    *,
    generation: str,
    lens: str,
    currency: str,
    start: str,
    end: str,
    buckets: list[str],
    sort: str = "amount",
    category_key: str | None = None,
    merchant_display: str | None = None,
) -> str:
    """Mint a handle for one displayed total or ranking row."""
    if sort not in _SORTS:
        raise FinCLError(f"unknown contributor sort {sort!r}")
    bad = set(buckets) - _SELECTABLE_BUCKETS
    if bad or not buckets:
        raise FinCLError(f"unknown contributor bucket selection {sorted(bad)}")
    body = {
        "kind": "legacy",
        "generation": generation,
        "lens": lens,
        "currency": currency,
        "start": start,
        "end": end,
        "buckets": sorted(buckets),
        "sort": sort,
        "category_key": category_key,
        "merchant_display": merchant_display,
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    token = {"b": base64.urlsafe_b64encode(raw).decode(), "s": _sign(raw)}
    return base64.urlsafe_b64encode(
        json.dumps(token, separators=(",", ":")).encode()
    ).decode()


def decode(handle: str) -> dict[str, Any]:
    """Return the validated scope for ``handle`` or raise :class:`FinCLError`."""
    try:
        token = json.loads(base64.urlsafe_b64decode(handle.encode()))
        raw = base64.urlsafe_b64decode(str(token["b"]).encode())
        signature = str(token["s"])
    except (binascii.Error, ValueError, TypeError, KeyError) as exc:
        raise FinCLError("This contributor link is not valid; reopen Analysis.") from exc
    if not hmac.compare_digest(signature, _sign(raw)):
        raise FinCLError("This contributor link is not valid; reopen Analysis.")
    body = json.loads(raw)
    # A reported handle (below) is signed with the same key but carries a
    # different body shape (no `currency` field, a `kind: "reported"` tag) —
    # reject it here explicitly rather than let a legacy caller's
    # `scope['currency']` raise a bare ``KeyError`` on a syntactically valid
    # but wrong-kind handle.
    if body.get("kind") != "legacy":
        raise FinCLError("This contributor link is not valid; reopen Analysis.")
    if body.get("sort") not in _SORTS or set(body.get("buckets", [])) - _SELECTABLE_BUCKETS:
        raise FinCLError("This contributor link is not valid; reopen Analysis.")
    return body


# --- reported (M3b.1 step 04) -----------------------------------------------

# Extends the legacy selectable set with the sign-based net-flow partitions
# (§3.1: inflow/outflow are not bucket memberships, they select nets=1 rows by
# sign) and the two exceptional buckets, which the legacy surface never
# exposed through a handle at all. Kept separate from `_SELECTABLE_BUCKETS` so
# this addition cannot loosen what a *legacy* handle may encode.
_REPORTED_SELECTABLE_BUCKETS = _SELECTABLE_BUCKETS | frozenset(
    {"inflow", "outflow", "exceptional_inflow", "exceptional_loss"}
)


def encode_reported(
    *,
    generation: str,
    reporting_currency: str,
    policy_version: str | None,
    rate_inventory: str,
    as_of: str,
    lens: str,
    start: str,
    end: str,
    buckets: list[str],
    sort: str = "amount",
    pocket_currency: str | None = None,
    category_key: str | None = None,
    merchant_display: str | None = None,
) -> str:
    """Mint a reported-analysis contributor handle (§6.2, M3b.1 step 04).

    Bound to the full reported context, not only the native scope fields
    ``encode`` already binds to: generation, reporting currency, policy
    version and rate-inventory digest, plus ``as_of``. Any of these changing
    — a rate import, a reporting-currency change, a refreshed generation —
    invalidates every handle minted under the old one, exactly as §6.2
    requires ("reject reuse under different filters, reporting policy,
    generation, rate inventory, valuation date, or sort").
    """
    if sort not in _SORTS:
        raise FinCLError(f"unknown contributor sort {sort!r}")
    bad = set(buckets) - _REPORTED_SELECTABLE_BUCKETS
    if bad or not buckets:
        raise FinCLError(f"unknown contributor bucket selection {sorted(bad)}")
    body = {
        "kind": "reported",
        "generation": generation,
        "reporting_currency": reporting_currency,
        "policy_version": policy_version,
        "rate_inventory": rate_inventory,
        "as_of": as_of,
        "lens": lens,
        "start": start,
        "end": end,
        "buckets": sorted(buckets),
        "sort": sort,
        "pocket_currency": pocket_currency,
        "category_key": category_key,
        "merchant_display": merchant_display,
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    token = {"b": base64.urlsafe_b64encode(raw).decode(), "s": _sign(raw)}
    return base64.urlsafe_b64encode(
        json.dumps(token, separators=(",", ":")).encode()
    ).decode()


def decode_reported(handle: str) -> dict[str, Any]:
    """Return the validated scope for a reported ``handle`` or raise :class:`FinCLError`."""
    try:
        token = json.loads(base64.urlsafe_b64decode(handle.encode()))
        raw = base64.urlsafe_b64decode(str(token["b"]).encode())
        signature = str(token["s"])
    except (binascii.Error, ValueError, TypeError, KeyError) as exc:
        raise FinCLError("This contributor link is not valid; reopen Analysis.") from exc
    if not hmac.compare_digest(signature, _sign(raw)):
        raise FinCLError("This contributor link is not valid; reopen Analysis.")
    body = json.loads(raw)
    if body.get("kind") != "reported":
        raise FinCLError("This contributor link is not valid; reopen Analysis.")
    if body.get("sort") not in _SORTS or set(body.get("buckets", [])) - _REPORTED_SELECTABLE_BUCKETS:
        raise FinCLError("This contributor link is not valid; reopen Analysis.")
    return body
