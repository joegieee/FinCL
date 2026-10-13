"""v0.7 saved report definitions (releases/v0.5-v2-product-completion.md §3)."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fincl.api import create_app
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from fincl.refresh import refresh_ledger
from tests.asgi_client import ASGITestClient
from tests.revolut_pdf import revolut_pdf_bytes
from tests.test_trips import _SGD, _USD

_ORIGIN = "http://127.0.0.1:8765"


def _definition(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Spending by category",
        "scope": {"window": {"kind": "all"}, "currencies": [], "accounts": [], "categories": [],
                  "trips": [], "countries": []},
        "lens": "recorded_flow", "group_by": ["category"], "columns": ["gross_cost", "refunds", "net_cost"],
        "reason": "Synthetic report",
    }
    body.update(over)
    return body


def _scope(**over: Any) -> dict[str, Any]:
    scope: dict[str, Any] = _definition()["scope"]
    scope.update(over)
    return scope


class _ReportLedger(unittest.TestCase):
    """A small synthetic ledger and command helpers; no tests of its own."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "ledger.sqlite3"
        service = DocumentImportService.plaintext_phase2(self.database, root / "evidence")
        for name, rows in (("sgd.pdf", _SGD), ("usd.pdf", _USD)):
            source = root / name
            source.write_bytes(revolut_pdf_bytes(rows))
            service.import_revolut_pdf(source)
        refresh_ledger(self.database)
        app = create_app(self.database, port=8765, bootstrap_secret="test-secret", require_access_key=True,
                         evidence_dir=root / "evidence")
        self.client = ASGITestClient(app, base_url=_ORIGIN)
        self.addCleanup(self.client.close)
        response = self.client.post("/session", json={"secret": "test-secret"},
                                    headers={"origin": _ORIGIN, "content-type": "application/json"})
        self.csrf = response.json()["csrf"]

    def _headers(self) -> dict[str, str]:
        return {"origin": _ORIGIN, "content-type": "application/json", "x-csrf-token": self.csrf}

    def generation(self) -> str:
        return str(self.client.get("/api/v1/trips").json()["generation"])

    def command(self, kind: str, anchors: dict[str, str], payload: dict[str, Any], *, status: int = 200) -> Any:
        response = self.client.post("/api/v1/commands", json={
            "request_id": str(uuid.uuid4()), "expected_generation": self.generation(),
            "kind": kind, "anchors": anchors, "payload": payload,
        }, headers=self._headers())
        self.assertEqual(response.status_code, status, response.text)
        return response

    def save(self, **over: Any) -> str:
        report = str(uuid.uuid4())
        self.command("REPORT_DEFINITION_SET", {"report": report}, _definition(**over))
        return report

    def result(self, report: str) -> dict[str, Any]:
        response = self.client.get(f"/api/v1/reports/{report}/result")
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json())

    def anchor(self, description: str) -> str:
        connection = sqlite3.connect(self.database)
        try:
            return str(connection.execute(
                "SELECT e.natural_anchor FROM ledger_entries e JOIN document_records d ON d.id=e.document_record_id "
                "WHERE d.description_raw=? AND e.economic_direction='OUT'", (description,)).fetchone()[0])
        finally:
            connection.close()


