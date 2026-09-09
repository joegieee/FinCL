"""Stage 1C (part 1) — the first baseline estimator and published metrics."""

from __future__ import annotations

import calendar
import functools
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from fincl.baseline import baseline_estimate
from fincl.document_service import DocumentImportService
from fincl.documents import parse_maribank_savings
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

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


def _revolut_ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "fincl.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


class EstimatorTests(unittest.TestCase):
    def setUp(self) -> None:
        run = _first_run(6)
        if run is None:
            self.skipTest("MariBank savings corpus unavailable")
        self.estimate = baseline_estimate(
            _mari_ledger([path for _, path in run]), today=_today_after(run)
        )
        self.sgd = self.estimate["by_currency"]["SGD"]

    def test_the_monthly_partition_reconciles_to_owner_cost(self) -> None:
        self.assertTrue(self.estimate["available"])
        self.assertEqual(self.estimate["sample"]["month_count"], 6)
        self.assertEqual(self.estimate["sample"]["method_version"], "baseline-estimator-v1")
        for row in self.sgd["months"]:
            self.assertTrue(row["reconciles"], row["month"])

    def test_variable_residual_is_the_arithmetic_mean_keeping_zero_months(self) -> None:
        residuals = [Decimal(row["routine_variable_residual"]) for row in self.sgd["months"]]
        self.assertEqual(len(residuals), 6)
        expected = (sum(residuals) / Decimal(6)).quantize(Decimal("0.01"))
        self.assertEqual(Decimal(self.sgd["variable_residual_monthly"]), expected)
        # median / min / max are exposed as comparison statistics only.
        self.assertIn("variable_residual_median", self.sgd)
        self.assertLessEqual(
            Decimal(self.sgd["variable_residual_min"]),
            Decimal(self.sgd["variable_residual_max"]),
        )

    def test_reserve_and_income_ratio_are_withheld_with_reasons(self) -> None:
        self.assertFalse(self.sgd["reserve"]["available"])
        self.assertIn("essential", self.sgd["reserve"]["reason"])
        # MariBank savings statements evidence no classified income stream.
        self.assertFalse(self.sgd["savings_rate"]["available"])
        self.assertIn("no classified income", self.sgd["savings_rate"]["reason"])

    def test_limitations_are_declared(self) -> None:
        self.assertTrue(
            any("unmapped categories" in item for item in self.estimate["limitations"])
        )

    def test_role_totals_reconcile_and_unresolved_coverage_withholds_reserve(self) -> None:
        roles = self.sgd["roles"]
        assert roles is not None
        self.assertTrue(roles["totals"]["reconciles"])
        self.assertTrue(all(row["reconciles"] for row in roles["months"]))
        self.assertFalse(self.sgd["reserve"]["available"])
        self.assertIn("essential role coverage", self.sgd["reserve"]["reason"])


class OutlierHoldoutTests(unittest.TestCase):
    def _rows(self) -> list[list[str]]:
        rows: list[list[str]] = [
            ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
             "Seed", "5000.00", "0.00", "SGD", "COMPLETED", "5000.00"],
        ]
        balance = Decimal("5000.00")
        for month in ("01", "02", "03"):
            for day, amount in (("05", "20.00"), ("12", "20.00"), ("19", "20.00")):
                balance -= Decimal(amount)
                rows.append([
                    "Card Payment", "Current", f"2026-{month}-{day} 10:00:00",
                    f"2026-{month}-{day} 10:00:01", "Corner Shop", f"-{amount}",
                    "0.00", "SGD", "COMPLETED", f"{balance}",
                ])
        balance -= Decimal("900.00")
        rows.append([
            "Card Payment", "Current", "2026-03-31 10:00:00", "2026-03-31 10:00:01",
            "Annual Thing", "-900.00", "0.00", "SGD", "COMPLETED", f"{balance}",
        ])
        return rows

    def test_a_large_single_entry_is_held_out_of_the_routine_residual(self) -> None:
        estimate = baseline_estimate(_revolut_ledger(self._rows()), today=date(2026, 4, 10))
        sgd = estimate["by_currency"]["SGD"]

        flagged = sgd["irregular_risk_entries"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["amount"], "900.00")
        self.assertEqual(flagged[0]["month"], "2026-03")

        march = next(row for row in sgd["months"] if row["month"] == "2026-03")
        self.assertEqual(march["unresolved_irregular"], "900.00")
        # The $900 left the routine residual but the month still reconciles.
        self.assertTrue(march["reconciles"])
        self.assertEqual(march["routine_variable_residual"], "60.00")
        # ...and it is still present in the historical owner-cost total.
        self.assertEqual(march["owner_cost_recorded"], "960.00")
        self.assertTrue(sgd["roles"]["totals"]["reconciles"])
        self.assertFalse(sgd["reserve"]["available"])
        self.assertTrue(
            any(group["cost_pattern"] == "irregular" for group in sgd["role_groups"])
        )


if __name__ == "__main__":
    unittest.main()
