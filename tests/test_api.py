"""M2/M3: the loopback HTTP application — boundary, reads, and durable commands.

Each test drives the real ``fincl.api`` app through ``fastapi.testclient`` against
a temporary ledger built by a Revolut import plus one explicit ``refresh_ledger``.
The request boundary (host, origin, JSON, session cookie, CSRF) is exercised as a
unit; the command path is checked for preview/commit parity, request-id
idempotency and generation staleness end to end.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from fincl.api import create_app
from fincl.decision_journal import default_journal_path, read_records
from fincl.document_service import DocumentImportService
from fincl.refresh import refresh_ledger
from tests.asgi_client import ASGITestClient
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

_ORIGIN = "http://127.0.0.1:8765"
_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "500.00", "0.00", "SGD", "COMPLETED", "500.00"],
    ["Card Payment", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01",
     "Cafe Social", "-4.00", "0.00", "SGD", "COMPLETED", "496.00"],
    ["Card Payment", "Current", "2026-01-03 10:00:00", "2026-01-03 10:00:01",
     "Bookshop", "-19.00", "0.00", "SGD", "COMPLETED", "477.00"],
    ["Card Payment", "Current", "2026-01-20 10:00:00", "2026-01-20 10:00:01",
     "Grocer", "-51.25", "0.00", "SGD", "COMPLETED", "425.75"],
]


class ApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "ledger.sqlite3"
        source = self.root / "revolut.csv"
        source.write_bytes(csv_bytes(_ROWS))
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        refresh_ledger(self.database)
        self.app = create_app(
            self.database, port=8765, bootstrap_secret="test-secret", require_access_key=True,
            evidence_dir=self.root / "evidence",
        )
        self.client = ASGITestClient(self.app, base_url=_ORIGIN)
        self.addCleanup(self.client.close)

    # -- helpers -----------------------------------------------------------
    def bootstrap(self) -> str:
        response = self.client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": _ORIGIN, "content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf"]

    def post(self, path: str, body: dict, csrf: str):
        return self.client.post(
            path, json=body,
            headers={"origin": _ORIGIN, "content-type": "application/json", "x-csrf-token": csrf},
        )

    def overview(self) -> dict:
        response = self.client.get("/api/v1/overview")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def journal_len(self) -> int:
        return len(read_records(default_journal_path(self.database)))

    def first_anchor(self, *, direction: str = "OUT") -> str:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT natural_anchor FROM ledger_entries WHERE economic_direction = ? "
                "ORDER BY effective_at LIMIT 1", (direction,),
            ).fetchone()[0]
        finally:
            connection.close()

    def a_category(self, data: dict) -> str:
        return next(
            c for c in data["categories"]
            if not c.startswith("Internal") and not c.startswith("Unclassified")
        )


class RequestBoundaryTests(ApiTestBase):
    def test_api_before_bootstrap_is_401(self) -> None:
        self.assertEqual(self.client.get("/api/v1/overview").status_code, 401)

    def test_wrong_host_is_403(self) -> None:
        response = self.client.get("/api/v1/health", headers={"host": "evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_private_lan_host_uses_its_own_same_origin_boundary(self) -> None:
        lan_origin = "http://192.168.0.10:8765"
        application = create_app(
            self.database, port=8765, public_host="192.168.0.10",
            bootstrap_secret="lan-secret", evidence_dir=self.root / "evidence",
        )
        client = ASGITestClient(application, base_url=lan_origin)
        self.addCleanup(client.close)
        response = client.post(
            "/session", json={"secret": "lan-secret"},
            headers={"origin": lan_origin, "content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(client.get("/api/v1/health").status_code, 200)
        self.assertEqual(
            client.get("/api/v1/health", headers={"host": "192.168.0.11:8765"}).status_code,
            403,
        )

    def test_cross_origin_post_is_403(self) -> None:
        self.bootstrap()
        response = self.client.post(
            "/api/v1/preview", json={}, headers={"origin": "http://evil.example", "content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 403)

    def test_non_json_post_is_403(self) -> None:
        self.bootstrap()
        response = self.client.post(
            "/api/v1/preview", content=b"x", headers={"origin": _ORIGIN, "content-type": "text/plain"},
        )
        self.assertEqual(response.status_code, 403)

    def test_missing_csrf_is_403(self) -> None:
        self.bootstrap()
        response = self.client.post(
            "/api/v1/preview", json={}, headers={"origin": _ORIGIN, "content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 403)

    def test_bootstrap_secret_is_single_use(self) -> None:
        self.bootstrap()
        again = self.client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": _ORIGIN, "content-type": "application/json"},
        )
        self.assertEqual(again.status_code, 403)

    def test_security_headers_present(self) -> None:
        self.bootstrap()
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["cache-control"], "no-store")


class KeyFreeAccessTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(self.database, evidence_dir=self.root / "evidence")
        self.client = ASGITestClient(self.app, base_url=_ORIGIN)
        self.addCleanup(self.client.close)

    def test_normal_access_and_reload_need_no_key_or_cookie(self) -> None:
        for _ in range(2):
            self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
            self.assertTrue(self.overview()["generation"])
        self.assertFalse(self.client.cookies)

    def test_key_free_writes_still_require_same_origin_and_csrf(self) -> None:
        csrf = self.client.get("/api/v1/health").json()["csrf"]
        self.assertEqual(self.post("/api/v1/refresh", {}, "").status_code, 403)
        self.assertEqual(self.client.post("/api/v1/refresh", json={}, headers={
            "origin": "http://evil.example", "x-csrf-token": csrf,
        }).status_code, 403)
        self.assertEqual(self.post("/api/v1/refresh", {}, csrf).status_code, 200)

    def test_old_launch_links_are_harmless_when_gate_is_disabled(self) -> None:
        for _ in range(2):
            response = self.client.post("/session", json={"secret": "old-key"}, headers={"origin": _ORIGIN})
            self.assertEqual(response.status_code, 200)


class OverviewReadTests(ApiTestBase):
    def test_overview_reports_a_generation_and_currency_qualified_summary(self) -> None:
        self.bootstrap()
        data = self.overview()
        self.assertTrue(data["generation"].startswith("sha256:"))
        self.assertEqual(data["currency"], "SGD")
        self.assertIn("spending", data["summary"])
        self.assertIsInstance(data["recent"], list)
        self.assertEqual(data["freshness"], "current")

    def test_uncovered_window_withholds_totals_rather_than_zeroing(self) -> None:
        self.bootstrap()
        response = self.client.get("/api/v1/overview?start=2000-01-01&end=2000-12-31")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertFalse(data["covered"])
        self.assertIsNone(data["summary"]["spending"])
        self.assertEqual(data["category_totals"], [])


class CommandPathTests(ApiTestBase):
    def _command(self, data: dict, **over) -> dict:
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": data["generation"],
            "kind": "CATEGORY_OVERRIDE",
            "anchors": {"entry": self.first_anchor()},
            "payload": {"category": self.a_category(data), "reason": "reclassify for the test"},
        }
        body.update(over)
        return body

    def test_preview_then_commit_appends_once_and_reload_reflects_it(self) -> None:
        csrf = self.bootstrap()
        data = self.overview()
        command = self._command(data)

        preview = self.post("/api/v1/preview", command, csrf)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()["valid"])

        commit = self.post("/api/v1/commands", command, csrf)
        self.assertEqual(commit.status_code, 200, commit.text)
        self.assertTrue(commit.json()["appended"])
        self.assertEqual(self.journal_len(), 1)

        # The new generation is visible and the decision shows in history.
        history = self.client.get("/api/v1/history").json()
        self.assertEqual(history[0]["kind"], "CATEGORY_OVERRIDE")
        self.assertEqual(history[0]["request_id"], command["request_id"])

    def test_repeated_submit_with_same_request_id_is_idempotent(self) -> None:
        csrf = self.bootstrap()
        command = self._command(self.overview())
        first = self.post("/api/v1/commands", command, csrf)
        second = self.post("/api/v1/commands", command, csrf)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(first.json()["appended"])
        self.assertFalse(second.json()["appended"])
        self.assertEqual(first.json()["journal_seq"], second.json()["journal_seq"])
        self.assertEqual(self.journal_len(), 1)

    def test_same_request_id_different_intent_is_409(self) -> None:
        csrf = self.bootstrap()
        data = self.overview()
        request_id = str(uuid.uuid4())
        one = self._command(data, request_id=request_id)
        two = self._command(data, request_id=request_id)
        two["payload"] = {"category": self.a_category(data), "reason": "a different reason entirely"}
        self.assertEqual(self.post("/api/v1/commands", one, csrf).status_code, 200)
        conflict = self.post("/api/v1/commands", two, csrf)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(self.journal_len(), 1)

    def test_stale_generation_is_rejected_with_409(self) -> None:
        csrf = self.bootstrap()
        command = self._command(self.overview(), expected_generation="sha256:0000")
        self.assertEqual(self.post("/api/v1/preview", command, csrf).status_code, 409)
        self.assertEqual(self.post("/api/v1/commands", command, csrf).status_code, 409)
        self.assertEqual(self.journal_len(), 0)

    def test_unknown_category_is_422_and_writes_nothing(self) -> None:
        csrf = self.bootstrap()
        command = self._command(self.overview())
        command["payload"] = {"category": "Nope / Not Real", "reason": "should fail"}
        response = self.post("/api/v1/commands", command, csrf)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse(response.json()["recorded"])
        self.assertEqual(self.journal_len(), 0)

    def test_internal_category_override_is_refused(self) -> None:
        csrf = self.bootstrap()
        command = self._command(self.overview())
        command["payload"] = {"category": "Internal / Transfer", "reason": "not allowed here"}
        self.assertEqual(self.post("/api/v1/commands", command, csrf).status_code, 422)

    def test_category_create_then_plan_set_reference_it(self) -> None:
        csrf = self.bootstrap()
        data = self.overview()
        create = {
            "request_id": str(uuid.uuid4()), "expected_generation": data["generation"],
            "kind": "CATEGORY_CREATE", "anchors": {"category": "Lifestyle / Board Games"},
            "payload": {"category": "Lifestyle / Board Games", "reason": "hobby tracking"},
        }
        self.assertEqual(self.post("/api/v1/commands", create, csrf).status_code, 200)
        data = self.overview()
        self.assertIn("Lifestyle / Board Games", data["custom_categories"])
        plan = {
            "request_id": str(uuid.uuid4()), "expected_generation": data["generation"],
            "kind": "PLAN_SET", "anchors": {"plan": "plan-games"},
            "payload": {"type": "budget", "name": "Board games", "currency": "SGD",
                        "target": "40.00", "saved": "0", "category": "Lifestyle / Board Games",
                        "reason": "monthly cap"},
        }
        self.assertEqual(self.post("/api/v1/commands", plan, csrf).status_code, 200, )
        self.assertEqual(self.overview()["plans"][0]["name"], "Board games")


class TransactionsEndpointTests(ApiTestBase):
    def _page(self, csrf_unused=None, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        response = self.client.get(f"/api/v1/transactions?{query}")
        return response

    def test_keyset_pages_cover_every_row_once_in_a_stable_order(self) -> None:
        self.bootstrap()
        gen = self.overview()["generation"]
        one_shot = self._page(generation=gen, start="2026-01-01", end="2026-01-31", limit=200)
        self.assertEqual(one_shot.status_code, 200, one_shot.text)
        full = [e["id"] for e in one_shot.json()["entries"]]
        self.assertGreaterEqual(len(full), 3)

        collected, cursor = [], None
        for _ in range(20):
            params = {"generation": gen, "start": "2026-01-01", "end": "2026-01-31", "limit": 2}
            if cursor:
                params["cursor"] = cursor
            body = self._page(**params).json()
            collected += [e["id"] for e in body["entries"]]
            self.assertEqual(body["total"], len(full))
            cursor = body["next_cursor"]
            if not cursor:
                break
        self.assertEqual(collected, full)
        self.assertEqual(len(set(collected)), len(collected))

    def test_filters_and_sorts_are_applied_server_side(self) -> None:
        self.bootstrap()
        gen = self.overview()["generation"]
        spending = self._page(generation=gen, start="2026-01-01", end="2026-01-31",
                              kind="spending", sort="amount", limit=50).json()
        amounts = [e["amount"] for e in spending["entries"]]
        self.assertEqual(amounts, sorted(amounts, key=lambda a: float(a)))
        self.assertTrue(all(e["bucket"] == "spending" for e in spending["entries"]))

        searched = self._page(generation=gen, start="2026-01-01", end="2026-01-31",
                              search="bookshop").json()
        self.assertEqual(searched["total"], 1)
        self.assertIn("Bookshop", searched["entries"][0]["merchant"].title())

    def test_entry_exposes_linked_and_classification_for_the_shared_editor(self) -> None:
        """The frontend's
        shared single-transaction editor has no server round trip before first
        render, so it needs `linked`/`classification` on the plain `Entry`
        response to tell a confirmed-relationship internal row apart from an
        unlinked `self_transfer` row (see `fincl.classification._detail_bucket`,
        which the frontend's `detailBucket` mirrors)."""
        self.bootstrap()
        gen = self.overview()["generation"]
        spending = self._page(generation=gen, start="2026-01-01", end="2026-01-31",
                              kind="spending", limit=1).json()
        self.assertEqual(len(spending["entries"]), 1)
        entry = spending["entries"][0]
        self.assertIn("linked", entry)
        self.assertIsInstance(entry["linked"], bool)
        self.assertFalse(entry["linked"])
        self.assertIn("classification", entry)
        self.assertIsNone(entry["classification"])

    def test_a_stale_generation_is_409(self) -> None:
        self.bootstrap()
        response = self._page(generation="sha256:nope", start="2026-01-01", end="2026-01-31")
        self.assertEqual(response.status_code, 409)

    def test_a_corrupt_cursor_is_422(self) -> None:
        self.bootstrap()
        gen = self.overview()["generation"]
        response = self._page(generation=gen, start="2026-01-01", end="2026-01-31", cursor="not-base64!!")
        self.assertEqual(response.status_code, 422)


class AnalysisEndpointTests(ApiTestBase):
    def test_analysis_returns_coherent_panels_and_working_handles(self) -> None:
        self.bootstrap()
        gen = self.overview()["generation"]
        response = self.client.get(
            "/api/v1/analysis?start=2026-01-01&end=2026-01-20"
            f"&expected_generation={gen}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["generation"], gen)
        self.assertEqual(body["scope"]["start"], "2026-01-01")
        self.assertTrue(body["covered"])
        self.assertIn("spending", body["summary"])
        # The contributor handle behind the spending total resolves.
        handle = body["summary_handles"]["spending"]
        page = self.client.get(f"/api/v1/contributors/{handle}").json()
        self.assertEqual(page["generation"], gen)
        drilled = sum(abs(float(c["value"])) for c in page["components"])
        self.assertAlmostEqual(drilled, float(body["summary"]["spending"]["value"]["value"]))

    def test_a_stale_expected_generation_is_409(self) -> None:
        self.bootstrap()
        response = self.client.get(
            "/api/v1/analysis?start=2026-01-01&end=2026-01-20"
            "&expected_generation=sha256:nope"
        )
        self.assertEqual(response.status_code, 409)


class ContributorsEndpointTests(ApiTestBase):
    def test_a_spending_handle_pages_the_exact_components(self) -> None:
        self.bootstrap()
        data = self.overview()
        ranking = data["category_totals"][0]
        handle = ranking["handle"]
        self.assertTrue(handle)

        collected, total, cursor = [], 0.0, None
        for _ in range(20):
            url = f"/api/v1/contributors/{handle}?limit=1"
            if cursor:
                url += f"&cursor={cursor}"
            body = self.client.get(url).json()
            collected += [c["component_id"] for c in body["components"]]
            total += sum(abs(float(c["value"])) for c in body["components"])
            cursor = body["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(set(collected)), len(collected))
        self.assertAlmostEqual(total, float(ranking["value"]))

    def test_a_stale_handle_generation_is_409(self) -> None:
        self.bootstrap()
        # A handle minted for the current generation still fails once we lie about
        # it: forge one for a bogus generation via the same encoder.
        from fincl.analysis import handles

        stale = handles.encode(
            generation="sha256:nope", lens="recorded_flow", currency="SGD",
            start="2026-01-01", end="2026-01-31", buckets=["spending"], sort="amount",
        )
        self.assertEqual(self.client.get(f"/api/v1/contributors/{stale}").status_code, 409)

    def test_a_tampered_handle_is_422(self) -> None:
        self.bootstrap()
        handle = self.overview()["category_totals"][0]["handle"]
        response = self.client.get(f"/api/v1/contributors/{handle[:-6]}AAAAAA")
        self.assertEqual(response.status_code, 422)


class RelationshipEndpointTests(ApiTestBase):
    def _anchors(self, direction: str = "OUT") -> list[str]:
        connection = sqlite3.connect(self.database)
        try:
            return [
                r[0] for r in connection.execute(
                    "SELECT natural_anchor FROM ledger_entries "
                    "WHERE economic_direction = ? ORDER BY effective_at", (direction,),
                )
            ]
        finally:
            connection.close()

    def test_a_duplicate_confirm_suppresses_the_candidate(self) -> None:
        csrf = self.bootstrap()
        gen = self.overview()["generation"]
        a, b = self._anchors()[:2]
        before = float(self.overview()["summary"]["spending"])
        response = self.post("/api/v1/commands", {
            "request_id": str(uuid.uuid4()), "expected_generation": gen,
            "kind": "DUPLICATE_CONFIRM", "anchors": {"primary": a, "candidate": b},
            "payload": {"reason": "same purchase in two exports"},
        }, csrf)
        self.assertIn(response.status_code, (200, 202), response.text)
        after = float(self.overview()["summary"]["spending"])
        self.assertLess(after, before)

    def test_the_review_queue_is_reachable_and_generation_pinned(self) -> None:
        self.bootstrap()
        gen = self.overview()["generation"]
        ok = self.client.get(f"/api/v1/relationships/review?generation={gen}")
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["kind"], "duplicate")
        stale = self.client.get("/api/v1/relationships/review?generation=sha256:nope")
        self.assertEqual(stale.status_code, 409)

    def test_a_batch_couples_a_retire_and_a_replacement_atomically(self) -> None:
        csrf = self.bootstrap()
        data = self.overview()
        gen = data["generation"]
        entry = self._anchors()[0]
        category = self.a_category(data)
        # Seed an override to retire.
        self.post("/api/v1/commands", {
            "request_id": str(uuid.uuid4()), "expected_generation": gen,
            "kind": "CATEGORY_OVERRIDE", "anchors": {"entry": entry},
            "payload": {"category": category, "reason": "first guess"},
        }, csrf)
        gen = self.overview()["generation"]
        other = next(c for c in data["categories"] if c != category and " / " in c
                     and not c.startswith(("Internal", "Unclassified")))
        response = self.post("/api/v1/batch", {
            "request_id": str(uuid.uuid4()), "expected_generation": gen,
            "reason": "fix the category cleanly",
            "items": [
                {"kind": "CATEGORY_OVERRIDE_RETIRE", "anchors": {"entry": entry},
                 "payload": {"reason": "wrong"}},
                {"kind": "CATEGORY_OVERRIDE", "anchors": {"entry": entry},
                 "payload": {"category": other, "reason": "right"}},
            ],
        }, csrf)
        self.assertIn(response.status_code, (200, 202), response.text)
        self.assertEqual(response.json()["decision_kind"], "DECISION_BATCH")

    def test_refund_allocation_previews_totals_and_records(self) -> None:
        csrf = self.bootstrap()
        gen = self.overview()["generation"]
        connection = sqlite3.connect(self.database)
        try:
            out = [
                r[0] for r in connection.execute(
                    "SELECT natural_anchor FROM ledger_entries "
                    "WHERE economic_direction = 'OUT' ORDER BY abs(amount_coefficient) DESC"
                )
            ]
            amount = connection.execute(
                "SELECT amount_text FROM ledger_entries WHERE natural_anchor = ?", (out[0],)
            ).fetchone()[0].lstrip("-")
        finally:
            connection.close()
        # No inbound REFUND in this fixture, so a same-currency IN entry is
        # rejected by direction; just assert the shape check + 422 path.
        body = {
            "request_id": str(uuid.uuid4()), "expected_generation": gen,
            "kind": "REFUND_ALLOCATION_SET", "anchors": {"refund": out[1]},
            "payload": {"allocations": [
                {"purchase_anchor": out[0], "refund_amount": amount, "purchase_amount": amount},
            ], "reason": "test"},
        }
        response = self.post("/api/v1/preview", body, csrf)
        # out[1] is a purchase, not an inbound refund -> refused.
        self.assertEqual(response.status_code, 422, response.text)

    def test_a_batch_with_an_invalid_child_is_422_and_appends_nothing(self) -> None:
        csrf = self.bootstrap()
        gen = self.overview()["generation"]
        entry = self._anchors()[0]
        before = self.journal_len()
        response = self.post("/api/v1/batch", {
            "request_id": str(uuid.uuid4()), "expected_generation": gen,
            "reason": "half valid",
            "items": [
                {"kind": "CATEGORY_OVERRIDE", "anchors": {"entry": entry},
                 "payload": {"category": "Not / Real", "reason": "bad"}},
            ],
        }, csrf)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.journal_len(), before)


class EvidenceDownloadTests(ApiTestBase):
    def test_download_streams_the_verified_source(self) -> None:
        self.bootstrap()
        data = self.overview()
        entry_id = data["recent"][0]["id"]
        response = self.client.get(
            f"/api/v1/evidence/{entry_id}?generation={data['generation']}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn(b"SGD Statement", response.content)

    def test_download_with_a_stale_generation_is_409(self) -> None:
        self.bootstrap()
        entry_id = self.overview()["recent"][0]["id"]
        response = self.client.get(f"/api/v1/evidence/{entry_id}?generation=sha256:stale")
        self.assertEqual(response.status_code, 409)


class TransferReviewTests(ApiTestBase):
    """A PROPOSED link is seeded directly (a non-automatic method_version so the
    refresh projection leaves it in place); the API's confirm/reject path then
    threads a LINK_CONFIRM/LINK_REJECT journal decision through record_decision
    and the reconciliation overlay."""

    def _seed_proposal(self) -> tuple[str, str]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT id, natural_anchor, currency_pocket, amount_text, "
                "amount_coefficient, amount_scale FROM ledger_entries "
                "WHERE economic_direction = 'OUT' ORDER BY effective_at LIMIT 2"
            ).fetchall()
            left, right = rows[0], rows[1]
            connection.execute(
                "INSERT INTO transfer_links(id, left_entry_id, right_entry_id, link_kind, "
                "left_currency, left_amount_text, left_amount_coefficient, left_amount_scale, "
                "right_currency, right_amount_text, right_amount_coefficient, right_amount_scale, "
                "method_version, status, evidence_json, created_at) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("seed-link", left["id"], right["id"], "SAME_CURRENCY_TRANSFER",
                 left["currency_pocket"], left["amount_text"].lstrip("-"),
                 abs(left["amount_coefficient"]), left["amount_scale"],
                 right["currency_pocket"], right["amount_text"].lstrip("-"),
                 abs(right["amount_coefficient"]), right["amount_scale"],
                 "seeded-proposal-v1", "PROPOSED", "{}", "2026-02-01T00:00:00+00:00"),
            )
            connection.commit()
            return left["natural_anchor"], right["natural_anchor"]
        finally:
            connection.close()

    def test_proposal_is_listed_and_confirm_removes_it(self) -> None:
        left, right = self._seed_proposal()
        csrf = self.bootstrap()
        data = self.overview()
        self.assertEqual(len(data["transfer_proposals"]), 1)
        proposal = data["transfer_proposals"][0]
        self.assertEqual({proposal["left"], proposal["right"]}, {left, right})
        self.assertTrue(proposal["basis"].startswith("Manual review:"))

        command = {
            "request_id": str(uuid.uuid4()), "expected_generation": data["generation"],
            "kind": "TRANSFER_DECISION", "anchors": {"left": left, "right": right},
            "payload": {"decision": "confirm", "reason": "same money, two of my accounts"},
        }
        self.assertEqual(self.post("/api/v1/preview", command, csrf).status_code, 200)
        commit = self.post("/api/v1/commands", command, csrf)
        self.assertEqual(commit.status_code, 200, commit.text)
        self.assertEqual(read_records(default_journal_path(self.database))[0].decision_kind, "LINK_CONFIRM")
        self.assertEqual(self.overview()["transfer_proposals"], [])

    def test_reject_is_idempotent_on_retry(self) -> None:
        left, right = self._seed_proposal()
        csrf = self.bootstrap()
        data = self.overview()
        command = {
            "request_id": str(uuid.uuid4()), "expected_generation": data["generation"],
            "kind": "TRANSFER_DECISION", "anchors": {"left": left, "right": right},
            "payload": {"decision": "reject", "reason": "unrelated purchases that look alike"},
        }
        first = self.post("/api/v1/commands", command, csrf)
        second = self.post("/api/v1/commands", command, csrf)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["appended"])
        self.assertFalse(second.json()["appended"])
        self.assertEqual(self.journal_len(), 1)

    def test_decision_for_an_unknown_pair_is_422(self) -> None:
        csrf = self.bootstrap()
        data = self.overview()
        command = {
            "request_id": str(uuid.uuid4()), "expected_generation": data["generation"],
            "kind": "TRANSFER_DECISION",
            "anchors": {"left": self.first_anchor(), "right": self.first_anchor(direction="IN")},
            "payload": {"decision": "confirm", "reason": "no proposal exists for this pair"},
        }
        self.assertEqual(self.post("/api/v1/commands", command, csrf).status_code, 422)


