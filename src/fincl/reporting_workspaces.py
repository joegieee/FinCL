"""Read-only reporting annotations over the remaining native workspaces.

Annotations identify the native field by JSON path. They never replace native
values, participate in matching, or enter a command payload. Every composed
reader, policy and rate query borrows one database snapshot.

Each amount is valued for its own purpose at its own date. A total that spans
several dates is composed from its dated parts (valued first, combined exactly,
rounded once); a foreign-currency total with no dated decomposition is reported
as unavailable rather than converted at one rate.
"""
from __future__ import annotations

import calendar
import inspect
import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import date, timedelta
from fractions import Fraction
from functools import wraps
from typing import Any, get_type_hints

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from . import clock
from .db import read_snapshot
from .errors import FinCLError
from .money import ExactMoney, format_decimal
from .reporting_context import ReportedScope, ReportingContext, build_reporting_context
from .reporting_currency.models import CURRENCY_REGISTRY
from .valuation import (
    FLOW_RATE_V1,
    FORWARD_MONTHLY_CLOSED_V1,
    POSITION_MONTHLY_CLOSED_V1,
    EntryPrecision,
    ExactValuation,
    Unavailability,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    ValuedMoneyDTO,
    to_dto,
    value_one,
)


class WorkspaceAmount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: list[str | int]
    label: str
    value: ValuedMoneyDTO


class ReportedWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: str
    reporting_currency: str
    valuation_as_of: date
    native: dict[str, Any]
    amounts: list[WorkspaceAmount]


Path = list[str | int]

# Closed inventory of monetary fields. Rates, percentages, counts, dates,
# predicates and native editor input strings deliberately do not qualify.
MONEY_FIELDS = frozenset(["amount", "principal", "fee", "rounding_remainder", "waiver", "remaining_principal", "known_principal", "remaining_obligation", "observed_total", "credit_balance_amount", "over_limit_amount", "headroom", "limit", "target", "saved", "remaining", "actual", "expected", "previous_limit", "new_limit", "previous_remaining", "new_remaining", "monthly_amount", "expected_amount", "min_amount", "max_amount", "total_received", "gross_cost", "owner_cost_recorded", "owner_cost_baseline", "identified_recurring", "unresolved_irregular", "adjustments", "routine_variable_residual", "income", "essential", "discretionary", "unresolved", "total", "fixed_recurring_monthly", "variable_residual_monthly", "variable_residual_median", "variable_residual_min", "variable_residual_max", "irregular_allowance_monthly", "routine_lifestyle_baseline", "total_observed_monthly", "net_received_income_monthly", "historical_surplus_monthly", "essential_monthly", "earlier_mean_owner_cost", "recent_mean_owner_cost", "excluded_routine_residual", "bank_withdrawal", "via_card_statement", "monthly_baseline_portion", "remaining_historical_portion", "amount_due", "required_contribution", "allocated_now", "total_cash", "earmarked_funds", "unallocated_cash", "minimum_reserve", "reserve_adjusted_unallocated_cash", "shortfall", "insufficient_allocation", "outstanding", "open_amount", "net_cash_effect", "receipts", "payments", "realized_amount", "cash_effect", "pace_projected_spend", "pace_estimated_headroom", "external_cash_outflows", "internal_earmarks", "additional_spending_capacity", "funding_need", "minimum_conservative_unallocated_reserve_adjusted_balance", "amount_per_week", "before_gap", "after_gap", "cash_improvement", "starting_balance", "reserve_target", "closing_balance", "opening_balance", "inflows", "outflows", "net_cash_change", "reserve_adjusted_closing", "minimum_balance", "opening_liability", "closing_liability", "credit_limit", "minimum_payment", "statement_due", "statement_payment_due", "recognized_principal", "financing_fee", "outstanding_earmark", "released", "amount_remaining", "known_amount", "unclassified_receipts", "unclassified_payments", "principal_amount", "fee_amount", "cost_amount", "absolute_amount", "left_amount", "right_amount", "other_inflow", "pending_inflow", "refunds_received", "reimbursements_received", "unmatched_receipts", "owner_cost_recorded_total", "owner_cost_baseline_total", "unmatched_receipts_total", "recorded_minus_baseline", "cross_month_refund_in", "cross_month_refund_out", "expected_from_allocations", "mean_monthly", "highest_observed_monthly", "source_backed_monthly", "maximum_suggested_monthly_reduction", "refunds", "net_cost", "refund_amount", "planned", "matched", "over_plan"])

