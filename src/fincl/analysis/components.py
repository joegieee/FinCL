"""The one economic-component projection shared by the report and the read surface.

`build_components` turns each eligible ledger entry into signed exact *components*
— one for its amount, one more for a nonzero fee — and classifies each into the
same six-plus buckets the MVP economic-flow report uses. `report._economic_flow`
folds these into its per-currency totals; the dashboard and the
`/contributors/{handle}` route read the committed materialization of the same
list. Because both sides call this module, a displayed total and the rows behind
it cannot diverge.

Identity (v0.2 analysis §2):

* ``origin_component_id`` = SHA-256 over ``origin_kind``, origin anchor, ``role``
  and ``lens``. Stable across rebuild; never a SQL row number.
* ``component_id`` = SHA-256 over the generation token and the origin id. Bound
  to one projection generation, and what the API returns.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from ..intervals import DateInterval, covers_date, month_span
from ..money import ExactMoney, exact_sum, format_decimal

LENS_RECORDED_FLOW = "recorded_flow"

# Buckets whose amount component nets into `net_economic_flow`. Spending and fees
# net negatively (their coefficients are already signed negative); the inflow
# buckets net positively. `pending_review` and `other` stay visible but net zero,
# exactly as the MVP report treats them.
_POSITIVE_NET_BUCKETS = frozenset(
    {"income", "refunds", "reimbursements", "non_income_inflow"}
)

# One shared shaping of an eligible ledger entry: the economic-flow join plus the
# category / merchant / provenance columns the read surface needs. The eligibility
# predicate is identical to `report._economic_flow`'s original inline query so the
# refactor cannot change which rows are counted.
_ENTRY_SELECT = """
WITH entries AS (
    SELECT e.id AS entry_id, e.source_family,
           e.natural_anchor AS entry_anchor,
           e.currency_pocket AS currency,
           substr(e.effective_at, 1, 10) AS posted_date,
           e.effective_precision,
           e.amount_text, e.amount_coefficient, e.amount_scale,
           COALESCE(e.fee_text, '0') AS fee_text,
           COALESCE(e.fee_coefficient, 0) AS fee_coefficient,
           COALESCE(e.fee_scale, 0) AS fee_scale,
           e.economic_direction, e.event_kind,
           e.evidence_sha256,
           a.display_name AS account,
           COALESCE(ec.group_name || ' / ' || ec.category,
                    'Unclassified / Unclassified') AS category_key,
           COALESCE(mo.merchant_canonical, mo.merchant_normalized) AS merchant_key,
           COALESCE(mo.merchant_canonical, mo.merchant_normalized,
                    r.description_raw, d.description_raw,
                    'Unknown merchant') AS merchant_display,
           COALESCE('Page ' || d.page_number,
                    'Lines ' || r.physical_line_start || '–' || r.physical_line_end,
                    'Source record') AS source_location
    FROM ledger_entries e
    JOIN extraction_runs x ON x.id = e.extraction_run_id
    JOIN accounts a ON a.id = e.account_id
    LEFT JOIN statements s ON s.id = e.statement_id
    LEFT JOIN entry_categories ec ON ec.entry_id = e.id
    LEFT JOIN merchant_observations mo ON mo.entry_id = e.id
    LEFT JOIN raw_records r ON r.id = e.raw_record_id
    LEFT JOIN document_records d ON d.id = e.document_record_id
    WHERE e.analytics_eligible = 1 AND e.lifecycle_state = 'COMPLETED'
      AND x.selection_state = 'SELECTED'
      AND (e.source_family = 'CSV' OR s.selection_state = 'SELECTED')
      AND e.natural_anchor NOT IN (SELECT suppressed_anchor FROM duplicate_suppressions)
), linked AS (
    SELECT left_entry_id AS id FROM transfer_links WHERE status = 'CONFIRMED'
    UNION
    SELECT right_entry_id AS id FROM transfer_links WHERE status = 'CONFIRMED'
)
SELECT entries.*,
       CASE WHEN l.id IS NULL THEN 0 ELSE 1 END AS is_linked,
       i.classification AS inbound_classification,
       i.journal_seq AS inbound_seq,
       co.journal_seq AS override_seq
