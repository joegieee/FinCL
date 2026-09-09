"""Stage 3C -- minimal temporal-holdout validation.

The harness deliberately answers a small question: given at least three complete
training months, how did the Stage 1 simple mean estimator compare with carrying
the last observed month forward into the immediately following month?

Training is selected by occurrence date *and* by when source evidence and
review/policy versions were available.  Callers that cannot reconstruct source
availability must say so; their result is labelled a retrospective corrected-data
evaluation and is never described as an as-issued forecast.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..decision_journal import default_journal_path, read_records
from ..generation import current_generation

VALIDATION_VERSION = "temporal-holdout-v1"
MINIMUM_TRAINING_MONTHS = 3
STATUS_BACKTEST = "backtest"
STATUS_NO_BACKTEST = "no-backtest"
EVALUATION_RECONSTRUCTION = "historical-cutoff reconstruction"
EVALUATION_RETROSPECTIVE = "retrospective corrected-data evaluation"


@dataclass(frozen=True, slots=True)
class MonthlyObservation:
    """One source observation contributing to a calendar month's actuals."""

    observation_id: str
    occurred_on: str
    currency: str
    spending: str = "0"
    income: str = "0"
    available_on: str | None = None
    review_policy_version_id: str | None = None

    def __post_init__(self) -> None:
        date.fromisoformat(self.occurred_on)
        if self.available_on is not None:
            date.fromisoformat(self.available_on[:10])
        if Decimal(self.spending) < 0 or Decimal(self.income) < 0:
            raise ValueError("validation observations carry non-negative magnitudes")


@dataclass(frozen=True, slots=True)
class ReviewPolicyVersion:
    """A review decision or policy implementation available at a point in time."""

    version_id: str
    decided_at: str
    kind: str

    def __post_init__(self) -> None:
        _timestamp_date(self.decided_at)


@dataclass(frozen=True, slots=True)
class CashBridge:
    """A reconciled actual cash path for the held-out month."""

    currency: str
    opening_balance: str
    lowest_actual_balance: str
    reconciled: bool

    def __post_init__(self) -> None:
        Decimal(self.opening_balance)
        Decimal(self.lowest_actual_balance)


def _timestamp_date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _month_end(month: str) -> date:
    year, index = (int(part) for part in month.split("-"))
    return date(year, index, calendar.monthrange(year, index)[1])


def _previous_month(month: str) -> str:
    year, index = (int(part) for part in month.split("-"))
    if index == 1:
        year, index = year - 1, 12
    else:
        index -= 1
    return f"{year:04d}-{index:02d}"


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _no_backtest(*, reason: str, heldout_month: str | None = None) -> dict[str, Any]:
    return {
        "contract_version": VALIDATION_VERSION,
        "status": STATUS_NO_BACKTEST,
        "reason": reason,
        "heldout_month": heldout_month,
        "training": None,
        "evaluation_classification": None,
        "as_issued": False,
        "comparisons": [],
        "observed_failures": [],
        "accuracy_score": None,
        "release_condition": False,
    }