# Rule thresholds and matching tolerances are editor/rule inputs: they stay native.
NATIVE_ONLY_FIELDS = frozenset(["threshold", "held_out_threshold", "amount_tolerance", "suggested_tolerance"])
# Fields that are native only beneath a given container. A match suggestion's
# difference and suggested amount compare native amounts and feed the command.
NATIVE_ONLY_UNDER = {"match_suggestions": frozenset(["outstanding", "amount_difference", "suggested_amount"])}
# Subtrees no retained screen renders from these reads. Forecast cases have
# their own reported Forecast workspace; per-day rows and allocation metadata
# are model internals. Reconciliation checks are native statement equations.
# The combined "will I have enough" answer is already in the reporting currency.
SKIPPED_SUBTREES = frozenset(["forecast_cases", "days", "allocation_metadata", "combined_answer"])

# Totals spanning several dates. If one is not composed from dated parts and
# its currency differs from the reporting currency, it is reported unavailable.
MULTI_PERIOD_FIELDS = frozenset(["total_received", "observed_principal_paid", "settled_principal",
                                 "owner_cost_recorded_total", "owner_cost_baseline_total",
                                 "unmatched_receipts_total", "recorded_minus_baseline",
                                 "cross_month_refund_in", "cross_month_refund_out", "expected_from_allocations"])

# Amounts owed or held at a date are stocks (POSITION), not flows.
POSITION_PARTS = ("remaining_principal", "known_principal", "remaining_obligation", "remaining_amount", "liability",
                  "credit_limit", "limit_basis", "headroom", "credit_balance", "over_limit", "payment_due",
                  "minimum_payment", "due_fields", "target", "saved", "allocated_now", "cash_separation",
                  "reserve_target", "starting_balance", "superseded_lump_sum")
FUTURE_PARTS = ("schedule", "planned_contributions", "capacity", "commitments", "forecast_cases", "first_breach",
                "scenario", "accounted_for", "drivers", "shortfall", "what_if_options")

READ_PREFIXES = (
    "debts", "cards", "installments", "obligations", "baseline", "funds",
    "budgets", "goals", "recurring", "spending-controls", "forecast/variance",
    "planning/sufficiency", "categorization", "relationships", "overview", "trips", "reports",
    "planned",
)
READ_POSTS = frozenset({"reports/preview", "classification/preview", "categorization/impact", "categorization/derive-rule",
                        "rules/preview", "rules/compile", "rules/compile-combined",
                        "spending-controls/what-if"})

_POLICY = {ValuationPurpose.POSITION: POSITION_MONTHLY_CLOSED_V1,
           ValuationPurpose.FORECAST: FORWARD_MONTHLY_CLOSED_V1,
           ValuationPurpose.FLOW: FLOW_RATE_V1}
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")
_DATE_KEYS = ("effective_on", "effective_at", "on", "date", "month", "observed_on", "allocated_on", "statement_date",
              "period_end", "modelled_on", "modelled_month", "effective_month", "opened_on", "landed_on", "last_date",
              "as_of", "end")


def _day(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}", value):
            year, month = map(int, value.split("-"))
            return date(year, month, calendar.monthrange(year, month)[1])
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _get(node: Any, path: Iterable[str | int]) -> Any:
    for segment in path:
        if isinstance(node, dict):
            node = node.get(segment)
        elif isinstance(node, list) and isinstance(segment, int) and 0 <= segment < len(node):
            node = node[segment]
        else:
            return None
    return node


def _default_currency(native: dict[str, Any], resource: str, connection: Any) -> str | None:
    """The owner's currency when the native payload omits it on the amount."""
    plan = native.get("plan")
    if isinstance(plan, dict) and plan.get("currency"):
        return str(plan["currency"])
    match = re.fullmatch(r"debts/([^/]+)/events", resource)
    if match:
        row = connection.execute("SELECT currency FROM personal_debts WHERE debt_id=?", (match.group(1),)).fetchone()
        return str(row[0]) if row else None
    return None


