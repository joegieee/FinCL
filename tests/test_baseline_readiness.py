"""Stage 1A — the account/pocket-by-month evidence readiness view.

A real ledger is built through the import path (MariBank monthly e-statements
for the ``established`` cases, a Revolut CSV for the observed-only case) so the
completeness basis, the recommended baseline window and the missing-evidence
tasks are all asserted against genuine parsed periods.
"""

from __future__ import annotations

import calendar
import functools
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fincl.api import create_app
from fincl.baseline import baseline_readiness
from fincl.document_service import DocumentImportService
from fincl.documents import parse_maribank_savings
from fincl.refresh import refresh_ledger
from tests.asgi_client import ASGITestClient
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
        result = service.import_maribank(path)
        assert result.status == "ACCEPTED", (path.name, result.status)
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


_REVOLUT_ROWS = [
    ["Topup", "Current", "2026-01-04 10:00:00", "2026-01-04 10:00:01",
     "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Rent", "-500.00", "0.00", "SGD", "COMPLETED", "360.00"],
]


class EstablishedMonthTests(unittest.TestCase):
    def _run(self, count: int) -> list[tuple[int, Path]]:
        run = _first_run(count)
        if run is None:
            self.skipTest("MariBank savings corpus unavailable")
        return run

    def test_six_consecutive_statement_months_are_a_normal_baseline(self) -> None:
        run = self._run(6)
        months = [_month_key(index) for index, _ in run]
        database = _mari_ledger([path for _, path in run])

        page = baseline_readiness(database, today=_today_after(run))

        self.assertEqual(len(page["pockets"]), 1)
        pocket = page["pockets"][0]
        self.assertEqual(pocket["currency_pocket"], "SGD")
        self.assertEqual([cell["month"] for cell in pocket["months"]], months)
        self.assertEqual(
            {cell["completeness"] for cell in pocket["months"]}, {"established"}
        )

        window = page["baseline_window"]
        self.assertEqual(window["quality"], "normal")
        self.assertEqual(window["basis"], "all-established")
        self.assertEqual(window["month_count"], 6)
        self.assertEqual((window["start_month"], window["end_month"]), (months[0], months[-1]))

        self.assertEqual(
            [t for t in page["tasks"] if t["kind"] in ("import-missing", "confirm-export-period")],
            [],
        )

    def test_a_skipped_month_is_missing_not_a_zero_and_raises_a_task(self) -> None:
        run = self._run(3)
        first, middle, last = (_month_key(index) for index, _ in run)
        year, month = (int(part) for part in middle.split("-"))
        last_day = calendar.monthrange(year, month)[1]
        database = _mari_ledger([run[0][1], run[2][1]])

        page = baseline_readiness(database, today=_today_after(run))
        cells = {cell["month"]: cell for cell in page["pockets"][0]["months"]}

        self.assertEqual(cells[first]["completeness"], "established")
        self.assertEqual(cells[middle]["completeness"], "missing")
        self.assertEqual(cells[last]["completeness"], "established")

        missing = [t for t in page["tasks"] if t["kind"] == "import-missing"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(
            missing[0]["interval"],
            {"start": f"{middle}-01", "end": f"{middle}-{last_day:02d}"},
        )

        # Two established months with a gap between them is not a 3-month run.
        self.assertEqual(page["baseline_window"]["quality"], "limited-history")
        self.assertEqual(page["baseline_window"]["month_count"], 1)

    def test_prefers_the_latest_six_of_a_longer_established_run(self) -> None:
        run = self._run(6)
        months = [_month_key(index) for index, _ in run]
        # Import order is deliberately shuffled; the run detection is by month.
        shuffled = [run[position][1] for position in (2, 0, 1, 5, 3, 4)]
        database = _mari_ledger(shuffled)

        window = baseline_readiness(database, today=_today_after(run))["baseline_window"]

        self.assertEqual(window["quality"], "normal")
        self.assertEqual((window["start_month"], window["end_month"]), (months[0], months[-1]))


class ObservedOnlyTests(unittest.TestCase):
    def test_a_revolut_pdf_pocket_establishes_the_months_its_period_covers(self) -> None:
        database = _revolut_ledger(_REVOLUT_ROWS)

        page = baseline_readiness(database, today=date(2026, 4, 1))
        pocket = page["pockets"][0]

        # The statement period runs 2026-01-04..2026-03-05, so January is a
        # fully-covered (established) month while March is still provisional.
        self.assertEqual(
            {cell["completeness"] for cell in pocket["months"]},
            {"established", "provisional"},
        )
        self.assertNotIn(
            "no independently proven statement period",
            " ".join(pocket["warnings"]),
        )

        window = page["baseline_window"]
        self.assertEqual(window["quality"], "limited-history")
        self.assertEqual(window["basis"], "all-established")

        self.assertEqual(
            [t for t in page["tasks"] if t["kind"] == "import-missing"], []
        )

    def test_zero_complete_months_never_fabricate_a_baseline(self) -> None:
        # A single one-row CSV: one observed month, nothing established.
        database = _revolut_ledger([_REVOLUT_ROWS[0]])

        window = baseline_readiness(database, today=date(2026, 2, 1))["baseline_window"]

        self.assertIn(window["quality"], {"limited-history", "none"})
        self.assertNotEqual(window["quality"], "normal")


class ReadinessApiTests(unittest.TestCase):
    def test_endpoint_returns_the_readiness_page(self) -> None:
        database = _revolut_ledger(_REVOLUT_ROWS)
        root = database.parent
        app = create_app(
            database, port=8765, bootstrap_secret="test-secret",
            evidence_dir=root / "evidence",
        )
        client = ASGITestClient(app, base_url="http://127.0.0.1:8765")
        self.addCleanup(client.close)
        client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": "http://127.0.0.1:8765", "content-type": "application/json"},
        )

        response = client.get("/api/v1/baseline/readiness")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("baseline_window", body)
        self.assertIn("pockets", body)
        self.assertEqual(body["scope"]["currencies"], ["SGD"])


if __name__ == "__main__":
    unittest.main()