def temporal_holdout_validation(
    observations: list[MonthlyObservation],
    *,
    review_policy_versions: list[ReviewPolicyVersion] | None = None,
    heldout_month: str | None = None,
    cash_bridges: list[CashBridge] | None = None,
) -> dict[str, Any]:
    """Compare the simple mean with prior-month carry-forward.

    The held-out month is the latest observed month unless explicitly selected.
    Only consecutive earlier months are admitted, and at least three are required.
    A source without ``available_on`` can still support a transparent retrospective
    evaluation, but not a historical-cutoff or as-issued claim.
    """
    if not observations:
        return _no_backtest(reason="no monthly observations are available")
    heldout = heldout_month or max(item.occurred_on[:7] for item in observations)
    cutoff = _month_end(_previous_month(heldout))
    heldout_rows = [item for item in observations if item.occurred_on[:7] == heldout]
    if not heldout_rows:
        return _no_backtest(
            reason="the selected held-out month has no actual observations",
            heldout_month=heldout,
        )

    source_availability_known = all(item.available_on is not None for item in observations)
    versions_known = review_policy_versions is not None
    versions = review_policy_versions or []
    eligible_versions = [v for v in versions if _timestamp_date(v.decided_at) <= cutoff]
    excluded_versions = [v for v in versions if _timestamp_date(v.decided_at) > cutoff]
    eligible_version_ids = {version.version_id for version in eligible_versions}
    eligible_training = [
        item
        for item in observations
        if item.occurred_on[:7] < heldout
        and (
            item.available_on is None
            or date.fromisoformat(item.available_on[:10]) <= cutoff
        )
        and (
            item.review_policy_version_id is None
            or item.review_policy_version_id in eligible_version_ids
        )
    ]
    months_present = {item.occurred_on[:7] for item in eligible_training}
    training_months: list[str] = []
    cursor = _previous_month(heldout)
    while cursor in months_present:
        training_months.append(cursor)
        cursor = _previous_month(cursor)
    training_months.reverse()
    if len(training_months) < MINIMUM_TRAINING_MONTHS:
        return _no_backtest(
            reason=(
                f"only {len(training_months)} consecutive pre-cutoff month(s) are "
                f"available; {MINIMUM_TRAINING_MONTHS} are required"
            ),
            heldout_month=heldout,
        )

    if source_availability_known and versions_known:
        classification = EVALUATION_RECONSTRUCTION
    else:
        classification = EVALUATION_RETROSPECTIVE

    training_set = [
        item for item in eligible_training if item.occurred_on[:7] in training_months
    ]
    currencies = sorted({item.currency for item in training_set} | {item.currency for item in heldout_rows})
    bridges = {bridge.currency: bridge for bridge in cash_bridges or []}
    comparisons: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for currency in currencies:
        monthly: dict[str, dict[str, Decimal]] = {
            month: {"spending": Decimal(0), "income": Decimal(0)}
            for month in training_months
        }
        actual = {"spending": Decimal(0), "income": Decimal(0)}
        for item in training_set:
            if item.currency == currency:
                monthly[item.occurred_on[:7]]["spending"] += Decimal(item.spending)
                monthly[item.occurred_on[:7]]["income"] += Decimal(item.income)
        for item in heldout_rows:
            if item.currency == currency:
                actual["spending"] += Decimal(item.spending)
                actual["income"] += Decimal(item.income)

        simple = {
            field: _mean([monthly[month][field] for month in training_months])
            for field in ("spending", "income")
        }
        prior = dict(monthly[training_months[-1]])
        estimator_rows: dict[str, dict[str, Any]] = {}
        for name, estimate in (("simple_mean", simple), ("prior_month_carry_forward", prior)):
            errors = {
                field: abs(estimate[field] - actual[field])
                for field in ("spending", "income")
            }
            bridge = bridges.get(currency)
            missed: dict[str, Any] | None = None
            if bridge is not None and bridge.reconciled:
                projected_close = (
                    Decimal(bridge.opening_balance) + estimate["income"] - estimate["spending"]
                )
                actual_low = Decimal(bridge.lowest_actual_balance)
                if projected_close >= 0 and actual_low < 0:
                    missed = {
                        "missed": True,
                        "projected_month_end_balance": _money(projected_close),
                        "actual_lowest_balance": _money(actual_low),
                    }
                    failures.append(
                        {
                            "kind": "missed_cash_shortfall",
                            "currency": currency,
                            "estimator": name,
                            **missed,
                        }
                    )
            estimator_rows[name] = {
                "estimate": {field: _money(value) for field, value in estimate.items()},
                "absolute_error": {field: _money(value) for field, value in errors.items()},
                "missed_cash_shortfall": missed,
            }
            for field, error in errors.items():
                if error:
                    failures.append(
                        {
                            "kind": "absolute_error",
                            "currency": currency,
                            "estimator": name,
                            "measure": field,
                            "amount": _money(error),
                        }
                    )
        comparisons.append(
            {
                "currency": currency,
                "actual": {field: _money(value) for field, value in actual.items()},
                "estimators": estimator_rows,
                "cash_bridge": (
                    {"reliable": True, "reconciled": True}
                    if currency in bridges and bridges[currency].reconciled
                    else {
                        "reliable": False,
                        "reconciled": bridges[currency].reconciled if currency in bridges else None,
                        "reason": "no reconciled cash bridge is available",
                    }
                ),
            }
        )

    return {
        "contract_version": VALIDATION_VERSION,
        "status": STATUS_BACKTEST,
        "reason": None,
        "heldout_month": heldout,
        "training": {
            "cutoff": cutoff.isoformat(),
            "months": training_months,
            "observation_ids": sorted(item.observation_id for item in training_set),
            "eligible_review_policy_versions": [v.version_id for v in eligible_versions],
            "excluded_later_review_policy_versions": [v.version_id for v in excluded_versions],
        },
        "evaluation_classification": classification,
        "as_issued": False,
        "comparisons": comparisons,
        "observed_failures": failures,
        "accuracy_score": None,
        "release_condition": False,
        "notes": [
            "the held-out month never enters either estimator's training inputs",
            "this reconstructed estimate is not an as-issued forecast",
            "absolute error is reported directly; no aggregate accuracy score is fabricated",
            "backtest improvement is descriptive and is not a release condition",
        ],
    }