def annotate_workspace(native: dict[str, Any], *, context: ReportingContext, resource: str) -> ReportedWorkspace:
    amounts: list[WorkspaceAmount] = []
    exact: dict[tuple[str | int, ...], ExactValuation] = {}
    composed: set[tuple[str | int, ...]] = set()
    issue = context.scope.valuation_as_of
    reporting = context.policy.currency
    forecast_issue = _day((native.get("snapshot") or {}).get("issued_at")) or issue
    conversion_date = _day((native.get("conversion") or {}).get("effective_on")) or _day((native.get("plan") or {}).get("opened_on"))

    def value(amount: str, currency: str, purpose: ValuationPurpose, dated: date | None, mark: date) -> ExactValuation:
        if purpose is ValuationPurpose.FORECAST:
            as_of = forecast_issue
        elif purpose is ValuationPurpose.POSITION:
            as_of = min(mark, issue)
        else:
            as_of = mark
        return value_one(context.connection, ValuationItem(
            native=ExactMoney.parse(amount), native_currency=currency, purpose=purpose,
            precision=EntryPrecision.DAY if dated else EntryPrecision.UNKNOWN, effective_on=dated,
        ), context=ValuationContext(
            reporting_currency=reporting, purpose=purpose, valuation_as_of=as_of,
            rate_selection_policy=_POLICY[purpose],
            forward_policy=FORWARD_MONTHLY_CLOSED_V1 if purpose is ValuationPurpose.FORECAST else None,
        ))

    def walk(node: Any, path: Path, currency: str | None, represented: date | None, label: str = "",
             opening: bool = False, period_start: date | None = None) -> None:
        if isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, [*path, index], currency, represented, label, opening, period_start)
            return
        if not isinstance(node, dict):
            return
        currency = node.get("currency") or node.get("currency_pocket") or currency
        opening = bool(node.get("opening_balance", opening))
        period_start = _day(node.get("period_start")) or period_start
        label = str(node.get("name") or node.get("label") or node.get("counterparty_label") or label)
        dated = next((_day(node.get(key)) for key in _DATE_KEYS if _day(node.get(key)) is not None), represented)
        observed_date = _day((node.get("observed_date") or {}).get("value")) if isinstance(node.get("observed_date"), dict) else None
        dated = observed_date or dated
        # Utilization and the limit basis are read off the latest statement.
        statement_date = _day((node.get("latest_statement") or {}).get("statement_date")) if isinstance(node.get("latest_statement"), dict) else None
        native_only = NATIVE_ONLY_FIELDS.union(*(fields for container, fields in NATIVE_ONLY_UNDER.items() if container in path))
        for key, child in node.items():
            if key in {"commands", "payload", "match", "anchors"} or key in SKIPPED_SUBTREES:
                continue
            if key == "checks" and "reconciliation" in path:
                continue
            child_path = [*path, key]
            if key in {"by_currency", "totals_by_currency", "reconciliation"} and isinstance(child, dict) \
                    and child and all(re.fullmatch(r"[A-Z]{3}", str(ccy)) for ccy in child):
                for ccy, grandchild in child.items():
                    walk(grandchild, [*child_path, ccy], ccy, dated, label, opening, period_start)
                continue
            if isinstance(child, (dict, list)):
                child_dated = statement_date if key in {"utilization", "limit_basis"} and statement_date else \
                    conversion_date if key == "recognized_purchase_cost" and conversion_date else dated
                walk(child, child_path, currency, child_dated, label, opening, period_start)
                continue
            if key in native_only:
                continue
            wire = key == "value" and ("currency" in node and "kind" not in node or node.get("kind") == "money" or (bool(path) and path[-1] == "observed_amount"))
            # String-form owner amounts and WireMoney both retain exact text.
            field_currency = node.get(key.removesuffix("_amount") + "_currency", currency) if key in {"left_amount", "right_amount"} else currency
            if not (key in MONEY_FIELDS or wire) or not isinstance(child, str) or not field_currency:
                continue
            if not _NUMBER.fullmatch(child):
                continue
            trail = "/".join(map(str, child_path))
            position = key in {"limit", "previous_limit", "new_limit"} or any(part in trail for part in POSITION_PARTS)
            future = any(part in trail for part in FUTURE_PARTS) or (resource.startswith("recurring") and "occurrences" not in trail)
            purpose = ValuationPurpose.POSITION if position else ValuationPurpose.FORECAST if future else ValuationPurpose.FLOW
            if "principal_history" in path:
                purpose = ValuationPurpose.POSITION if opening else ValuationPurpose.FLOW
            # Derived baseline estimates describe a monthly reference model,
            # not a new historical receipt. Dated observations remain FLOW.
            if resource.startswith("baseline") and not resource.startswith("baseline/contributors") \
                    and not any(part in path for part in ("months", "irregular_risk_entries", "occurrences", "reconciliation", "tasks")):
                purpose = ValuationPurpose.FORECAST
            if resource.startswith("obligations") and "baseline_component_effects" in path:
                purpose = ValuationPurpose.FORECAST
            if resource.startswith("forecast/variance") and key == "expected":
                purpose = ValuationPurpose.FORECAST
            if resource == "overview" and "recurring" in path:
                purpose = ValuationPurpose.FORECAST
            # A planned amount is a forward figure; a matched payment or a
            # trip's spend is a dated flow.
            if resource == "planned":
                purpose = ValuationPurpose.FLOW if any(part in path for part in ("matches", "actual_parts")) \
                    else ValuationPurpose.FORECAST
            field_dated = _day(node.get(key.removesuffix("_amount") + "_date")) or dated if key in {"left_amount", "right_amount"} else dated
            mark = _day(node.get(key + "_as_of")) or field_dated or issue
            if node.get("type") == "goal" and key in {"actual", "remaining"}:
                purpose = ValuationPurpose.POSITION
                mark = issue
            if path and path[-1] in {"remaining_principal", "remaining_obligation", "remaining_amount"}:
                mark = _day(native.get("as_of")) or issue
            if node.get("type") == "goal" and key == "target":
                mark = issue
            # An opening liability is the balance the instant before the period opened.
            if "opening_liability" in path and period_start is not None:
                mark = period_start - timedelta(days=1)
            valuation = value(child, field_currency, purpose, field_dated, mark)
            exact[tuple(child_path)] = valuation
            field_label = str(path[-1]) if key == "value" and path else key
            amounts.append(WorkspaceAmount(path=child_path, label=(label + " · " if label else "") + field_label.replace("_", " "),
                                           value=to_dto(valuation, currency_registry=CURRENCY_REGISTRY)))

    walk(native, [], _default_currency(native, resource, context.connection), None)

    def publish(path: Path, valuation: ExactValuation) -> None:
        exact[tuple(path)] = valuation
        dto = to_dto(valuation, currency_registry=CURRENCY_REGISTRY)
        if len({rate.period_month for rate in valuation.rate_path}) > 1:
            dto = dto.model_copy(update={"rate_period": None})
        for index, annotation in enumerate(amounts):
            if annotation.path == path:
                amounts[index] = annotation.model_copy(update={"value": dto})
                return

    def combine(path: Path, references: list[tuple[Path, Fraction]]) -> None:
        """Value operands first; combine exact fractions; round only at DTO.

        The references are the native owner's decomposition, not a new
        financial model. The native scalar and its status remain unchanged.
        """
        target = exact.get(tuple(path))
        children = [(exact.get(tuple(child)), weight) for child, weight in references]
        if target is None or not children or any(child is None for child, _ in children):
            return
        resolved = [(child, weight) for child, weight in children if child is not None]
        missing = next((child.unavailable for child, _ in resolved if child.unavailable), None)
        total = None if missing else sum((child.reported_exact * weight for child, weight in resolved if child.reported_exact is not None), Fraction())
        result = replace(target, reported_exact=total, unavailable=missing, basis=resolved[0][0].basis,
                         rate_path=tuple(dict.fromkeys(rate for child, _ in resolved for rate in child.rate_path)))
        purposes = {child.context.purpose for child, _ in resolved}
        if len(purposes) == 1:
            # A composite has the purpose of its parts; a flow total is dated by its latest part.
            result = replace(result, context=replace(resolved[0][0].context,
                valuation_as_of=max(child.context.valuation_as_of for child, _ in resolved)))
        composed.add(tuple(path))
        publish(path, result)

    def kind_parts(parts_path: Path, parts: list[Any], kind: str, currency: str | None = None) -> list[tuple[Path, Fraction]]:
        """A cost or refund total's dated parts. With none of that kind (no
        refunds, say), the same owner's other dated parts at weight zero: the
        total is exactly zero and keeps the rates and dates it sits beside,
        instead of reading as unconverted."""
        mine = [index for index, part in enumerate(parts) if currency in (None, part.get("currency"))]
        chosen = [([*parts_path, index, "amount"], Fraction(1)) for index in mine if parts[index].get("kind") == kind]
        return chosen or [([*parts_path, index, "amount"], Fraction(0)) for index in mine]

    def value_sources(prefix: Path, rows: Iterable[tuple[str, str, str | None]]) -> list[Path]:
        """Value dated native source rows (amount, currency, date) as FLOW at their own dates."""
        paths: list[Path] = []
        for index, (amount, currency, on) in enumerate(rows):
            day = _day(on)
            source_path: Path = ["_source", *prefix, index]
            exact[tuple(source_path)] = value(amount, currency, ValuationPurpose.FLOW, day, day or issue)
            paths.append(source_path)
        return paths

    def each(container: Any, path: Path) -> Iterable[tuple[int, dict[str, Any], Path]]:
        if isinstance(container, list):
            for index, item in enumerate(container):
                if isinstance(item, dict):
                    yield index, item, [*path, index]

    def pick(rows: list[dict[str, Any]], field: str, choose: Callable[[list[Fraction]], Fraction]) -> int | None:
        values = [Fraction(str(row.get(field))) for row in rows if _NUMBER.fullmatch(str(row.get(field)))]
        if len(values) != len(rows) or not values:
            return None
        return values.index(choose(values))

    # Baseline: historical means value each month first. Each figure is composed
    # from the same terms the estimator adds natively (flat fixed cost plus the
    # monthly partition), never from owner cost, which also carries the matched
    # recurring cost and the receipt adjustments the model excludes.
    def baseline_estimate_cell(root: Path, cell: dict[str, Any]) -> None:
        rows = cell.get("months", [])
        if not rows:
            return

        def mean(field: str) -> list[tuple[Path, Fraction]]:
            return [([*root, "months", index, field], Fraction(1, len(rows))) for index in range(len(rows))]

        fixed: list[tuple[Path, Fraction]] = [([*root, "fixed_recurring_monthly"], Fraction(1))]
        routine = fixed + mean("routine_variable_residual")
        total = routine + mean("unresolved_irregular")
        for name, parts in {
            "variable_residual_monthly": mean("routine_variable_residual"),
            "irregular_allowance_monthly": mean("unresolved_irregular"),
            "routine_lifestyle_baseline": routine,
            "total_observed_monthly": total,
            "net_received_income_monthly": mean("income"),
            "historical_surplus_monthly": mean("income") + [(path, -weight) for path, weight in total],
        }.items():
            combine([*root, name], parts)
        months = {row.get("month"): index for index, row in enumerate(rows)}
        for name, (fields, with_fixed) in {
            "routine_lifestyle_baseline": (("routine_variable_residual",), True),
            "irregular_allowance_monthly": (("unresolved_irregular",), False),
            "total_observed_monthly": (("routine_variable_residual", "unresolved_irregular"), True),
        }.items():
            spread = (cell.get("observed_range") or {}).get(name) or {}
            for bound in ("min", "max"):
                index = months.get(spread.get(f"{bound}_month"))
                if index is not None:
                    combine([*root, "observed_range", name, f"{bound}_amount"],
                            [([*root, "months", index, field], Fraction(1)) for field in fields] + (fixed if with_fixed else []))

    def roles_totals(root: Path, roles: dict[str, Any]) -> None:
        for field in ("essential", "discretionary", "unresolved", "total"):
            combine([*root, "totals", field], [([*root, "months", index, field], Fraction(1))
                                              for index in range(len(roles.get("months", [])))])

    def role_groups(groups: Any, root: Path) -> None:
        for _, group, path in each(groups, root):
            months = group.get("months") or []
            if not months:
                continue
            parts = [[*path, "months", index, "amount"] for index in range(len(months))]
            combine([*path, "amount"], [(part, Fraction(1)) for part in parts])
            combine([*path, "mean_monthly"], [(part, Fraction(1, len(parts))) for part in parts])
            highest = pick(months, "amount", max)
            if highest is not None:
                combine([*path, "highest_observed_monthly"], [(parts[highest], Fraction(1))])

    def income_streams(streams: Any, root: Path) -> None:
        for _, stream, path in each(streams, root):
            occurrences = stream.get("occurrences") or []
            if not occurrences:
                continue
            parts = [[*path, "occurrences", index, "amount"] for index in range(len(occurrences))]
            combine([*path, "total_received"], [(part, Fraction(1)) for part in parts])
            for field, choose in (("min_amount", min), ("max_amount", max)):
                chosen = pick(occurrences, "amount", choose)
                if chosen is not None:
                    combine([*path, field], [(parts[chosen], Fraction(1))])

    def reconciliation_totals(month_cells: list[tuple[Path, dict[str, Any]]], root: Path) -> None:
        for field in ("owner_cost_recorded", "owner_cost_baseline", "unmatched_receipts"):
            parts = [([*cell_path, field], Fraction(1)) for cell_path, cell in month_cells if field in cell]
            if parts and len(parts) == len(month_cells):
                combine([*root, field + "_total"], parts)
        if {(*root, "owner_cost_recorded_total"), (*root, "owner_cost_baseline_total")} <= composed:
            combine([*root, "recorded_minus_baseline"], [([*root, "owner_cost_recorded_total"], Fraction(1)),
                                                         ([*root, "owner_cost_baseline_total"], Fraction(-1))])

    if resource.startswith("baseline"):
        for currency, cell in (native.get("by_currency") or {}).items():
            root: Path = ["by_currency", currency]
            if resource.startswith("baseline/estimate"):
                baseline_estimate_cell(root, cell)
                if cell.get("roles"):
                    roles_totals([*root, "roles"], cell["roles"])
                role_groups(cell.get("role_groups"), [*root, "role_groups"])
                reconciliation_totals([([*root, "months", index], month) for index, month in enumerate(cell.get("months", []))],
                                      ["reconciliation", currency])
            elif resource.startswith("baseline/roles"):
                roles_totals(root, cell)
        if resource.startswith("baseline/roles"):
            role_groups(native.get("groups"), ["groups"])
        if resource.startswith("baseline/cost-income"):
            for currency in (native.get("reconciliation") or {}):
                cells = [(["months", index, "by_currency", currency], month["by_currency"][currency])
                         for index, month in enumerate(native.get("months", [])) if currency in month.get("by_currency", {})]
                reconciliation_totals(cells, ["reconciliation", currency])
        income_streams(native.get("income_streams"), ["income_streams"])
        for _, part, path in each(native.get("parts"), ["parts"]):
            combine([*path, "total"], [([*path, "contributors", index, "amount"], Fraction(1))
                                       for index in range(len(part.get("contributors") or []))])

    if resource.startswith("forecast/variance"):
        events = native.get("events", [])
        for index, event in enumerate(events):
            combine(["events", index, "actual"], [(["events", index, "matches", j, "amount"], Fraction(1))
                                                  for j in range(len(event.get("matches", [])))])
        for index, month in enumerate(native.get("months", [])):
            for j, group in enumerate(month.get("by_flow_class", [])):
                prefix: Path = ["months", index, "by_flow_class", j]
                # The native owner publishes the cash-effect sign explicitly.
                if group.get("actual") is not None and group.get("expected") is not None and group.get("cash_effect") is not None:
                    sign = Fraction(1 if group.get("direction") == "inflow" else -1)
                    combine([*prefix, "cash_effect"], [([*prefix, "actual"], sign), ([*prefix, "expected"], -sign)])
            combine(["months", index, "net_cash_effect"], [(["months", index, "by_flow_class", j, "cash_effect"], Fraction(1))
                                                            for j in range(len(month.get("by_flow_class", [])))])
        by_id = {str(event.get("event_id")): index for index, event in enumerate(events)}
        for _, miss, path in each(native.get("biggest_misses"), ["biggest_misses"]):
            source = by_id.get(str(miss.get("event_id")))
            if source is None:
                continue
            combine([*path, "expected"], [(["events", source, "expected"], Fraction(1))])
            if miss.get("actual") is not None:
                combine([*path, "actual"], [(["events", source, "actual"], Fraction(1))])
                sign = Fraction(1 if events[source].get("direction") == "inflow" else -1)
                combine([*path, "cash_effect"], [([*path, "actual"], sign), ([*path, "expected"], -sign)])
        realization = native.get("likely_income_realization") or {}
        located = [(path, by_id.get(str(item.get("event_id"))))
                   for _, item, path in each(realization.get("observations"), ["likely_income_realization", "observations"])]
        observed = [(path, source) for path, source in located if source is not None]
        if observed and len(observed) == len(located):
            for path, source in observed:
                combine([*path, "expected"], [(["events", source, "expected"], Fraction(1))])
                if _get(native, [*path, "actual"]) is not None:
                    combine([*path, "actual"], [(["events", source, "actual"], Fraction(1))])
            root = ["likely_income_realization"]
            combine([*root, "expected_amount"], [(["events", source, "expected"], Fraction(1)) for _, source in observed])
            combine([*root, "realized_amount"], [(["events", source, "actual"], Fraction(1)) for _, source in observed
                                                 if events[source].get("actual") is not None])
            discount = _get(native, [*root, "suggested_discount", "amount"])
            if discount is not None and Fraction(str(discount)) > 0:
                combine([*root, "suggested_discount", "amount"], [([*root, "expected_amount"], Fraction(1)),
                                                                  ([*root, "realized_amount"], Fraction(-1))])

    if resource.startswith("obligations"):
        lanes: dict[tuple[str, str], list[Path]] = {}
        for _, obligation, path in each(native.get("obligations"), ["obligations"]):
            payments = obligation.get("actual_payments") or {}
            entries = payments.get("entries") or []
            if entries:
                combine([*path, "actual_payments", "total"], [([*path, "actual_payments", "entries", j, "amount"], Fraction(1))
                                                              for j in range(len(entries))])
            if obligation.get("remaining_amount") is not None and obligation.get("direction") == "outflow" and obligation.get("stays_visible"):
                lane = "via_card_statement" if obligation.get("cash_effect") == "via_card_statement" else "bank_withdrawal"
                lanes.setdefault((str(obligation.get("currency")), lane), []).append([*path, "remaining_amount", "value"])
        for (currency, lane), parts in lanes.items():
            combine(["totals_by_currency", currency, lane], [(part, Fraction(1)) for part in parts])

    if resource.startswith("installments"):
        plans = [(item, path) for _, item, path in each(native.get("plans"), ["plans"])]
        if isinstance(native.get("plan"), dict):
            plans.append((native["plan"], ["plan"]))
        for plan, path in plans:
            plan_id, currency = plan.get("plan_id"), plan.get("currency")
            if not plan_id or not currency:
                continue
            occurrences = context.connection.execute(
                "SELECT amount_coefficient, amount_scale, effective_on FROM installment_occurrences WHERE plan_id=? ORDER BY effective_on, occurrence_id",
                (plan_id,)).fetchall()
            settlements = context.connection.execute(
                "SELECT principal_coefficient, principal_scale, settled_on FROM installment_settlements WHERE plan_id=? AND bound=1 ORDER BY settled_on, settlement_id",
                (plan_id,)).fetchall()
            for field, rows in (("observed_principal_paid", occurrences), ("settled_principal", settlements)):
                if not rows:
                    continue
                parts = value_sources([plan_id, field], ((format_decimal(int(row[0]), int(row[1])), currency, row[2]) for row in rows))
                combine([*path, "progress", field, "value"], [(part, Fraction(1)) for part in parts])

    if resource.startswith("trips"):
        summaries = [(item, path) for _, item, path in each(native.get("trips"), ["trips"])]
        if isinstance(native.get("trip"), dict):
            summaries.append((native["trip"], ["trip"]))
        for summary, path in summaries:
            parts = summary.get("cost_parts") or []
            for currency in summary.get("totals_by_currency") or {}:
                totals: Path = [*path, "totals_by_currency", currency]
                for field, kind in (("gross_cost", "cost"), ("refunds", "refund")):
                    combine([*totals, field], kind_parts([*path, "cost_parts"], parts, kind, currency))
                combine([*totals, "net_cost"], [([*totals, "gross_cost"], Fraction(1)), ([*totals, "refunds"], Fraction(-1))])
        contributors = native.get("contributors") or []
        for index, row in enumerate(native.get("by_category") or []):
            combine(["by_category", index, "cost_amount"], [
                (["contributors", j, "categories", k, "cost_amount"], Fraction(1))
                for j, member in enumerate(contributors) if member.get("currency") == row.get("currency")
                for k, part in enumerate(member.get("categories") or [])
                if part.get("category_key") == row.get("category_key")])

    if resource.startswith("reports"):
        rows = native.get("rows") or []
        for index, row in enumerate(rows):
            path = ["rows", index]
            for field, kind in (("gross_cost", "cost"), ("refunds", "refund")):
                combine([*path, field], kind_parts([*path, "cost_parts"], row.get("cost_parts") or [], kind))
            combine([*path, "net_cost"], [([*path, "gross_cost"], Fraction(1)), ([*path, "refunds"], Fraction(-1))])
        for currency in native.get("totals_by_currency") or {}:
            report_totals: Path = ["totals_by_currency", currency]
            for field in ("gross_cost", "refunds", "net_cost"):
                combine([*report_totals, field], [(["rows", index, field], Fraction(1))
                                           for index, row in enumerate(rows) if row.get("currency") == currency])

    for _, budget, path in each(native.get("budgets"), ["budgets"]):
        if budget.get("remaining") is not None:
            combine([*path, "remaining"], [([*path, "limit"], Fraction(1)), ([*path, "actual"], Fraction(-1))])

    if resource == "classification/preview":
        sources: dict[str, list[Path]] = {}
        for index, row in enumerate(native.get("rows", [])):
            entry = context.connection.execute(
                "SELECT currency_pocket, amount_text, effective_at FROM ledger_entries WHERE natural_anchor=?",
                (row["anchor"],),
            ).fetchone()
            if entry is None:
                continue
            magnitude = str(entry["amount_text"]).lstrip("+-")
            sources.setdefault(entry["currency_pocket"], []).extend(
                value_sources(["classification", index], [(magnitude, entry["currency_pocket"], entry["effective_at"])]))
        for index, group in enumerate(native.get("by_currency", [])):
            combine(["by_currency", index, "absolute_amount"], [(path, Fraction(1)) for path in sources.get(group["currency"], [])])

    def breakdown_totals(node: Any, path: Path) -> None:
        if isinstance(node, list):
            for index, child in enumerate(node):
                breakdown_totals(child, [*path, index])
        elif isinstance(node, dict):
            if "valuation_items" in node:
                for currency in node.get("by_currency", {}):
                    combine([*path, "by_currency", currency, "absolute_amount"],
                            [([*path, "valuation_items", index, "amount"], Fraction(1))
                             for index, row in enumerate(node["valuation_items"]) if row["currency"] == currency])
            for key, child in node.items():
                breakdown_totals(child, [*path, key])
    breakdown_totals(native, [])

    # A multi-date total without a dated decomposition is not converted at one rate.
    for annotation in list(amounts):
        key = tuple(annotation.path)
        field = next((str(p) for p in reversed(annotation.path) if isinstance(p, str) and p != "value"), "")
        valuation = exact[key]
        multi_period = field in MULTI_PERIOD_FIELDS or (field == "absolute_amount" and "tasks" in annotation.path)
        if multi_period and key not in composed and valuation.native_currency != reporting \
                and not valuation.native.is_zero():
            publish(annotation.path, replace(valuation, reported_exact=None, basis=None, rate_path=(), unavailable=Unavailability(
                "NO_DATED_BREAKDOWN",
                f"This {valuation.native_currency} total spans several dates and has no dated breakdown, so it is not converted at a single rate.",
                "Use the dated rows it sums, or read the original amount.")))

    return ReportedWorkspace(generation=context.generation_token, reporting_currency=reporting,
                             valuation_as_of=issue, native=native, amounts=amounts)