class ReportTests(_ReportLedger):
    def test_a_saved_report_is_listed_and_evaluated_against_the_current_data(self) -> None:
        report = self.save()
        [listed] = self.client.get("/api/v1/reports").json()["reports"]
        self.assertEqual((listed["report_id"], listed["name"]), (report, "Spending by category"))

        result = self.result(report)
        sgd = result["totals_by_currency"]["SGD"]
        # Same recorded-flow rows as Trips/Analysis: 400 + 30 + 200 + 15 + 5 spent, 50 refunded.
        self.assertEqual((sgd["gross_cost"], sgd["refunds"], sgd["net_cost"]), ("650.00", "50.00", "600.00"))
        self.assertEqual(result["totals_by_currency"]["USD"]["gross_cost"], "20.00")
        self.assertEqual(result["generation"], self.generation())
        self.assertTrue(result["evaluated_on"])
        # Rows reconcile to the totals, per currency; unlike currencies are never added.
        for currency, total in result["totals_by_currency"].items():
            rows = [row for row in result["rows"] if row["currency"] == currency]
            self.assertAlmostEqual(sum(float(row["gross_cost"]) for row in rows), float(total["gross_cost"]))

    def test_a_fixed_window_keeps_only_transactions_inside_it(self) -> None:
        report = self.save(scope=_scope(window={"kind": "fixed", "start": "2026-01-01", "end": "2026-01-31"}),
                           group_by=["month"])
        result = self.result(report)
        self.assertEqual(result["totals_by_currency"]["SGD"]["gross_cost"], "250.00")
        self.assertEqual({row["group"][0]["value"] for row in result["rows"]}, {"2026-01"})

    def test_a_removed_trip_warns_and_never_broadens_the_report(self) -> None:
        trip = str(uuid.uuid4())
        self.command("TRIP_SET", {"trip": trip}, {"name": "Trip A", "start_on": "2026-01-10", "end_on": "2026-01-15",
                                                  "countries": ["FR"], "reason": "Synthetic trip"})
        self.command("ENTRY_COUNTRY_SET", {"entry": self.anchor("Meal")}, {"country": "FR", "reason": "Receipt"})
        report = self.save(scope=_scope(trips=[trip]), group_by=["trip"])
        result = self.result(report)
        self.assertEqual(result["totals_by_currency"]["SGD"]["gross_cost"], "30.00")
        self.assertEqual(result["rows"][0]["group"][0]["label"], "Trip A")

        self.command("TRIP_RETIRE", {"trip": trip}, {"reason": "Cancelled"})
        result = self.result(report)
        self.assertEqual(result["rows"], [])
        self.assertIn("removed", result["warnings"][0])

    def test_a_category_that_no_longer_exists_warns_and_matches_nothing(self) -> None:
        report = self.save(scope=_scope(categories=["Nowhere / Gone"]))
        result = self.result(report)
        self.assertEqual(result["rows"], [])
        self.assertIn("Nowhere / Gone", result["warnings"][0])

    def test_definitions_are_closed_and_take_no_free_text_search(self) -> None:
        bad = [
            {**_definition(), "search": "hotel"},
            _definition(scope={**_scope(), "search": "hotel"}),
            _definition(group_by=["payee"]),
            _definition(group_by=["category", "month", "account"]),
            _definition(columns=[]),
            _definition(lens="attributed"),
            _definition(scope=_scope(window={"kind": "recent_months", "months": 0})),
            _definition(scope=_scope(window={"kind": "fixed", "start": "2026-02-01", "end": "2026-01-01"})),
            _definition(scope=_scope(countries=["ZZ"])),
            _definition(name=""),
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                self.command("REPORT_DEFINITION_SET", {"report": str(uuid.uuid4())}, payload, status=422)

    def test_an_unsaved_definition_can_be_previewed(self) -> None:
        body = {key: value for key, value in _definition(group_by=["currency"]).items() if key != "reason"}
        response = self.client.post("/api/v1/reports/preview", json=body, headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({row["currency"] for row in response.json()["rows"]}, {"SGD", "USD"})

    def test_retire_removes_and_replay_rebuilds_the_rest(self) -> None:
        kept = self.save(name="Kept")
        gone = self.save(name="Gone")
        self.command("REPORT_DEFINITION_RETIRE", {"report": gone}, {"reason": "Not needed"})
        self.command("REPORT_DEFINITION_RETIRE", {"report": gone}, {"reason": "Again"}, status=422)

        connection = sqlite3.connect(self.database)
        try:
            replay_journal(connection)
            connection.commit()
            ids = [row[0] for row in connection.execute("SELECT report_id FROM report_definitions")]
        finally:
            connection.close()
        self.assertEqual(ids, [kept])
        self.assertEqual(self.client.get(f"/api/v1/reports/{gone}/result").status_code, 404)

    def test_the_reported_mirror_values_every_row(self) -> None:
        report = self.save()
        response = self.client.get(f"/api/v1/reporting/workspaces/reports/{report}/result")
        self.assertEqual(response.status_code, 200, response.text)
        paths = {tuple(amount["path"]) for amount in response.json()["amounts"]}
        self.assertIn(("totals_by_currency", "SGD", "gross_cost"), paths)


class ReportExportTests(_ReportLedger):
    def export(self, *, status: int = 200, **body: Any) -> Any:
        response = self.client.post("/api/v1/reports/export", json=body, headers=self._headers())
        self.assertEqual(response.status_code, status, response.text)
        return response

    def test_json_is_lossless_and_carries_its_manifest(self) -> None:
        report = self.save(name="Spending / by category", columns=["gross_cost", "entry_count"])
        response = self.export(format="json", report_id=report)
        self.assertRegex(response.headers["content-disposition"], r'attachment; filename="spending-by-category-\d{4}-\d{2}-\d{2}\.json"')
        data = json.loads(response.content)
        manifest = data["manifest"]
        for key in ("schema_version", "api_version", "app_version", "generation", "lens", "window", "evaluated_on",
                    "data_through", "reporting_currency", "exchange_rates", "contents"):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["generation"], self.generation())
        self.assertEqual(data["totals_by_currency"]["SGD"]["gross_cost"], "650.00")
        self.assertEqual(data["columns"], ["gross_cost", "entry_count"])
        self.assertNotIn("cost_parts", data["rows"][0])

    def test_csv_comes_zipped_with_its_manifest_and_never_runs_a_formula(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            account = str(connection.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0])
            connection.execute("UPDATE accounts SET display_name='=HYPERLINK(\"http://x\")' WHERE id=?", (account,))
            connection.commit()
        finally:
            connection.close()
        report = self.save(group_by=["account"])
        response = self.export(format="csv", report_id=report)
        self.assertEqual(response.headers["content-type"], "application/zip")
        bundle = zipfile.ZipFile(io.BytesIO(response.content))
        names = sorted(bundle.namelist())
        self.assertEqual(len(names), 2)
        self.assertIn("manifest.json", names)
        table = list(csv.reader(io.StringIO(bundle.read(next(n for n in names if n.endswith(".csv"))).decode("utf-8-sig"))))
        self.assertEqual(table[0][:2], ["Account", "Currency"])
        labels = [row[0] for row in table[1:]]
        self.assertIn("'=HYPERLINK(\"http://x\")", labels)
        self.assertFalse(any(label.startswith("=") for label in labels))
        # Totals keep exact decimal text.
        self.assertIn(["Total", "SGD", "650.00", "50.00", "600.00"], [row[:5] for row in table])

    def test_html_is_one_self_contained_page_with_escaped_text(self) -> None:
        response = self.export(format="html", definition={
            key: value for key, value in _definition(name="<script>alert(1)</script>").items() if key != "reason"})
        page = response.content.decode()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("http://", page.replace('"http://x"', ""))
        self.assertNotIn("src=", page)
        manifest = page.split('id="manifest">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(manifest)["report"]["name"], "<script>alert(1)</script>")

    def test_an_export_contains_no_evidence_or_journal(self) -> None:
        report = self.save()
        for fmt in ("json", "html"):
            body = self.export(format=fmt, report_id=report).content.decode()
            self.assertNotIn("decision-journal", body)
            self.assertNotIn("natural_anchor", body)
            self.assertNotIn(str(self.database), body)
        bundle = zipfile.ZipFile(io.BytesIO(self.export(format="csv", report_id=report).content))
        self.assertTrue(all(name.endswith((".csv", ".json")) for name in bundle.namelist()))

    def test_bad_requests_are_refused(self) -> None:
        report = self.save()
        self.export(format="pdf", report_id=report, status=422)
        self.export(format="json", status=422)
        self.export(format="json", report_id=report, definition=_definition(), status=422)
        self.export(format="json", report_id=str(uuid.uuid4()), status=404)
        self.export(format="json", report_id=report, generation="stale", status=409)
        self.export(format="json", report_id=report, path="/tmp/out.json", status=422)


if __name__ == "__main__":
    unittest.main()
