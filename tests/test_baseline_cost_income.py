"""Stage 1B (part 1) — the per-month cost and income evidence table."""

from __future__ import annotations

import calendar
import functools
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fincl.baseline import cost_income_table
from fincl.document_service import DocumentImportService
from fincl.documents import parse_maribank_savings
from fincl.money import ExactMoney, compare, exact_sum, format_decimal
from fincl.refresh import refresh_ledger

_REPO = Path(__file__).resolve().parent.parent
_RAW = _REPO / "raw"
_MARI_GLOB = "*_MariBank_e-Statement.pdf"


def _month_index(day: date) -> int:
    return day.year * 12 + day.month - 1


def _month_key(index: int) -> str:
    return f"{index // 12}-{index % 12 + 1:02d}"


@functools.cache
def _mari_corpus() -> tuple[tuple[int, Path], ...]:
    """Local whole-month savings statements as (month index, path), by parsed period."""
    found: dict[int, Path] = {}
    for path in _RAW.glob(_MARI_GLOB):
        document = parse_maribank_savings(path.read_bytes())
        if document.period_start is None or document.period_end is None:
            continue
        start = date.fromisoformat(document.period_start)
        end = date.fromisoformat(document.period_end)
        # Only statements spanning a whole calendar month establish that month.
        if start.day == 1 and end.day == calendar.monthrange(end.year, end.month)[1]:
            found[_month_index(start)] = path
    return tuple(sorted(found.items()))


def _first_run(count: int) -> list[tuple[int, Path]] | None:
    """The earliest ``count`` calendar-contiguous statements, or None if absent."""
    corpus = _mari_corpus()
    for start in range(len(corpus) - count + 1):
        window = list(corpus[start : start + count])
        if window[-1][0] - window[0][0] == count - 1:
            return window
    return None


def _today_after(run: list[tuple[int, Path]]) -> date:
    """A mid-month date in the month after the run's last statement month."""
    following = run[-1][0] + 1
    return date(following // 12, following % 12 + 1, 15)


def _mari_ledger(paths: list[Path]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "fincl.sqlite3"
    service = DocumentImportService.plaintext_phase2(database, root / "evidence")
    for path in paths:
        assert service.import_maribank(path).status == "ACCEPTED"
    refresh_ledger(database)
    return database


def _sum(values: list[str]) -> str:
    terms = [ExactMoney.parse(v) for v in values]
    return format_decimal(*exact_sum(*terms)) if terms else "0"


class CostIncomeTableTests(unittest.TestCase):
    def setUp(self) -> None:
        run = _first_run(6)
        if run is None:
            self.skipTest("MariBank savings corpus unavailable")
        self.months = [_month_key(index) for index, _ in run]
        self.database = _mari_ledger([path for _, path in run])
        self.table = cost_income_table(self.database, today=_today_after(run))

    def test_the_table_covers_every_month_of_the_normal_window(self) -> None:
        self.assertTrue(self.table["available"])
        self.assertEqual(self.table["baseline_window"]["quality"], "normal")
        self.assertEqual(
            [row["month"] for row in self.table["months"]],
            self.months,
        )
        self.assertIn("SGD", self.table["currencies"])

    def test_owner_cost_views_agree_when_there_are_no_refund_allocations(self) -> None:
        for row in self.table["months"]:
            cell = row["by_currency"]["SGD"]
            self.assertEqual(
                cell["owner_cost_recorded"], cell["owner_cost_baseline"], row["month"]
            )
            self.assertEqual(cell["unmatched_receipts"], "0", row["month"])

        recon = self.table["reconciliation"]["SGD"]
        self.assertTrue(recon["balances"])
        self.assertEqual(
            recon["owner_cost_recorded_total"], recon["owner_cost_baseline_total"]
        )

    def test_reconciliation_total_matches_the_monthly_rows(self) -> None:
        recon = self.table["reconciliation"]["SGD"]
        monthly = _sum(
            [row["by_currency"]["SGD"]["owner_cost_recorded"] for row in self.table["months"]]
        )
        self.assertEqual(monthly, recon["owner_cost_recorded_total"])

        gross = _sum([row["by_currency"]["SGD"]["gross_cost"] for row in self.table["months"]])
        refunds = _sum(
            [row["by_currency"]["SGD"]["refunds_received"] for row in self.table["months"]]
        )
        reimb = _sum(
            [row["by_currency"]["SGD"]["reimbursements_received"] for row in self.table["months"]]
        )
        terms = [ExactMoney.parse(gross)]
        for term in (refunds, reimb):
            parsed = ExactMoney.parse(term)
            terms.append(ExactMoney(-parsed.coefficient, parsed.scale, ""))
        coefficient, scale = exact_sum(*terms)
        self.assertEqual(
            compare(
                ExactMoney(coefficient, scale, ""),
                ExactMoney.parse(recon["owner_cost_recorded_total"]),
            ),
            0,
        )

    def test_gross_cost_is_non_negative_and_present(self) -> None:
        grosses = [row["by_currency"]["SGD"]["gross_cost"] for row in self.table["months"]]
        for value in grosses:
            self.assertGreaterEqual(compare(ExactMoney.parse(value), ExactMoney(0, 0, "")), 0)
        self.assertTrue(
            any(compare(ExactMoney.parse(v), ExactMoney(0, 0, "")) > 0 for v in grosses),
            "expected some spending across six months of statements",
        )

    def test_income_streams_are_labelled_likely_never_confirmed(self) -> None:
        for stream in self.table["income_streams"]:
            self.assertEqual(stream["certainty"], "likely")
            self.assertGreaterEqual(stream["occurrence_count"], 1)
            self.assertLessEqual(
                compare(
                    ExactMoney.parse(stream["min_amount"]),
                    ExactMoney.parse(stream["max_amount"]),
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