def register_workspace_reports(app: FastAPI, database: Any) -> None:
    """Mirror the closed native read inventory, preserving route validation."""
    def adapt(route: APIRoute, resource: str) -> Any:
        endpoint = route.endpoint

        @wraps(endpoint)
        def reported(**kwargs: Any) -> ReportedWorkspace:
            with read_snapshot(database) as connection:
                try:
                    context = build_reporting_context(connection, scope=ReportedScope(valuation_as_of=clock.today()))
                    expected = kwargs.get("generation")
                    if expected and expected != context.generation_token:
                        raise FinCLError("STALE_GENERATION: reload this workspace")
                    if resource in {"budgets", "overview"}:
                        currencies = [str(row[0]) for row in connection.execute(
                            "SELECT DISTINCT currency_pocket FROM ledger_entries UNION SELECT currency FROM budget_revisions UNION SELECT currency FROM goal_details ORDER BY 1"
                        )]
                        currencies = currencies or [context.policy.currency]
                        pages = [endpoint(**{**kwargs, "currency": currency}) for currency in currencies]
                        native = pages[0]
                        keys = ("budgets",) if resource == "budgets" else ("plans", "recurring", "transfer_proposals")
                        for key in keys:
                            items: list[dict[str, Any]] = []
                            seen: set[str] = set()
                            for currency, page in zip(currencies, pages, strict=True):
                                for item in page[key]:
                                    identity = str(item.get("id") or item.get("plan") or (currency, item.get("merchant")))
                                    if identity not in seen:
                                        items.append({**item, "currency": item.get("currency", currency)})
                                        seen.add(identity)
                            native[key] = items
                    else:
                        native = endpoint(**kwargs)
                    concrete = resource
                    for name, argument in kwargs.items():
                        concrete = concrete.replace("{" + name + "}", str(argument))
                    result = annotate_workspace(native, context=context, resource=concrete)
                    build_reporting_context(connection, scope=context.scope)
                    return result
                except FinCLError as exc:
                    if str(exc).startswith("STALE_GENERATION"):
                        raise HTTPException(409, str(exc)) from exc
                    raise

        hints = get_type_hints(endpoint)
        signature = inspect.signature(endpoint)
        reported.__signature__ = signature.replace(  # type: ignore[attr-defined]
            parameters=[parameter.replace(annotation=hints.get(name, parameter.annotation))
                        for name, parameter in signature.parameters.items()],
            return_annotation=ReportedWorkspace,
        )
        return reported

    for route in list(app.routes):
        if not isinstance(route, APIRoute):
            continue
        resource = route.path.removeprefix("/api/v1/")
        is_read = route.methods == {"GET"} and any(resource == prefix or resource.startswith(prefix + "/") for prefix in READ_PREFIXES)
        retirement_preview = resource.startswith("rules/") and resource.endswith("/retire-combined/preview")
        if not is_read and not (route.methods == {"POST"} and (resource in READ_POSTS or retirement_preview)):
            continue
        app.add_api_route("/api/v1/reporting/workspaces/" + resource, adapt(route, resource),
                          methods=list(route.methods or []), response_model=ReportedWorkspace,
                          name="reported_workspace_" + route.name)
