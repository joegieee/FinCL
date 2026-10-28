"""Stage 4B spending controls and isolated what-if scenarios."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..baseline.estimators import baseline_estimate
from ..baseline.roles import baseline_roles
from ..db import connect_readonly, read_snapshot
from ..errors import FinCLError
from ..forecast.engine import COLUMN_IRREGULAR, ResidualCategory, cash_timeline
from ..funds.queries import sinking_funds_page
from ..intervals import DateInterval
from ..money import ExactMoney, format_decimal
from ..obligations.queries import obligations_page
from ..planning.queries import budgets_page
from ..read_cache import request_cache
from ..report import _coverage_contract
from .models import (
    apply_what_if_case,
    budget_pace,
    case_comparison,
    conservative_capacity,
    group_commitments,
)


def _month(value: str | None, today: date) -> str:
    value = value or today.strftime("%Y-%m")
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise FinCLError("month must be YYYY-MM") from exc
    if parsed.strftime("%Y-%m") != value:
        raise FinCLError("month must be YYYY-MM")
    return value


def _minor(value: str, scale: int) -> int:
    return ExactMoney.parse(value).rescaled_coefficient(scale)


def _role_inputs(
    roles: dict[str, Any], estimate: dict[str, Any], currency: str
) -> tuple[list[ResidualCategory], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in roles.get("groups", []):
        # Recurring commitments are forecast on their own schedule, and
        # irregular cost has its own allowance below.
        if row["currency"] != currency or row["cost_pattern"] in ("irregular", "fixed_recurring"):
            continue
        category = str(row["category_key"])
        cell = grouped.setdefault(
            category,
            {
                "mean": Decimal(0),
                "high": Decimal(0),
                "tier": row.get("tier"),
                "essential_status": row.get("essential_status"),
                "basis": [],
            },
        )
        cell["mean"] += max(Decimal(str(row.get("mean_monthly", "0"))), Decimal(0))
        cell["high"] += max(
            Decimal(str(row.get("highest_observed_monthly", "0"))), Decimal(0)
        )
        cell["basis"].append(row.get("basis"))

    residuals: list[ResidualCategory] = []
    role_map: dict[str, dict[str, Any]] = {}
    options: list[dict[str, Any]] = []
    for category, cell in sorted(grouped.items()):
        category_id = f"role:{currency}:{category}"
        mean = str(cell["mean"].quantize(Decimal("0.01")))
        high = str(cell["high"].quantize(Decimal("0.01")))
        role_map[category] = {
            "tier": cell["tier"],
            "essential_status": cell["essential_status"],
        }
        residuals.append(
            ResidualCategory(
                category_id=category_id,
                currency=currency,
                mean_monthly=mean,
                highest_observed_monthly=high,
                label=category,
                provenance={
                    "source": "fincl.baseline.roles.baseline_roles",
                    "category_id": category_id,
                    "category_key": category,
                    "tier": cell["tier"],
                    "essential_status": cell["essential_status"],
                    "basis": cell["basis"],
                },
            )
        )
        if cell["tier"] in (3, 4) and cell["mean"] > 0:
            options.append(
                {
                    "category_id": category_id,
                    "category_key": category,
                    "tier": cell["tier"],
                    "source_backed_monthly": mean,
                    "maximum_suggested_monthly_reduction": mean,
                    "basis": "Stage 1 source-backed Tier 3/4 baseline role",
                }
            )

    estimate_cell = estimate.get("by_currency", {}).get(currency, {})
    irregular = str(estimate_cell.get("irregular_allowance_monthly", "0"))
    if Decimal(irregular) >= 0:
        residuals.append(
            ResidualCategory(
                category_id=f"manifest:irregular:{currency}",
                currency=currency,
                mean_monthly=irregular,
                highest_observed_monthly=irregular,
                label="Unresolved irregular allowance",
                column=COLUMN_IRREGULAR,
                provenance={
                    "source": "fincl.baseline.estimators.baseline_estimate",
                    "category_id": f"manifest:irregular:{currency}",
                    "essential_status": "unresolved",
                },
            )
        )
    return residuals, role_map, options


def _pace_rows(
    database: Path, *, month: str, currency: str, today: date
) -> dict[str, Any]:
    page = budgets_page(database, month=month, currency=currency)
    first = date.fromisoformat(f"{month}-01")
    last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    observation_end = min(today, last) if today >= first else None
    connection = connect_readonly(database)
    try:
        contract = _coverage_contract(connection)
        covered = bool(
            observation_end
            and any(
                interval.contains_interval(
                    DateInterval(first.isoformat(), observation_end.isoformat())
                )
                for interval in contract.covered_intervals
            )
        )
    finally:
        connection.close()
    return {
        **page,
        "budgets": [
            {
                **row,
                **budget_pace(
                    month=month,
                    as_of=today.isoformat(),
                    limit=str(row["limit"]),
                    observed_spending=str(row["actual"]),
                    coverage_supported=covered,
                ),
            }
            for row in page["budgets"]
        ],
        "coverage_through_observed_date": covered,
        "coverage_reasons": contract.reasons,
    }


def _horizon_obligation_gaps(page: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    for obligation in page.get("obligations", []):
        if (
            obligation.get("direction") != "outflow"
            or not obligation.get("stays_visible", True)
        ):
            continue
        if obligation.get("remaining_amount") is None:
            gaps.append(f"{obligation['obligation_id']} has no supported remaining amount")
        date_basis = obligation.get("date_basis") or {}
        if not date_basis.get("value") and not obligation.get("schedule"):
            gaps.append(f"{obligation['obligation_id']} has no supported settlement date")
    return gaps


def _breach_drivers(cell: dict[str, Any]) -> dict[str, Any] | None:
    conservative = cell["conservative"]
    zero = conservative.get("first_zero") or {}
    reserve = conservative.get("first_reserve_breach") or {}
    candidates = []
    if zero.get("occurs"):
        candidates.append((zero.get("on") or (zero.get("window") or {}).get("earliest"), "cash_zero"))
    if reserve.get("available") and reserve.get("occurs"):
        candidates.append((reserve.get("on") or (reserve.get("window") or {}).get("earliest"), "reserve_breach"))
    candidates = [(when, kind) for when, kind in candidates if when]
    if not candidates:
        return None
    breach_on, kind = min(candidates)
    day = next((row for row in conservative["days"] if row["date"] == breach_on), None)
    event_ids = set(day.get("event_ids", [])) if day else set()
    indexed = {row["event_id"]: row for row in conservative["events"]["items"]}
    events = [
        {
            "event_id": event_id,
            "source_id": indexed[event_id]["source_id"],
            "label": indexed[event_id]["label"],
            "amount": indexed[event_id]["amount"],
            "on": indexed[event_id]["on"],
            "edit_href": _driver_href(indexed[event_id]),
        }
        for event_id in sorted(event_ids)
        if event_id in indexed
    ]
    incomes = [
        {
            "event_id": event["event_id"],
            "label": event["label"],
            "amount": event["amount"],
            "on": event["on"],
            "assumption": (event.get("provenance") or {}).get("confidence", "included source income"),
            "edit_href": "?view=Planning",
        }
        for event in cell["base"]["events"]["items"]
        if event.get("direction") == "inflow" and event.get("on") > breach_on
    ][:5]
    return {
        "kind": kind,
        "on": breach_on,
        "events": events,
        "later_income_assumptions": incomes,
        "note": (
            "events on the first breach day and later source-income assumptions; "
            "later income does not erase the earlier breach"
        ),
    }


def _driver_href(event: dict[str, Any]) -> str:
    source = (event.get("provenance") or {}).get("source") or {}
    if not isinstance(source, dict):
        return "?view=Baseline"
    return {
        "personal_debt": "?view=Debts",
        "card_statement": "?view=Cards",
        "installment_plan": "?view=Installments",
        "recurring_commitment": "?view=Planning",
    }.get(str(source.get("kind")), "?view=Baseline")


def _event_options(cell: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in cell["base"]["events"]["items"]:
        provenance = event.get("provenance") or {}
        if provenance.get("kind") == "unscheduled_residual_allocation":
            continue
        event_id = str(event["event_id"])
        if event_id in seen:
            continue
        seen.add(event_id)
        tier = provenance.get("tier")
        if tier is None:
            link = provenance.get("baseline_link") or {}
            tier = link.get("tier")
        result.append(
            {
                "event_id": event_id,
                "source_id": event["source_id"],
                "label": event["label"],
                "amount": event["amount"],
                "on": event["on"],
                "direction": event["direction"],
                "tier": tier,
                "protected_tier_1_or_2": tier in (1, 2),
                "basis": "Stage 3B source event",
            }
        )
    return result


def spending_controls_page(
    database: Path,
    *,
    month: str | None = None,
    currency: str = "SGD",
    today: date,
    include_forecast: bool = True,
) -> dict[str, Any]:
    """§5.3: ``today`` is required — resolved once by the caller, never here."""
    with read_snapshot(database) as connection, request_cache(connection):
        page = _spending_controls_page(database, month=month, currency=currency, today=today)
        return page if include_forecast else _without_forecast_paths(page)


def _without_forecast_paths(page: dict[str, Any]) -> dict[str, Any]:
    """UI summary; keep full paths available to existing API/reader clients.

    Scenario construction still uses the complete cases on the server. Only
    the response omits them; displayed comparisons and evidence stay intact.
    """
    summary = {key: value for key, value in page.items() if key != "forecast_cases"}
    if page.get("scenario"):
        summary["scenario"] = {key: value for key, value in page["scenario"].items() if key != "cases"}
    return summary


def _spending_controls_page(
    database: Path, *, month: str | None, currency: str, today: date
) -> dict[str, Any]:
    selected_month = _month(month, today)
    roles = baseline_roles(database, today=today)
    estimate = baseline_estimate(database, today=today)
    residuals, role_map, category_options = _role_inputs(roles, estimate, currency)
    funds = sinking_funds_page(database, as_of=today.isoformat(), today=today)
    forecast = cash_timeline(database, today=today, residual_categories=residuals)
    cell = forecast.get("by_currency", {}).get(currency)
    obligations = obligations_page(
        database,
        as_of=(forecast.get("horizon") or {}).get("supported_cash_cutoff") or today.isoformat(),
        today=today,
    )
    pace = _pace_rows(database, month=selected_month, currency=currency, today=today)
    if not cell or not cell.get("available"):
        return {
            "available": False,
            "generation": forecast.get("generation"),
            "month": selected_month,
            "currency": currency,
            "reason": (cell or {}).get("reason") or forecast.get("reason") or "no supported currency forecast",
            "pace": pace,
            "limitations": [*forecast.get("limitations", []), *funds.get("limitations", [])],
            "what_if_options": {"categories": category_options, "events": []},
        }

    scale = int(cell["scale"])
    fund_currency = funds.get("cash_separation", {}).get("by_currency", {}).get(currency, {})
    allocations = [
        {
            "source_id": f"fund:{fund['fund']}:earmark",
            "event_id": f"fund:{fund['fund']}:earmark",
            "label": fund["name"],
            "amount": fund["earmark"]["outstanding"],
            "on": funds["as_of"],
            "edit_href": "?view=Controls#commitments",
            "tier": None,
        }
        for fund in funds.get("funds", [])
        if fund["currency"] == currency and Decimal(fund["earmark"]["outstanding"]) > 0
    ]
    commitments = group_commitments(
        events=cell["conservative"]["events"]["items"],
        role_by_category=role_map,
        internal_allocations=allocations,
        scale=scale,
    )
    cash_supported = bool(
        fund_currency.get("available")
        and funds.get("cash_separation", {}).get("cash_basis", {}).get("current_position_supported")
    )
    roles_currency = roles.get("by_currency", {}).get(currency, {})
    essential_supported = bool(
        roles.get("available")
        and roles_currency
        and Decimal(roles_currency.get("totals", {}).get("unresolved", "0")) == 0
    )
    obligation_gaps = _horizon_obligation_gaps(obligations)
    commitment_supported = bool(
        cell.get("available")
        and commitments["disjoint"]
        and not commitments["unresolved"]
        and not forecast.get("limitations")
        and not funds.get("limitations")
    )
    horizon_supported = not obligation_gaps and all(
        fund.get("status") != "incomplete" for fund in funds.get("funds", [])
    )
    gates = {
        "cash": {"supported": cash_supported, "reason": None if cash_supported else "today's liquid cash is not fully observed"},
        "commitments": {"supported": commitment_supported, "reason": None if commitment_supported else "one or more commitments are unresolved or not uniquely grouped"},
        "essential_scope": {"supported": essential_supported, "reason": None if essential_supported else "Tier 1/2 essential scope contains unresolved owner cost"},
        "horizon_end_obligations": {"supported": horizon_supported, "reason": None if horizon_supported else "an obligation or fund lacks a supported amount or settlement date"},
    }
    unsupported: list[str] = [
        str(gate["reason"])
        for gate in gates.values()
        if not gate["supported"] and gate["reason"]
    ]
    reserve_supported = cell.get("reserve_readiness", {}).get("available", False)
    if not reserve_supported:
        unsupported.append(
            str(
                cell.get("reserve_readiness", {}).get(
                    "reason", "minimum reserve is unavailable"
                )
            )
        )
    earmarked_minor = _minor(str(fund_currency.get("earmarked_funds", "0")), scale)
    reserve_minor = _minor(str(cell.get("reserve_readiness", {}).get("target", "0")), scale)
    capacity = conservative_capacity(
        case=cell["conservative"],
        earmarked_minor=earmarked_minor,
        reserve_minor=reserve_minor,
        scale=scale,
        supported=all(item["supported"] for item in gates.values()) and reserve_supported,
        unsupported_reasons=unsupported,
    )
    total_cash = fund_currency.get("total_cash")
    free_after = None
    if total_cash is not None:
        free_after = format_decimal(
            _minor(str(total_cash), scale)
            - _minor(commitments["external_cash_outflows"], scale)
            - _minor(commitments["internal_earmarks"], scale),
            scale,
        )
    unresolved = list(dict.fromkeys([
        *forecast.get("limitations", []),
        *funds.get("limitations", []),
        *commitments["unresolved"],
        *obligation_gaps,
        *unsupported,
    ]))
    return {
        "available": True,
        "generation": forecast.get("generation"),
        "as_of": today.isoformat(),
        "month": selected_month,
        "currency": currency,
        "horizon": forecast["horizon"],
        "pace": pace,
        "commitments": commitments,
        "free_cash_after_commitments": {
            "amount": free_after,
            "formula": "total cash − disjoint external commitment events − recorded internal earmarks",
            "income_included": False,
            "definitive_allowance": False,
        },
        "support_gates": gates,
        "capacity": capacity,
        "first_breach_drivers": _breach_drivers(cell),
        "what_if_options": {
            "categories": category_options,
            "events": _event_options(cell),
        },
        "forecast_cases": {"base": cell["base"], "conservative": cell["conservative"]},
        "scenario": None,
        "unresolved": unresolved,
        "disclosure": (
            "Only recorded evidence is in the default. A negative capacity is a funding need. "
            "Proposed changes are isolated and never become Confirmed."
        ),
    }


def spending_controls_what_if(
    database: Path,
    *,
    month: str,
    currency: str,
    category_cuts: list[dict[str, str]],
    date_changes: list[dict[str, str]],
    hypothetical_income: list[dict[str, str]],
    today: date,
    include_forecast: bool = True,
) -> dict[str, Any]:
    page = spending_controls_page(database, month=month, currency=currency, today=today)
    if not page.get("available"):
        return page
    category_ids = {row["category_id"] for row in page["what_if_options"]["categories"]}
    event_ids = {row["event_id"] for row in page["what_if_options"]["events"]}
    for cut in category_cuts:
        if cut.get("category_id") not in category_ids:
            raise FinCLError("A cut must select a source-backed Tier 3/4 category")
        if ExactMoney.parse(str(cut.get("monthly_reduction", "0"))).coefficient <= 0:
            raise FinCLError("A proposed cut must be a positive exact amount")
    for change in date_changes:
        if change.get("event_id") not in event_ids:
            raise FinCLError("A date change must select a source-backed event")
        try:
            date.fromisoformat(str(change.get("new_date")))
        except ValueError as exc:
            raise FinCLError("A proposed date must use YYYY-MM-DD") from exc
    start = page["horizon"]["supported_cash_cutoff"]
    end = page["horizon"]["end"]
    for income in hypothetical_income:
        if income.get("currency", currency) != currency:
            raise FinCLError("Hypothetical income must use the selected currency")
        if ExactMoney.parse(str(income.get("amount", "0"))).coefficient <= 0:
            raise FinCLError("Hypothetical income must be a positive exact amount")
        if not start < str(income.get("on")) <= end:
            raise FinCLError("Hypothetical income must fall inside the forecast horizon")

    fund_currency = page["commitments"]["groups"]["planned_allocations"]
    scale = int(page["forecast_cases"]["conservative"]["scale"])
    earmarked_minor = _minor(fund_currency["amount"], scale)
    reserve_cell = page["forecast_cases"]["conservative"].get("first_reserve_breach", {})
    reserve_minor = _minor(str(reserve_cell.get("reserve_target", "0")), scale)
    scenario_cases: dict[str, Any] = {}
    comparisons: dict[str, Any] = {}
    scenario_unresolved: list[str] = []
    for name in ("base", "conservative"):
        before = page["forecast_cases"][name]
        after, unresolved = apply_what_if_case(
            case=before,
            currency=currency,
            category_cuts=category_cuts,
            date_changes=date_changes,
            hypothetical_income=hypothetical_income,
        )
        scenario_cases[name] = after
        comparisons[name] = case_comparison(
            before,
            after,
            earmarked_minor=earmarked_minor,
            reserve_minor=reserve_minor,
        )
        scenario_unresolved.extend(unresolved)
    page["scenario"] = {
        "hypothetical": True,
        "confirmed": False,
        "statement": "Proposed only. No cut, date change or income has happened or been recorded.",
        "inputs": {
            "category_cuts": category_cuts,
            "date_changes": date_changes,
            "hypothetical_income": hypothetical_income,
        },
        "cases": scenario_cases,
        "comparison": comparisons,
        "unresolved": list(dict.fromkeys(scenario_unresolved)),
        "capacity_is_not_an_allowance": True,
    }
    page["unresolved"] = list(dict.fromkeys([*page["unresolved"], *scenario_unresolved]))
    return page if include_forecast else _without_forecast_paths(page)


__all__ = ["spending_controls_page", "spending_controls_what_if"]