def forecast_validation(database: Path) -> dict[str, Any]:
    """Run the harness over the current projection, explicitly as corrected data.

    The current disposable projection contains the latest review interpretation;
    it cannot reconstruct superseded row classifications.  Transaction months are
    still held out correctly and later journal versions are excluded from the
    eligible-version inventory, but the result is labelled retrospective.
    """
    connection = connect_readonly(database)
    try:
        generation = current_generation(connection)
        if generation is None:
            return _no_backtest(reason="the ledger has no completed generation")
        token = str(generation["generation_token"])
        rows = connection.execute(
            """
            SELECT component_id, period, currency, bucket, signed_coefficient, scale
            FROM analysis_components
            WHERE generation_token = ? AND lens = 'recorded_flow'
              AND bucket IN ('spending', 'fees', 'income')
            ORDER BY represented_start, component_id
            """,
            (token,),
        ).fetchall()
    finally:
        connection.close()

    observations = [
        MonthlyObservation(
            observation_id=str(row["component_id"]),
            occurred_on=f"{row['period']}-01",
            currency=str(row["currency"]),
            spending=(
                str(abs(Decimal(row["signed_coefficient"]).scaleb(-int(row["scale"]))))
                if row["bucket"] in ("spending", "fees")
                else "0"
            ),
            income=(
                str(abs(Decimal(row["signed_coefficient"]).scaleb(-int(row["scale"]))))
                if row["bucket"] == "income"
                else "0"
            ),
            available_on=None,
            review_policy_version_id=None,
        )
        for row in rows
    ]
    versions = [
        ReviewPolicyVersion(
            version_id=f"journal:{record.seq}:{record.record_hash}",
            decided_at=record.decided_at,
            kind=record.decision_kind,
        )
        for record in read_records(default_journal_path(database))
    ]
    result = temporal_holdout_validation(
        observations,
        review_policy_versions=versions,
    )
    result["generation"] = token
    result["projection_basis"] = json.loads(str(generation["manifest_json"]))
    return result


__all__ = [
    "CashBridge",
    "EVALUATION_RECONSTRUCTION",
    "EVALUATION_RETROSPECTIVE",
    "MINIMUM_TRAINING_MONTHS",
    "MonthlyObservation",
    "ReviewPolicyVersion",
    "STATUS_BACKTEST",
    "STATUS_NO_BACKTEST",
    "forecast_validation",
    "temporal_holdout_validation",
]
