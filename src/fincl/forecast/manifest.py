"""Stage 3A — forecast readiness and the input manifest.

One operation, :func:`forecast_input_manifest`, that consumes the Stage 1
baseline reads (:mod:`fincl.baseline.readiness`, :mod:`fincl.baseline.queries`,
:mod:`fincl.baseline.roles`, :mod:`fincl.baseline.estimators`) and the Stage 2
adapters (:mod:`fincl.obligations.queries`, :mod:`fincl.position.queries`) and
returns a single structured manifest:

* account scope and the supported cash cutoff;
* the baseline months used and the named estimator / role methods;
* the inferred (confirmed-schedule) recurrences the forecast will project;
* the unresolved items that stay visible instead of being zeroed;
* source provenance links, one per adapter; and
* a per-output readiness classification with its limitations.

This module does **not** re-implement any estimation or query — it only folds
the existing reads and names what they can and cannot support.

Single generation
-----------------
:func:`fincl.generation.current_generation` is resolved once here and every
adapter output that stamps a generation token is checked against it.
``fincl.baseline`` reads and ``fincl.obligations.queries`` report a token and are
recorded as *generation-pinned*; ``fincl.position.queries`` does not stamp one
and is recorded as *unpinned* (read in the same process immediately after the
pinned inputs, but a concurrent refresh between reads is not detected). If any
pinned input reports a different token the manifest is **stale** and no figures
are combined.

``fincl.generation.build_manifest`` is an unrelated concept (the refresh input
digest); it is neither reused nor extended here.

Readiness vocabulary
--------------------
:mod:`fincl.baseline.readiness` already owns two axes — month completeness
(``established`` / ``provisional`` / ``missing``) and window quality
(``normal`` / ``limited-history`` / ``none``). The per-output readiness here is a
**third, independent axis** with its own values (:data:`READINESS_ESTABLISHED`,
:data:`READINESS_LIMITED`, :data:`READINESS_UNAVAILABLE`); it is never aliased to
the readiness.py constants. It maps roughly — a ``normal`` window backs an
established-input estimate, a ``limited-history`` window backs a limited-history
estimate — but it is decided per output, so a missing opening balance can make
the cash outputs unavailable while the cost table stays established.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..baseline.estimators import baseline_estimate_from
from ..baseline.queries import cost_income_table_from
from ..baseline.readiness import baseline_readiness_from
from ..baseline.roles import ROLE_RULE_VERSION, baseline_roles_from
from ..db import connect_readonly
from ..errors import FinCLError
from ..generation import current_generation
from ..money import ExactMoney, exact_sum, format_decimal
from ..obligations.queries import obligations_page_from
from ..position.queries import position_page_from
from ..reserve import reserve_policy

# --- the per-output readiness axis (see the module docstring) ----------------
READINESS_ESTABLISHED = "established-input estimate"
READINESS_LIMITED = "limited-history estimate"
READINESS_UNAVAILABLE = "unavailable"
FORECAST_READINESS_LEVELS = (
    READINESS_ESTABLISHED,
    READINESS_LIMITED,
    READINESS_UNAVAILABLE,
)

_OUTPUTS = (
    "income_evidence",
    "spending_evidence",
    "observed_cost_table",
    "routine_baseline",
    "reserve",
    "allowance",
    "cash_balances",
    "recurrence_inference",
)

_AMBIGUOUS_DATE_KINDS = frozenset(
    {
        "no_stated_date",
        "no_remaining_due_date",
        "no_scheduled_occurrence",
        "statement_due_date_not_printed",
    }
)

_POSITION_NOT_PINNED = (
    "position_page is not generation-pinned (it does not stamp a generation "
    "token); it is read in the same process immediately after the pinned "
    "inputs, but a concurrent refresh between reads is not detected here"
)


def _sum_money(values: list[str]) -> str:
    if not values:
        return "0"
    return format_decimal(*exact_sum(*[ExactMoney.parse(value) for value in values]))


def _window_readiness(window: dict[str, Any]) -> str:
    """Map a readiness.py window quality onto the per-output axis."""
    quality = window.get("quality")
    if quality == "normal":
        return READINESS_ESTABLISHED
    if quality == "limited-history":
        return READINESS_LIMITED
    return READINESS_UNAVAILABLE


def _summarize(levels: list[str]) -> str:
    """Headline for a currency-split output: a supported currency is never hidden."""
    if not levels or all(level == READINESS_UNAVAILABLE for level in levels):
        return READINESS_UNAVAILABLE
    if any(level != READINESS_ESTABLISHED for level in levels):
        return READINESS_LIMITED
    return READINESS_ESTABLISHED


def _source(
    name: str,
    module: str,
    *,
    pinned: bool,
    reported: str | None,
    resolved: str,
    available: bool,
    reason: str | None,
    consumed_by: list[str],
) -> dict[str, Any]:
    return {
        "input": name,
        "module": module,
        "generation_pinned": pinned,
        "reported_generation": reported,
        "matches_resolved_generation": reported is None or reported == resolved,
        "available": available,
        "reason": reason,
        "consumed_by": consumed_by,
    }


def _role_scope(roles: dict[str, Any], currency: str) -> tuple[bool, str | None]:
    """Is essential-role scope complete for this currency? (does not block the cost table)."""
    if not roles.get("available"):
        return False, roles.get("reason") or "baseline role scope is unavailable"
    summary = roles["by_currency"].get(currency)
    if summary is None:
        return False, "no role summary is available for this currency"
    if Decimal(summary["totals"]["unresolved"]) != 0:
        return False, (
            "essential-role scope is incomplete; unresolved owner-cost "
            "contributions must be reviewed before a protective claim is derived"
        )
    return True, None


def forecast_input_manifest(
    database: Path, *, today: date, as_of: str | None = None
) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return forecast_input_manifest_from(connection, today=today, as_of=as_of)
    finally:
        connection.close()


def forecast_input_manifest_from(
    connection: sqlite3.Connection, *, today: date, as_of: str | None = None
) -> dict[str, Any]:
    """Gather every forecast input behind one shape and classify its readiness.

    ``today`` is required (§5.3): resolved once by the caller, never by this
    reader.
    """
    if not connection.in_transaction:
        connection.execute("BEGIN")
    generation = current_generation(connection)
    if generation is None:
        raise FinCLError("The ledger has no completed generation; refresh it first")
    policy = reserve_policy(connection)
    resolved = str(generation["generation_token"])

    readiness = baseline_readiness_from(connection, today=today)
    table = cost_income_table_from(connection, today=today)
    roles = baseline_roles_from(connection, today=today)
    estimate = baseline_estimate_from(connection, today=today)
    obligations = obligations_page_from(connection, as_of=as_of, today=today)
    position = position_page_from(connection, today=today)
    resolved_as_of = obligations["as_of"]

    reported_generations = {
        "baseline_readiness": readiness.get("generation"),
        "cost_income_table": table.get("generation"),
        "baseline_roles": roles.get("generation"),
        "baseline_estimate": estimate.get("generation"),
        "obligations_page": obligations.get("generation"),
        "position_page": None,
    }
    mismatched = sorted(
        name
        for name, token in reported_generations.items()
        if token is not None and token != resolved
    )

    sources = [
        _source(
            "baseline_readiness", "fincl.baseline.readiness.baseline_readiness",
            pinned=True, reported=readiness.get("generation"), resolved=resolved,
            available=True, reason=None,
            consumed_by=["income_evidence", "spending_evidence", "observed_cost_table"],
        ),
        _source(
            "cost_income_table", "fincl.baseline.queries.cost_income_table",
            pinned=True, reported=table.get("generation"), resolved=resolved,
            available=bool(table["available"]), reason=table.get("reason"),
            consumed_by=[
                "income_evidence", "spending_evidence", "observed_cost_table",
                "routine_baseline",
            ],
        ),
        _source(
            "baseline_roles", "fincl.baseline.roles.baseline_roles",
            pinned=True, reported=roles.get("generation"), resolved=resolved,
            available=bool(roles.get("available")), reason=roles.get("reason"),
            consumed_by=["observed_cost_table", "reserve", "allowance"],
        ),
        _source(
            "baseline_estimate", "fincl.baseline.estimators.baseline_estimate",
            pinned=True, reported=estimate.get("generation"), resolved=resolved,
            available=bool(estimate["available"]), reason=estimate.get("reason"),
            consumed_by=["routine_baseline", "reserve", "allowance"],
        ),
        _source(
            "obligations_page", "fincl.obligations.queries.obligations_page",
            pinned=True, reported=obligations.get("generation"), resolved=resolved,
            available=True, reason=None,
            consumed_by=["recurrence_inference"],
        ),
        _source(
            "position_page", "fincl.position.queries.position_page",
            pinned=False, reported=None, resolved=resolved,
            available=True, reason=_POSITION_NOT_PINNED,
            consumed_by=["cash_balances"],
        ),
    ]

    scope = readiness["scope"]
    account_scope = {
        "accounts": scope["accounts"],
        "currencies": scope["currencies"],
        "analysis_window": scope["analysis_window"],
        "liquid_pockets": [
            {
                "account_id": pocket["account_id"],
                "currency_pocket": pocket["currency_pocket"],
                "source_type": pocket["source_type"],
                "basis": pocket["basis"],
                "as_of": pocket["as_of"],
            }
            for pocket in position["included_pockets"]
        ],
        "non_liquid_pockets": [
            {
                "account_id": pocket["account_id"],
                "currency_pocket": pocket["currency_pocket"],
                "excluded_reason": pocket["excluded_reason"],
            }
            for pocket in position["pockets"]
            if not pocket["liquid"]
        ],
    }

    if mismatched:
        return _stale_manifest(
            resolved=resolved,
            today=today,
            as_of=resolved_as_of,
            reported_generations=reported_generations,
            mismatched=mismatched,
            account_scope=account_scope,
            baseline_window=readiness["baseline_window"],
            sources=sources,
        )

    window = readiness["baseline_window"]
    table_available = bool(table["available"])
    table_reason = table.get("reason")
    currencies: list[str] = list(table["currencies"]) if table_available else list(scope["currencies"])
    window_level = _window_readiness(window)

    # --- unresolved items: kept visible, never zeroed -----------------------
    unclassified_spending = [
        {
            **task,
            "cuttable": False,
            "role": "unresolved_spending",
            "treatment": (
                "retained at full observed value; not assigned a discretionary "
                "role and not treated as zero"
            ),
        }
        for task in readiness["tasks"]
        if task["kind"] == "resolve-categorization"
    ]
    evidence_gaps = [
        task
        for task in readiness["tasks"]
        if task["kind"] in {"import-missing", "confirm-export-period", "resolve-reconciliation"}
    ]
    ambiguous_obligations = _ambiguous_obligations(obligations["obligations"])

    # --- income evidence and the named income gap --------------------------
    income_streams = table["income_streams"] if table_available else []
    classified_income = bool(income_streams)
    pending_by_currency: dict[str, list[str]] = {}
    other_by_currency: dict[str, list[str]] = {}
    if table_available:
        for row in table["months"]:
            for currency, cell in row["by_currency"].items():
                if cell["pending_inflow"] != "0":
                    pending_by_currency.setdefault(currency, []).append(cell["pending_inflow"])
                if cell["other_inflow"] != "0":
                    other_by_currency.setdefault(currency, []).append(cell["other_inflow"])
    pending_totals = {currency: _sum_money(values) for currency, values in pending_by_currency.items()}
    other_totals = {currency: _sum_money(values) for currency, values in other_by_currency.items()}

    if not table_available:
        income_gap: dict[str, Any] | None = {
            "kind": "income_evidence_unavailable",
            "reason": table_reason,
            "treatment": "no income figure is derived while the evidence table is unavailable",
        }
    elif not classified_income:
        income_gap = {
            "kind": "no_classified_income_stream",
            "detail": "No classified income stream is evidenced in the baseline window.",
            "pending_inflow_by_currency": pending_totals,
            "other_inflow_by_currency": other_totals,
            "treatment": (
                "pending and unclassified inflows are recorded separately and are "
                "not counted as expected income; a no-income forecast is a stress "
                "case, never the base case, and no salary is invented"
            ),
        }
    else:
        income_gap = None

    # --- reserve / allowance, per currency, gated on essential-role scope --
    estimate_available = bool(estimate["available"])
    estimate_reason = estimate.get("reason")
    reserve_by_currency: dict[str, Any] = {}
    allowance_by_currency: dict[str, Any] = {}
    blocked_scope_currencies: list[str] = []
    for currency in currencies:
        scope_ok, scope_reason = _role_scope(roles, currency)
        if not scope_ok:
            blocked_scope_currencies.append(currency)
        estimate_cell = (
            estimate["by_currency"].get(currency, {}) if estimate_available else {}
        )
        reserve_cell = estimate_cell.get("reserve") if estimate_available else None
        if not estimate_available:
            reserve_by_currency[currency] = {"available": False, "reason": estimate_reason}
        else:
            reserve_by_currency[currency] = reserve_cell or {
                "available": False,
                "reason": "no reserve was derived for this currency",
            }
        if not estimate_available:
            allowance_by_currency[currency] = {"available": False, "reason": estimate_reason}
        elif not scope_ok:
            allowance_by_currency[currency] = {"available": False, "reason": scope_reason}
        else:
            allowance_by_currency[currency] = {
                "available": True,
                "irregular_allowance_monthly": estimate_cell.get("irregular_allowance_monthly"),
                "routine_lifestyle_baseline": estimate_cell.get("routine_lifestyle_baseline"),
                "basis": "mean of the monthly held-out irregular amounts over the window",
            }

    # --- cash balances, per currency, gated on a supported opening balance -
    cash_by_currency = _cash_by_currency(position, currencies)

    # --- per-output readiness classification ------------------------------
    outputs: dict[str, Any] = {}
    outputs["income_evidence"] = _output(
        readiness=window_level if table_available else READINESS_UNAVAILABLE,
        reason=None if table_available else table_reason,
        depends_on=["baseline_readiness", "cost_income_table"],
        limitations=(
            [
                "No classified income stream is evidenced; recorded as a named "
                "income gap. Pending inflows are retained separately and never "
                "counted as expected income."
            ]
            if table_available and not classified_income
            else []
        ),
    )
    outputs["spending_evidence"] = _output(
        readiness=window_level if table_available else READINESS_UNAVAILABLE,
        reason=None if table_available else table_reason,
        depends_on=["baseline_readiness", "cost_income_table"],
        limitations=(
            [
                f"{len(unclassified_spending)} unclassified observed-outflow "
                "group(s) remain as non-cuttable unresolved spending."
            ]
            if unclassified_spending
            else []
        ),
    )
    cost_table_limits: list[str] = []
    if blocked_scope_currencies:
        cost_table_limits.append(
            "Essential-role scope is incomplete for "
            f"{', '.join(blocked_scope_currencies)}; the observed cost table is "
            "shown in full but reserve and allowance claims are withheld."
        )
    if unclassified_spending:
        cost_table_limits.append(
            "Imperfect category coverage does not block the cost table; "
            "unclassified outflows sit in non-cuttable unresolved spending."
        )
    outputs["observed_cost_table"] = _output(
        readiness=window_level if table_available else READINESS_UNAVAILABLE,
        reason=None if table_available else table_reason,
        depends_on=["baseline_readiness", "cost_income_table", "baseline_roles"],
        limitations=cost_table_limits,
    )
    outputs["routine_baseline"] = _output(
        readiness=window_level if estimate_available else READINESS_UNAVAILABLE,
        reason=None if estimate_available else estimate_reason,
        depends_on=["baseline_readiness", "cost_income_table", "baseline_estimate"],
        limitations=list(estimate.get("limitations", [])) if estimate_available else [],
    )
    reserve_levels = [
        window_level if cell.get("available") else READINESS_UNAVAILABLE
        for cell in reserve_by_currency.values()
    ]
    outputs["reserve"] = _output(
        readiness=_summarize(reserve_levels),
        reason=None,
        depends_on=["baseline_roles", "baseline_estimate"],
        limitations=(
            [
                "Reserve claims are withheld for "
                f"{', '.join(blocked_scope_currencies)}: essential-role scope is "
                "incomplete."
            ]
            if blocked_scope_currencies
            else []
        ),
        by_currency=reserve_by_currency,
    )
    allowance_levels = [
        window_level if cell.get("available") else READINESS_UNAVAILABLE
        for cell in allowance_by_currency.values()
    ]
    outputs["allowance"] = _output(
        readiness=_summarize(allowance_levels),
        reason=None,
        depends_on=["baseline_roles", "baseline_estimate"],
        limitations=(
            [
                "Allowance claims are withheld for "
                f"{', '.join(blocked_scope_currencies)}: essential-role scope is "
                "incomplete."
            ]
            if blocked_scope_currencies
            else []
        ),
        by_currency=allowance_by_currency,
    )
    cash_levels = [cell["readiness"] for cell in cash_by_currency.values()]
    outputs["cash_balances"] = _output(
        readiness=_summarize(cash_levels) if cash_levels else READINESS_UNAVAILABLE,
        reason=(
            None
            if any(cell["readiness"] != READINESS_UNAVAILABLE for cell in cash_by_currency.values())
            else (
                (position["current_position_reasons"] or [None])[0]
                or "no supported opening cash balance"
            )
        ),
        depends_on=["position_page"],
        limitations=[_POSITION_NOT_PINNED],
        by_currency=cash_by_currency,
    )
    recurrence_count = len(
        [o for o in obligations["obligations"] if o["origin"] == "recurring_commitment"]
    )
    outputs["recurrence_inference"] = _output(
        readiness=READINESS_ESTABLISHED if recurrence_count else READINESS_LIMITED,
        reason=(
            None
            if recurrence_count
            else "no confirmed recurring commitments are recorded to project"
        ),
        depends_on=["obligations_page"],
        limitations=(
            [
                f"{len(ambiguous_obligations)} obligation(s) have ambiguous amount "
                "or date terms and are a named forecast limitation, not a precise "
                "projected cash event."
            ]
            if ambiguous_obligations
            else []
        ),
    )

    inferred_recurrences = [
        {
            "obligation_id": o["obligation_id"],
            "label": o["label"],
            "currency": o["currency"],
            "direction": o["direction"],
            "schedule": o["date_basis"].get("schedule"),
            "next_date": o["date_basis"].get("value"),
            "expected_amount": o["expected_amount"],
            "occurrence_amounts": o["occurrence_amounts"],
            "confidence": o.get("confidence"),
            "confidence_basis": o.get("confidence_basis"),
            "baseline_link": o["baseline_link"],
            "unmatched_past_occurrences": o.get("unmatched_past_occurrences", []),
            "source": o["source"],
        }
        for o in obligations["obligations"]
        if o["origin"] == "recurring_commitment"
    ]

    limitations = [_POSITION_NOT_PINNED]
    if window.get("basis") == "some-provisional":
        limitations.append(
            "The baseline window rests on observed-only (provisional) months; "
            "every baseline-derived output is a limited-history estimate."
        )
    if income_gap and income_gap["kind"] == "no_classified_income_stream":
        limitations.append(
            "No classified income stream is evidenced; a no-income forecast is a "
            "stress case, never the base case, and pending inflows are never "
            "converted to expected income."
        )
    if unclassified_spending:
        limitations.append(
            f"{len(unclassified_spending)} unclassified observed-outflow group(s) "
            "remain as non-cuttable unresolved spending; recurrence and baseline "
            "inference are not blocked by this."
        )
    if ambiguous_obligations:
        limitations.append(
            f"{len(ambiguous_obligations)} obligation(s) have ambiguous amount or "
            "date terms and are a named forecast limitation."
        )
    if blocked_scope_currencies:
        limitations.append(
            "Essential-role scope is incomplete for "
            f"{', '.join(blocked_scope_currencies)}; reserve and allowance claims "
            "are withheld while the observed cost table remains available."
        )

    return {
        "generation": resolved,
        "generated_for": today.isoformat(),
        "as_of": resolved_as_of,
        "stale": False,
        "stale_reasons": [],
        "reported_generations": reported_generations,
        "mixed_generation_inputs": [],
        "account_scope": account_scope,
        "cutoff": {
            "supported_cutoff": position["supported_cutoff"],
            "current_position_supported": position["current_position_supported"],
            "as_of": resolved_as_of,
            "reasons": position["current_position_reasons"],
            "stale_after_days": position["stale_after_days"],
            "missing_intervals": position["missing_intervals"],
        },
        "baseline_window": window,
        "baseline_months_used": (
            [row["month"] for row in table["months"]] if table_available else []
        ),
        "methods": {
            "estimator": (estimate.get("sample") or {}).get("method_version"),
            "residual_method": (estimate.get("sample") or {}).get("residual_method"),
            "outlier_method": (estimate.get("sample") or {}).get("outlier_method"),
            "role_rule_version": ROLE_RULE_VERSION,
            "reserve_months": str(policy.months),
            "reserve_basis": (
                f"default {policy.months} months of mean essential owner cost"
                if policy.is_default
                else f"chosen policy of {policy.months} months of mean essential owner cost"
            ),
            "reserve_policy_is_default": policy.is_default,
            "window_quality": window.get("quality"),
            "window_basis": window.get("basis"),
        },
        "inferred_recurrences": inferred_recurrences,
        "unresolved_items": {
            "unclassified_spending": unclassified_spending,
            "evidence_gaps": evidence_gaps,
            "ambiguous_obligations": ambiguous_obligations,
            "unmatched_receipts": (
                {
                    currency: table["reconciliation"][currency]["unmatched_receipts_total"]
                    for currency in table["currencies"]
                }
                if table_available
                else {}
            ),
        },
        "income": {
            "streams": income_streams,
            "classified_income_evidenced": classified_income,
            "gap": income_gap,
            "pending_inflow_by_currency": pending_totals,
            "other_inflow_by_currency": other_totals,
        },
        "observed_cost": {
            "available": table_available,
            "reason": table_reason,
            "currencies": list(table["currencies"]) if table_available else [],
            "months": table["months"] if table_available else [],
            "reconciliation": table.get("reconciliation", {}) if table_available else {},
            "roles_by_currency": (
                {
                    currency: roles["by_currency"][currency]["totals"]
                    for currency in roles["by_currency"]
                }
                if roles.get("available")
                else {}
            ),
        },
        "cash": {
            "supported_cutoff": position["supported_cutoff"],
            "current_position_supported": position["current_position_supported"],
            "combined_liquid_position": position["combined_liquid_position"],
            "withheld": position["liquid_total_withheld"],
            "reasons": position["current_position_reasons"],
            "by_currency": cash_by_currency,
        },
        "reserve_and_allowance": {
            "by_currency": {
                currency: {
                    "reserve": reserve_by_currency[currency],
                    "allowance": allowance_by_currency[currency],
                }
                for currency in currencies
            }
        },
        "sources": sources,
        "outputs": outputs,
        "limitations": limitations,
    }


def _output(
    *,
    readiness: str,
    reason: str | None,
    depends_on: list[str],
    limitations: list[str],
    by_currency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "readiness": readiness,
        "available": readiness != READINESS_UNAVAILABLE,
        "reason": reason,
        "depends_on": depends_on,
        "limitations": limitations,
    }
    if by_currency is not None:
        result["by_currency"] = by_currency
    return result


def _ambiguous_obligations(obligations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ambiguous: list[dict[str, Any]] = []
    for o in obligations:
        reasons: list[str] = []
        if o.get("amount_basis") == "unknown":
            reasons.append(o.get("remaining_unknown_reason") or "amount terms are unknown")
        date_kind = o.get("date_basis", {}).get("kind")
        if date_kind in _AMBIGUOUS_DATE_KINDS:
            reasons.append(f"date basis is '{date_kind}'")
        link = o.get("baseline_link", {})
        if o["origin"] == "recurring_commitment" and not link.get("available"):
            reasons.append(link.get("reason") or "baseline component link is ambiguous")
        if reasons:
            ambiguous.append(
                {
                    "obligation_id": o["obligation_id"],
                    "origin": o["origin"],
                    "label": o["label"],
                    "currency": o["currency"],
                    "reasons": reasons,
                }
            )
    return ambiguous


def _cash_by_currency(
    position: dict[str, Any], currencies: list[str]
) -> dict[str, Any]:
    combined = position["combined_liquid_position"]
    withheld = position["liquid_total_withheld"]
    supported = bool(position["current_position_supported"])
    bridges = position["cash_bridge"]["combined"]["by_currency"]
    reasons = position["current_position_reasons"]
    generated_for = date.fromisoformat(str(position["generated_for"]))
    included_currencies = {
        pocket["currency_pocket"]
        for pocket in position["included_pockets"]
        if pocket["balance"] is not None
    }
    keys = sorted(set(currencies) | set(combined) | set(withheld) | included_currencies)
    result: dict[str, Any] = {}
    for currency in keys:
        has_balance = currency in combined or currency in included_currencies
        if not has_balance:
            result[currency] = {
                "readiness": READINESS_UNAVAILABLE,
                "balance": None,
                "observation_as_of": None,
                "stale_days": None,
                "missing_intervals": [
                    gap for gap in position["missing_intervals"]
                    if gap["currency_pocket"] == currency
                ],
                "reason": (
                    withheld.get(currency)
                    or (reasons[0] if reasons else None)
                    or "no supported opening cash balance for this pocket"
                ),
            }
            continue
        bridge_ok = bridges.get(currency, {}).get("reconciled", True)
        if supported and bridge_ok:
            level = READINESS_ESTABLISHED
            reason = None
        else:
            level = READINESS_LIMITED
            reason = (
                "a last-known balance is available but complete movements through "
                "the as-of date are not supported"
                if not supported
                else "the historical cash bridge for this pocket does not reconcile"
            )
        result[currency] = {
            "readiness": level,
            "balance": combined.get(currency),
            # The forecast opens at this supported historical boundary.  It is
            # deliberately not relabelled as a balance observed today.
            "observation_as_of": position["supported_cutoff"],
            "stale_days": (
                (generated_for - date.fromisoformat(position["supported_cutoff"])).days
                if position["supported_cutoff"] else None
            ),
            "missing_intervals": [
                gap for gap in position["missing_intervals"]
                if gap["currency_pocket"] == currency
            ],
            "reason": reason,
        }
    return result


def _stale_manifest(
    *,
    resolved: str,
    today: date,
    as_of: str,
    reported_generations: dict[str, str | None],
    mismatched: list[str],
    account_scope: dict[str, Any],
    baseline_window: dict[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    stale_reasons = [
        f"{name} reports generation {reported_generations[name]}, but the manifest "
        f"resolved {resolved}; refusing to combine mixed-generation inputs"
        for name in mismatched
    ]
    unavailable_reason = (
        "the manifest is stale: its inputs describe different ledger generations; "
        "refresh the ledger and retry"
    )
    return {
        "generation": resolved,
        "generated_for": today.isoformat(),
        "as_of": as_of,
        "stale": True,
        "stale_reasons": stale_reasons,
        "reported_generations": reported_generations,
        "mixed_generation_inputs": mismatched,
        "account_scope": account_scope,
        "cutoff": None,
        "baseline_window": baseline_window,
        "baseline_months_used": [],
        "methods": {},
        "inferred_recurrences": [],
        "unresolved_items": {},
        "income": {},
        "observed_cost": {"available": False, "reason": unavailable_reason},
        "cash": {},
        "reserve_and_allowance": {},
        "sources": sources,
        "outputs": {
            name: {
                "readiness": READINESS_UNAVAILABLE,
                "available": False,
                "reason": unavailable_reason,
                "depends_on": [],
                "limitations": [],
            }
            for name in _OUTPUTS
        },
        "limitations": [
            "The manifest is stale: " + "; ".join(stale_reasons) + ". No figures are combined."
        ],
    }


__all__ = [
    "FORECAST_READINESS_LEVELS",
    "READINESS_ESTABLISHED",
    "READINESS_LIMITED",
    "READINESS_UNAVAILABLE",
    "forecast_input_manifest",
]