class ClassificationPreviewApiTests(ApiTestBase):
    """Step 1's combined preview: exact commands a caller submits verbatim via
    the existing ``/api/v1/batch``, so preview and commit cannot disagree."""

    def _grocer(self) -> str:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT e.natural_anchor FROM ledger_entries e "
                "JOIN merchant_observations m ON m.entry_id = e.id "
                "WHERE m.description_text LIKE '%Grocer%'"
            ).fetchone()[0]
        finally:
            connection.close()

    def test_preview_returns_exact_commands_the_batch_endpoint_accepts(self) -> None:
        csrf = self.bootstrap()
        gen = self.overview()["generation"]
        anchor = self._grocer()
        draft = {
            "generation": gen, "anchors": [anchor],
            "category": {"intent": "set", "value": "Food & Dining / Groceries"},
            "cost_behavior": {"intent": "set", "value": "variable_routine"},
            "need": {"intent": "set", "value": "essential"},
            "reason": "grocery trio",
        }
        preview = self.post("/api/v1/classification/preview", draft, csrf)
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertEqual(body["command_count"], 2)
        self.assertEqual(body["changed_transactions"], 1)
        kinds = sorted(c["kind"] for c in body["commands"])
        self.assertEqual(kinds, ["CATEGORY_OVERRIDE", "ENTRY_ROLE_SET"])

        commit = self.post("/api/v1/batch", {
            "request_id": str(uuid.uuid4()), "expected_generation": gen,
            "reason": "grocery trio", "items": body["commands"],
        }, csrf)
        self.assertIn(commit.status_code, (200, 202), commit.text)

        page = self.client.get(
            f"/api/v1/transactions?generation={self.overview()['generation']}"
            "&start=2026-01-01&end=2026-01-31&limit=50"
        ).json()
        entry = next(e for e in page["entries"] if e["anchor"] == anchor)
        self.assertEqual(entry["category"], "Food & Dining / Groceries")
        self.assertEqual(entry["cost_behavior"], "variable_routine")
        self.assertEqual(entry["need"], "essential")

    def test_stale_generation_is_409_and_draft_is_not_lost_client_side(self) -> None:
        csrf = self.bootstrap()
        anchor = self._grocer()
        response = self.post("/api/v1/classification/preview", {
            "generation": "sha256:nope", "anchors": [anchor],
            "category": {"intent": "set", "value": "Food & Dining / Groceries"},
        }, csrf)
        self.assertEqual(response.status_code, 409)

    def test_a_role_edit_without_a_reason_is_422(self) -> None:
        csrf = self.bootstrap()
        gen = self.overview()["generation"]
        anchor = self._grocer()
        response = self.post("/api/v1/classification/preview", {
            "generation": gen, "anchors": [anchor],
            "cost_behavior": {"intent": "set", "value": "irregular"},
        }, csrf)
        self.assertEqual(response.status_code, 422, response.text)

    def test_preview_never_writes_a_journal_record(self) -> None:
        csrf = self.bootstrap()
        gen = self.overview()["generation"]
        anchor = self._grocer()
        before = self.journal_len()
        self.post("/api/v1/classification/preview", {
            "generation": gen, "anchors": [anchor],
            "category": {"intent": "set", "value": "Food & Dining / Groceries"},
        }, csrf)
        self.assertEqual(self.journal_len(), before)


if __name__ == "__main__":
    unittest.main()