FROM entries
LEFT JOIN linked l ON l.id = entries.entry_id
LEFT JOIN inbound_classifications i
       ON i.bound = 1 AND i.anchor = entries.entry_anchor
LEFT JOIN category_overrides co
       ON co.bound = 1 AND co.entry_anchor = entries.entry_anchor
ORDER BY entries.currency, entries.posted_date, entries.source_family, entries.entry_id
"""


@dataclass(frozen=True, slots=True)
class Component:
    """One signed exact economic contribution of a single ledger entry."""

    origin_kind: Literal["ENTRY"]
    origin_anchor: str
    entry_id: str
    role: Literal["amount", "fee"]
    lens: str
    bucket: str
    coefficient: int
    scale: int
    currency: str
    represented_start: str
    represented_end: str
    precision: str
    period: str
    category_key: str
    merchant_key: str | None
    merchant_display: str
    account: str
    source_location: str
    evidence_sha256: str
    decision_seqs: tuple[int, ...]
    nets: bool

    @property
    def origin_component_id(self) -> str:
        payload = "\0".join(
            (self.origin_kind, self.origin_anchor, self.role, self.lens)
        )
        return "oc_" + hashlib.sha256(payload.encode()).hexdigest()

    def component_id(self, generation_token: str) -> str:
        payload = "\0".join((generation_token, self.origin_component_id))
        return "c_" + hashlib.sha256(payload.encode()).hexdigest()

    def as_exact_money(self) -> ExactMoney:
        return ExactMoney(self.coefficient, self.scale, format_decimal(self.coefficient, self.scale))


@dataclass(frozen=True, slots=True)
class CurrencyStats:
    """Per-currency entry counters the economic-flow report exposes verbatim."""

    entries: int = 0
    outside_covered_window: int = 0
    outside_analysis_window: int = 0
    excluded_confirmed_internal_entries: int = 0
    excluded_self_transfer_entries: int = 0


@dataclass(frozen=True, slots=True)
class ComponentProjection:
    lens: str
    components: list[Component]
    per_currency: dict[str, CurrencyStats]
    excluded_entry_count: int
    period_precision_excluded: int
    currencies: list[str] = field(default_factory=list)

    def netting(self) -> list[tuple[str, int, int, str]]:
        """`(currency, signed_coefficient, scale, YYYY-MM)` for every netting component."""
        return [
            (c.currency, c.coefficient, c.scale, c.period)
            for c in self.components
            if c.nets
        ]

    def bucket_total(self, currency: str, bucket: str) -> str:
        values = [
            c.as_exact_money()
            for c in self.components
            if c.currency == currency and c.bucket == bucket
        ]
        # Magnitudes: the report prints spending / fees / pending_review as
        # positive sums.
        magnitudes = [ExactMoney(abs(v.coefficient), v.scale, v.text) for v in values]
        coefficient, scale = exact_sum(*magnitudes)
        return format_decimal(coefficient, scale)


def _classify_amount(
    *, is_linked: bool, direction: str, event_kind: str, classification: str | None
) -> tuple[str, bool]:
    """Bucket and net-flag for an entry's amount component (application contract §4).

    This is the single definition previously split between
    ``report._classify_inbound`` and the ``queries._ENTRY_VIEW`` SQL CASE.
    """
    if is_linked:
        return "internal", False
    if direction == "OUT":
        return "spending", True
    if direction == "IN":
        if classification is None:
            return ("refunds", True) if event_kind == "REFUND" else ("pending_review", False)
        if classification == "income":
            return "income", True
        if classification == "reimbursement":
            return "reimbursements", True
        if classification in ("gift", "family_transfer"):
            return "non_income_inflow", True
        if classification == "self_transfer":
            return "internal", False
        if classification == "other":
            return "other", False
        return "pending_review", False
    return "internal", False  # ZERO direction contributes no amount


def build_components(
    connection: sqlite3.Connection,
    *,
    covered_intervals: list[DateInterval],
    analysis_intervals: list[DateInterval],
    lens: str = LENS_RECORDED_FLOW,
) -> ComponentProjection:
    """Project every eligible entry inside ``analysis_intervals`` into components.

    ``covered_intervals`` is only used for the ``outside_covered_window`` counter;
    membership and every emitted component honour ``analysis_intervals``, with the
    MVP rule that a MONTH-precision row contributes only when its whole month
    fits.
    """
    rows = connection.execute(_ENTRY_SELECT).fetchall()

    components: list[Component] = []
    stats: dict[str, dict[str, int]] = {}
    excluded_entry_count = 0
    period_precision_excluded = 0
    seen_currencies: list[str] = []

    for row in rows:
        currency = row["currency"]
        bucket_stats = stats.setdefault(
            currency,
            {
                "entries": 0,
                "outside_covered_window": 0,
                "outside_analysis_window": 0,
                "excluded_confirmed_internal_entries": 0,
                "excluded_self_transfer_entries": 0,
            },
        )
        if currency not in seen_currencies:
            seen_currencies.append(currency)
        bucket_stats["entries"] += 1
        posted_date = row["posted_date"]
        if not covers_date(covered_intervals, posted_date):
            bucket_stats["outside_covered_window"] += 1

        precision = row["effective_precision"]
        if precision == "MONTH":
            in_analysis = any(
                interval.contains_interval(month_span(posted_date[:7]))
                for interval in analysis_intervals
            )
            if not in_analysis and covers_date(analysis_intervals, posted_date):
                period_precision_excluded += 1
            represented = month_span(posted_date[:7])
        else:
            in_analysis = covers_date(analysis_intervals, posted_date)
            represented = DateInterval(posted_date, posted_date)

        if not in_analysis:
            bucket_stats["outside_analysis_window"] += 1
            excluded_entry_count += 1
            continue

        is_linked = bool(row["is_linked"])
        classification = row["inbound_classification"]
        amount_bucket, amount_nets = _classify_amount(
            is_linked=is_linked,
            direction=row["economic_direction"],
            event_kind=row["event_kind"],
            classification=classification,
        )
        if is_linked:
            bucket_stats["excluded_confirmed_internal_entries"] += 1
        elif row["economic_direction"] == "IN" and classification == "self_transfer":
            bucket_stats["excluded_self_transfer_entries"] += 1

        decision_seqs = tuple(
            int(seq)
            for seq in (row["inbound_seq"], row["override_seq"])
            if seq is not None
        )
        period = posted_date[:7]

        shared = {
            "origin_kind": "ENTRY",
            "origin_anchor": row["entry_anchor"],
            "entry_id": row["entry_id"],
            "lens": lens,
            "currency": currency,
            "represented_start": represented.start,
            "represented_end": represented.end,
            "precision": precision,
            "period": period,
            "category_key": row["category_key"],
            "merchant_key": row["merchant_key"],
            "merchant_display": row["merchant_display"],
            "account": row["account"],
            "source_location": row["source_location"],
            "evidence_sha256": row["evidence_sha256"],
            "decision_seqs": decision_seqs,
        }

        magnitude = abs(int(row["amount_coefficient"]))
        amount_scale = int(row["amount_scale"])
        if amount_bucket in _POSITIVE_NET_BUCKETS:
            amount_coefficient = magnitude
        elif amount_bucket == "spending":
            amount_coefficient = -magnitude
        else:  # pending_review / other are visible but net zero
            amount_coefficient = magnitude

        # A confirmed internal / self-transfer / ZERO-direction entry contributes
        # no amount component, exactly as the MVP report drops it.
        if amount_bucket != "internal":
            components.append(
                Component(
                    role="amount",
                    bucket=amount_bucket,
                    coefficient=amount_coefficient,
                    scale=amount_scale,
                    nets=amount_nets,
                    **shared,
                )
            )

        fee_coefficient = int(row["fee_coefficient"])
        if fee_coefficient:
            components.append(
                Component(
                    role="fee",
                    bucket="fees",
                    coefficient=-abs(fee_coefficient),
                    scale=int(row["fee_scale"]),
                    nets=True,
                    **shared,
                )
            )

    per_currency = {
        currency: CurrencyStats(**values) for currency, values in stats.items()
    }
    return ComponentProjection(
        lens=lens,
        components=components,
        per_currency=per_currency,
        excluded_entry_count=excluded_entry_count,
        period_precision_excluded=period_precision_excluded,
        currencies=seen_currencies,
    )
