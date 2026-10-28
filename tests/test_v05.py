"""v0.5 intake, inventory, maintenance and review application slices."""

from __future__ import annotations

import io
import json
import sqlite3
import uuid
from pathlib import Path

from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes
from tests.test_api import _ORIGIN, _ROWS, ApiTestBase


class V05ReadTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = self.bootstrap()

    def command(self, kind: str, anchors: dict[str, str], payload: dict[str, object]) -> dict:
        generation = self.overview()["generation"]
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": generation,
            "kind": kind,
            "anchors": anchors,
            "payload": payload,
        }
        preview = self.post("/api/v1/preview", body, self.csrf)
        self.assertEqual(preview.status_code, 200, preview.text)
        response = self.post("/api/v1/commands", body, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)
        return response.json()

    def test_setup_import_history_and_account_inventory_are_available(self) -> None:
        setup = self.client.get("/api/v1/setup/options")
        self.assertEqual(setup.status_code, 200, setup.text)
        self.assertEqual(setup.json()["state"], "ready")

        imports = self.client.get("/api/v1/imports")
        self.assertEqual(imports.status_code, 200, imports.text)
        self.assertEqual(len(imports.json()["imports"]), 1)
        import_id = imports.json()["imports"][0]["id"]
        self.assertEqual(self.client.get(f"/api/v1/imports/{import_id}").status_code, 200)

        accounts = self.client.get("/api/v1/accounts")
        self.assertEqual(accounts.status_code, 200, accounts.text)
        self.assertEqual(len(accounts.json()["accounts"]), 1)
        account = accounts.json()["accounts"][0]
        self.assertEqual(account["coverage_status"], "unconfigured")

    def test_account_scope_and_details_replay_from_the_journal(self) -> None:
        account = self.client.get("/api/v1/accounts").json()["accounts"][0]
        for pocket in account["currencies"]:
            self.command(
                "ACCOUNT_SCOPE_SET",
                {"account": account["id"], "pocket": pocket},
                {"expected_start": "2026-01-01", "expected_end": "2026-01-20", "reason": "Expected active period"},
            )
        self.command(
            "ACCOUNT_DETAILS_SET",
            {"account": account["id"]},
            {"display_name": "Daily spending", "archived_on": "", "reason": "Readable label"},
        )
        changed = self.client.get("/api/v1/accounts").json()["accounts"][0]
        self.assertEqual(changed["display_name"], "Daily spending")
        self.assertEqual(changed["coverage_status"], "covered")
        self.assertTrue(all(p["coverage_status"] == "covered" for p in changed["pockets"]))

    def test_category_metadata_and_review_deferral_use_stable_identity(self) -> None:
        category = "Lifestyle / Board Games"
        self.command(
            "CATEGORY_CREATE",
            {"category": category},
            {"category": category, "reason": "Track hobby spending"},
        )
        self.command(
            "CATEGORY_DETAILS_SET",
            {"category": category},
            {"display_group": "Lifestyle", "display_name": "Tabletop games", "reason": "Readable label"},
        )
        self.command(
            "CATEGORY_ARCHIVE_SET",
            {"category": category},
            {"archived": True, "reason": "Not currently used"},
        )
        categories = self.client.get("/api/v1/categories").json()["categories"]
        item = next(row for row in categories if row["key"] == category)
        self.assertEqual(item["display_name"], "Tabletop games")
        self.assertEqual(item["display_group"], "Lifestyle")
        self.assertTrue(item["archived"])

        review = self.client.get("/api/v1/review").json()
        if review["issues"]:
            issue = review["issues"][0]
            self.command(
                "REVIEW_DEFER_SET",
                {"issue": issue["issue_key"]},
                {"fingerprint": issue["fingerprint"], "reason": "Review later"},
            )
            refreshed = self.client.get("/api/v1/review").json()["issues"]
            self.assertTrue(next(row for row in refreshed if row["issue_key"] == issue["issue_key"])["deferred"])

    def test_rule_reads_and_inverse_preview(self) -> None:
        self.command(
            "MERCHANT_ALIAS_SET",
            {"canonical": "CAFE SOCIAL"},
            {"patterns": [{"exact": "CAFE SOCIAL"}], "display_name": "Cafe Social", "reason": "Normalize merchant"},
        )
        rules = self.client.get("/api/v1/rules")
        self.assertEqual(rules.status_code, 200, rules.text)
        self.assertEqual(rules.json()["aliases"][0]["canonical"], "CAFE SOCIAL")
        history = self.client.get("/api/v1/history").json()
        sequence = next(row["sequence"] for row in history if row["kind"] == "MERCHANT_ALIAS_SET")
        inverse = self.client.get(f"/api/v1/history/{sequence}/inverse-preview").json()
        self.assertTrue(inverse["supported"])
        self.assertEqual(inverse["command"]["kind"], "MERCHANT_ALIAS_RETIRE")

    def test_merchant_display_name_is_used_in_spending_views(self) -> None:
        self.command(
            "MERCHANT_ALIAS_SET",
            {"canonical": "CAFE SOCIAL"},
            {
                "patterns": [{"exact": "CAFE SOCIAL"}],
                "display_name": "Washing machine",
                "reason": "Personal bill name",
            },
        )
        overview = self.overview()
        merchant = next(row for row in overview["merchant_totals"] if row["name"] == "Washing machine")
        self.assertEqual(merchant["value"], "4.00")
        self.assertEqual(merchant["count"], 1)
        transactions = self.client.get(
            f"/api/v1/transactions?generation={overview['generation']}"
            f"&start={overview['start']}&end={overview['end']}&currency={overview['currency']}"
        ).json()["entries"]
        self.assertIn("Washing machine", {row["merchant"] for row in transactions})

    def test_category_override_reason_is_optional(self) -> None:
        overview = self.overview()
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": overview["generation"],
            "kind": "CATEGORY_OVERRIDE",
            "anchors": {"entry": self.first_anchor()},
            "payload": {"category": self.a_category(overview), "reason": ""},
        }
        self.assertEqual(self.post("/api/v1/preview", body, self.csrf).status_code, 200)
        response = self.post("/api/v1/commands", body, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)

    def test_rules_compile_returns_a_server_anchored_command_and_match_count(self) -> None:
        response = self.post(
            "/api/v1/rules/compile",
            {"match": {"description_contains": "o"}, "category": "Food & Dining / Groceries", "reason": "Frequent grocer"},
            self.csrf,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["command"]["kind"], "CATEGORY_RULE_SET")
        self.assertIn("rule_id", body["command"]["anchors"])
        self.assertGreaterEqual(body["match_count"], 0)
        self.assertEqual(body["match_count"], body["impact"]["predicate_matches"])
        self.assertIn("effective_assignments", body["impact"])
        # The compiled command is directly acceptable.
        self.command(
            body["command"]["kind"], body["command"]["anchors"], body["command"]["payload"],
        )

    def test_categorization_summary_and_candidates_are_generation_pinned(self) -> None:
        generation = self.overview()["generation"]
        summary = self.client.get(f"/api/v1/categorization/summary?generation={generation}")
        self.assertEqual(summary.status_code, 200, summary.text)
        body = summary.json()
        self.assertEqual(body["generation"], generation)
        self.assertEqual(
            sum(body["counts"].values()), body["total_selected"],
        )
        first = self.client.get(
            f"/api/v1/categorization/candidates?generation={generation}&limit=1"
        )
        self.assertEqual(first.status_code, 200, first.text)
        page = first.json()
        self.assertEqual(page["candidate_version"], "categorization-candidates-2026-v2")
        self.assertLessEqual(len(page["candidates"]), 1)
        if page["candidates"]:
            kind = page["candidates"][0]["kind"]
            filtered = self.client.get(
                f"/api/v1/categorization/candidates?generation={generation}&kind={kind}"
            )
            self.assertEqual(filtered.status_code, 200, filtered.text)
            self.assertTrue(all(item["kind"] == kind for item in filtered.json()["candidates"]))
            narrowed = self.client.get(
                f"/api/v1/categorization/candidates?generation={generation}&start=2026-01-01&end=2026-12-31"
            )
            self.assertEqual(narrowed.status_code, 200, narrowed.text)
            self.assertTrue(all(item["date_range"]["first"] >= "2026-01-01" for item in narrowed.json()["candidates"]))
        if page["next_cursor"]:
            second = self.client.get(
                f"/api/v1/categorization/candidates?generation={generation}&limit=1"
                f"&cursor={page['next_cursor']}"
            ).json()
            self.assertNotEqual(page["candidates"][0]["candidate_id"], second["candidates"][0]["candidate_id"])
        if page["candidates"]:
            detail = self.client.get(
                f"/api/v1/categorization/candidates/{page['candidates'][0]['candidate_id']}"
                f"?generation={generation}"
            )
            self.assertEqual(detail.status_code, 200, detail.text)
            record = detail.json()["records"][0]
            derived = self.post(
                "/api/v1/categorization/derive-rule",
                {"generation": generation, "anchors": [record["anchor"]]}, self.csrf,
            )
            self.assertEqual(derived.status_code, 200, derived.text)
            self.assertEqual(derived.json()["anchors"], [record["anchor"]])
            for option in derived.json()["options"]:
                self.assertIn(next(iter(option["match"])), {
                    "merchant_canonical", "paynow_to", "description_contains",
                })
        stale = self.client.get("/api/v1/categorization/summary?generation=sha256:stale")
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_alias_and_rule_can_commit_as_one_atomic_batch(self) -> None:
        generation = self.overview()["generation"]
        compiled = self.post(
            "/api/v1/rules/compile",
            {"match": {"merchant_canonical": "MY CAFE"}, "category": "Food & Dining / Groceries", "reason": "cafe"},
            self.csrf,
        ).json()["command"]
        body = {
            "request_id": str(uuid.uuid4()), "expected_generation": generation, "reason": "Normalize and categorize",
            "items": [
                {"kind": "MERCHANT_ALIAS_SET", "anchors": {"canonical": "MY CAFE"}, "payload": {"patterns": [{"exact": "CAFE"}], "reason": "Normalize and categorize"}},
                compiled,
            ],
        }
        response = self.post("/api/v1/batch", body, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)
        rules = self.client.get("/api/v1/rules").json()
        self.assertEqual(rules["aliases"][0]["canonical"], "MY CAFE")
        self.assertIn("observed_matches", rules["aliases"][0]["metrics"])
        self.assertEqual(rules["rules"][0]["category"], "Groceries")
        self.assertIn("effective_assignments", rules["rules"][0]["metrics"])
        rule_id = rules["rules"][0]["rule_id"]
        self.assertEqual(self.client.get(f"/api/v1/rules/{rule_id}/history").json()["history"][0]["kind"], "CATEGORY_RULE_SET")
        self.assertEqual(self.client.get("/api/v1/aliases/MY%20CAFE/history").json()["history"][0]["kind"], "MERCHANT_ALIAS_SET")

    def test_exact_impact_preview_is_generation_pinned_and_makes_no_writes(self) -> None:
        generation = self.overview()["generation"]
        before = self.client.get("/api/v1/rules").json()
        response = self.post(
            "/api/v1/categorization/impact",
            {"generation": generation, "match": {"description_contains": "o"}, "category": "Food & Dining / Groceries", "reason": "preview"},
            self.csrf,
        )
        self.assertEqual(response.status_code, 200, response.text)
        impact = response.json()["impact"]
        self.assertIn("predicate_matches", impact)
        self.assertIn("before_coverage", impact)
        self.assertIn("after_coverage", impact)
        self.assertIn("breakdowns", impact)
        self.assertIn("matched", impact["breakdowns"])
        self.assertIn("by_currency", impact["breakdowns"]["matched"])
        self.assertIn("overlaps", impact)
        self.assertIn("warnings", impact)
        self.assertEqual(self.client.get("/api/v1/rules").json(), before)
        stale = self.post(
            "/api/v1/categorization/impact",
            {"generation": "sha256:stale", "match": {"description_contains": "o"}, "category": "Food & Dining / Groceries", "reason": "preview"},
            self.csrf,
        )
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_rule_retirement_impact_is_exact_and_read_only(self) -> None:
        compiled = self.post(
            "/api/v1/rules/compile",
            {"match": {"description_contains": "o"}, "category": "Food & Dining / Groceries", "reason": "preview"}, self.csrf,
        ).json()["command"]
        self.command(compiled["kind"], compiled["anchors"], compiled["payload"])
        generation = self.overview()["generation"]
        response = self.client.get(
            f"/api/v1/rules/{compiled['anchors']['rule_id']}/retirement-impact?generation={generation}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreaterEqual(response.json()["impact"]["effective_assignments_removed"], 0)
        self.assertEqual(len(self.client.get("/api/v1/rules").json()["rules"]), 1)
        self.command("CATEGORY_RULE_RETIRE", compiled["anchors"], {"reason": "retire"})
        page = self.client.get("/api/v1/rules").json()
        self.assertEqual(page["inactive_rules"][0]["rule_id"], compiled["anchors"]["rule_id"])

    def test_combined_rule_compile_saves_category_and_role_in_one_batch(self) -> None:
        generation = self.overview()["generation"]
        compiled = self.post(
            "/api/v1/rules/compile-combined",
            {
                "generation": generation, "match": {"description_contains": "o"},
                "category": "Food & Dining / Groceries", "cost_behavior": "variable_routine",
                "need": "discretionary", "reason": "Grouped rule",
            },
            self.csrf,
        )
        self.assertEqual(compiled.status_code, 200, compiled.text)
        body = compiled.json()
        rule_id = body["rule_id"]
        kinds = {command["kind"] for command in body["commands"]}
        self.assertEqual(kinds, {"CATEGORY_RULE_SET", "ROLE_RULE_SET"})
        self.assertTrue(all(command["anchors"]["rule_id"] == rule_id for command in body["commands"]))
        self.assertIn("category", body["impact"])
        self.assertIn("role", body["impact"])

        batch = {
            "request_id": str(uuid.uuid4()), "expected_generation": generation, "reason": "Grouped rule",
            "items": body["commands"],
        }
        response = self.post("/api/v1/batch", batch, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)

        generation = self.overview()["generation"]
        grouped = self.client.get(f"/api/v1/rules/grouped?generation={generation}").json()
        row = next(item for item in grouped["grouped"] if item["rule_id"] == rule_id)
        self.assertEqual(set(row["kinds"]), {"category", "role"})
        self.assertEqual(row["category"], "Food & Dining / Groceries")
        self.assertEqual(row["cost_behavior"], "variable_routine")
        self.assertEqual(row["need"], "discretionary")

        # Editing with need/behavior cleared retires the role half only.
        cleared = self.post(
            "/api/v1/rules/compile-combined",
            {
                "generation": generation, "match": {"description_contains": "o"},
                "category": "Food & Dining / Groceries", "reason": "Drop the role rule",
            },
            self.csrf,
        ).json()
        self.assertEqual({command["kind"] for command in cleared["commands"]}, {"CATEGORY_RULE_SET", "ROLE_RULE_RETIRE"})
        response = self.post(
            "/api/v1/batch",
            {"request_id": str(uuid.uuid4()), "expected_generation": generation,
             "reason": "Drop the role rule", "items": cleared["commands"]},
            self.csrf,
        )
        self.assertIn(response.status_code, {200, 202}, response.text)
        generation = self.overview()["generation"]
        grouped = self.client.get(f"/api/v1/rules/grouped?generation={generation}").json()
        row = next(item for item in grouped["grouped"] if item["rule_id"] == rule_id)
        self.assertEqual(row["kinds"], ["category"])
        self.assertIsNone(row["cost_behavior"])

    def test_combined_rule_compile_requires_at_least_one_field(self) -> None:
        generation = self.overview()["generation"]
        response = self.post(
            "/api/v1/rules/compile-combined",
            {"generation": generation, "match": {"description_contains": "o"}, "reason": "nothing to set"},
            self.csrf,
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_combined_rule_retire_preview_returns_every_active_kind(self) -> None:
        generation = self.overview()["generation"]
        compiled = self.post(
            "/api/v1/rules/compile-combined",
            {
                "generation": generation, "match": {"description_contains": "o"},
                "category": "Food & Dining / Groceries", "cost_behavior": "variable_routine",
                "need": "discretionary", "reason": "Grouped rule",
            },
            self.csrf,
        ).json()
        rule_id = compiled["rule_id"]
        self.post(
            "/api/v1/batch",
            {"request_id": str(uuid.uuid4()), "expected_generation": generation,
             "reason": "Grouped rule", "items": compiled["commands"]},
            self.csrf,
        )
        generation = self.overview()["generation"]
        preview = self.post(
            f"/api/v1/rules/{rule_id}/retire-combined/preview",
            {"generation": generation, "reason": "No longer needed"},
            self.csrf,
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertEqual({command["kind"] for command in body["commands"]}, {"CATEGORY_RULE_RETIRE", "ROLE_RULE_RETIRE"})
        self.assertIn("category", body["impact"])
        self.assertIn("role", body["impact"])
        response = self.post(
            "/api/v1/batch",
            {"request_id": str(uuid.uuid4()), "expected_generation": generation,
             "reason": "No longer needed", "items": body["commands"]},
            self.csrf,
        )
        self.assertIn(response.status_code, {200, 202}, response.text)
        generation = self.overview()["generation"]
        grouped = self.client.get(f"/api/v1/rules/grouped?generation={generation}").json()
        self.assertFalse(any(item["rule_id"] == rule_id for item in grouped["grouped"]))

    def test_rules_compile_rejects_an_unknown_predicate(self) -> None:
        response = self.post(
            "/api/v1/rules/compile",
            {"match": {"not_a_predicate": "x"}, "category": "Food & Dining / Groceries", "reason": "bad"},
            self.csrf,
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_merchant_search_and_similar_routes(self) -> None:
        generation = self.overview()["generation"]
        search = self.client.get(f"/api/v1/merchants?generation={generation}&q=cafe")
        self.assertEqual(search.status_code, 200, search.text)
        merchants = search.json()["merchants"]
        self.assertEqual(len(merchants), 1)
        self.assertEqual(merchants[0]["merchant_key"], "CAFE SOCIAL")
        self.assertFalse(merchants[0]["has_rules"])

        similar = self.client.get(f"/api/v1/merchants/similar?generation={generation}&merchant_key=CAFE SOCIAL")
        self.assertEqual(similar.status_code, 200, similar.text)
        self.assertEqual(similar.json()["merchant_key"], "CAFE SOCIAL")

        missing = self.client.get(f"/api/v1/merchants/similar?generation={generation}&merchant_key=NOPE")
        self.assertEqual(missing.status_code, 404, missing.text)

        stale = self.client.get("/api/v1/merchants?generation=sha256:stale&q=")
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_rule_suggestions_route_is_generation_pinned(self) -> None:
        generation = self.overview()["generation"]
        suggestions = self.client.get(f"/api/v1/categorization/rule-suggestions?generation={generation}")
        self.assertEqual(suggestions.status_code, 200, suggestions.text)
        self.assertEqual(suggestions.json(), {"generation": generation, "suggestions": []})

        stale = self.client.get("/api/v1/categorization/rule-suggestions?generation=sha256:stale")
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_merchant_merge_preview_rewrites_rules_and_reports_a_conflict(self) -> None:
        generation = self.overview()["generation"]
        bookshop_rule = self.post(
            "/api/v1/rules/compile",
            {"match": {"merchant_canonical": "BOOKSHOP"}, "category": "Food & Dining / Groceries", "reason": "grocer-ish"},
            self.csrf,
        ).json()["command"]
        self.command(bookshop_rule["kind"], bookshop_rule["anchors"], bookshop_rule["payload"])
        grocer_rule = self.post(
            "/api/v1/rules/compile",
            {"match": {"merchant_canonical": "GROCER"}, "category": "Shopping / General Shopping", "reason": "shopping"},
            self.csrf,
        ).json()["command"]
        self.command(grocer_rule["kind"], grocer_rule["anchors"], grocer_rule["payload"])

        generation = self.overview()["generation"]
        preview = self.post(
            "/api/v1/categorization/merchant-merge/preview",
            {"generation": generation, "sources": ["BOOKSHOP", "GROCER"], "target": "Combined Store",
             "display_name": "Combined Store", "reason": "same shop"},
            self.csrf,
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertEqual(len(body["conflicts"]), 1)
        self.assertEqual(body["conflicts"][0]["kind"], "category")
        self.assertNotIn("CATEGORY_RULE_SET", [c["kind"] for c in body["commands"]])
        self.assertGreaterEqual(body["affected_transactions"], 2)

        resolved = self.post(
            "/api/v1/categorization/merchant-merge/preview",
            {"generation": generation, "sources": ["BOOKSHOP", "GROCER"], "target": "Combined Store",
             "display_name": "Combined Store", "reason": "same shop", "resolutions": {"category": "BOOKSHOP"}},
            self.csrf,
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        resolved_body = resolved.json()
        self.assertEqual(resolved_body["conflicts"], [])
        response = self.post("/api/v1/batch", {
            "request_id": str(uuid.uuid4()), "expected_generation": generation, "reason": "same shop",
            "items": resolved_body["commands"],
        }, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)
        rules = self.client.get("/api/v1/rules").json()
        self.assertEqual(len(rules["rules"]), 1)
        self.assertEqual(rules["rules"][0]["category"], "Groceries")

    def test_multipart_import_is_bounded_and_reuses_existing_evidence(self) -> None:
        response = self.client.post(
            "/api/v1/imports",
            data={"adapter": "REVOLUT_PDF"},
            files={"files": ("again.pdf", io.BytesIO(csv_bytes(_ROWS)), "application/pdf")},
            headers={"origin": _ORIGIN, "x-csrf-token": self.csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["accepted"], 1)
        self.assertTrue(response.json()["results"][0]["evidence_reused"])

    def upload(self, *files: tuple[str, bytes]) -> dict:
        response = self.client.post(
            "/api/v1/imports",
            files=[("files", (name, io.BytesIO(data), "application/pdf")) for name, data in files],
            headers={"origin": _ORIGIN, "x-csrf-token": self.csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_an_upload_names_its_account_period_rows_and_evidence_reach(self) -> None:
        later = [["Topup", "Current", "2026-01-21 10:00:00", "2026-01-21 10:00:01",
                  "Seed", "10.00", "0.00", "SGD", "COMPLETED", "435.75"],
                 ["Card Payment", "Current", "2026-01-31 10:00:00", "2026-01-31 10:00:01",
                  "Grocer", "-5.75", "0.00", "SGD", "COMPLETED", "430.00"]]
        body = self.upload(("late-january.pdf", csv_bytes(later)))

        (result,) = body["results"]
        self.assertEqual(
            (result["account_id"], result["period_start"], result["period_end"],
             result["transaction_count"]),
            ("acct_revolut_current", "2026-01-21", "2026-01-31", 2),
        )
        self.assertEqual((body["accepted"], body["quarantined"]), (1, 0))
        self.assertEqual(body["evidence_through"], [
            {"account_id": "acct_revolut_current", "display_name": "Revolut Current",
             "through": "2026-01-31"}])
        history = self.client.get("/api/v1/imports").json()["imports"][0]
        self.assertEqual(
            (history["filename"], history["outcome"], history["account_display_name"],
             history["period_end"], history["transaction_count"]),
            ("late-january.pdf", "ACCEPTED", "Revolut Current", "2026-01-31", 2),
        )

    def test_a_quarantined_upload_is_not_counted_as_accepted(self) -> None:
        unbalanced = (Path(__file__).parent / "fixtures" / "trust_savings_unbalanced.pdf").read_bytes()
        body = self.upload(("trust.pdf", unbalanced))

        self.assertEqual((body["accepted"], body["quarantined"]), (0, 1))
        history = self.client.get("/api/v1/imports").json()["imports"][0]
        self.assertEqual((history["filename"], history["outcome"]), ("trust.pdf", "QUARANTINED"))

    def test_a_redownloaded_statement_reads_as_already_imported(self) -> None:
        body = self.upload(("again.pdf", csv_bytes(_ROWS) + b"\n% downloaded again\n"))

        self.assertEqual(body["results"][0]["superseded_by"], "2026-01-01..2026-01-20")
        history = self.client.get("/api/v1/imports").json()["imports"][0]
        self.assertEqual(
            (history["outcome"], history["superseded_by"]),
            ("ALREADY_IMPORTED", "2026-01-01..2026-01-20"),
        )

    def test_a_conflicting_statement_names_both_files(self) -> None:
        changed = [*_ROWS[:3], ["Card Payment", "Current", "2026-01-20 10:00:00",
                                "2026-01-20 10:00:01", "Grocer", "-52.25", "0.00", "SGD",
                                "COMPLETED", "424.75"]]
        body = self.upload(("corrected.pdf", csv_bytes(changed)))

        self.assertEqual((body["accepted"], body["quarantined"]), (0, 1))
        history = self.client.get("/api/v1/imports").json()["imports"][0]
        self.assertEqual(history["outcome"], "CONFLICT")
        detail = self.client.get(f"/api/v1/imports/{history['id']}").json()
        self.assertEqual(
            sorted((row["filename"], row["this_import"]) for row in detail["conflict_with"]),
            [("corrected.pdf", True), ("revolut.csv", False)],
        )

    def test_an_upload_without_a_type_is_recognised_by_its_parser(self) -> None:
        mari = (Path(__file__).parent / "fixtures" / "mari_card.pdf").read_bytes()
        response = self.client.post(
            "/api/v1/imports",
            files=[("files", ("statement.pdf", io.BytesIO(mari), "application/pdf")),
                   ("files", ("scan.pdf", io.BytesIO((Path(__file__).parent / "fixtures" / "unclassified.pdf").read_bytes()), "application/pdf"))],
            headers={"origin": _ORIGIN, "x-csrf-token": self.csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        recognised, unknown = response.json()["results"]
        self.assertEqual((recognised["adapter"], recognised["detected"]), ("MARI_CARD", True))
        self.assertNotIn(recognised["status"], {"FAILED", "UNRECOGNISED"})
        self.assertEqual((unknown["adapter"], unknown["status"]), (None, "UNRECOGNISED"))
        self.assertIn("Choose its type", unknown["error"])
        self.assertEqual(response.json()["accepted"], 1)

    def test_fx_rates_endpoint_loads_a_json_rate_document(self) -> None:
        document = {
            "source": "MAS monthly average",
            "source_version": "mas-2026-v1",
            "quote_currency": "SGD",
            "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.30"}],
        }
        response = self.client.post(
            "/api/v1/fx-rates",
            data={"mas_weekly_csv": "false", "source_version": ""},
            files={"file": ("rates.json", io.BytesIO(json.dumps(document).encode()), "application/json")},
            headers={"origin": _ORIGIN, "x-csrf-token": self.csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "LOADED", "rates_written": 1})

    def test_fx_rates_endpoint_requires_source_version_for_mas_weekly_csv(self) -> None:
        response = self.client.post(
            "/api/v1/fx-rates",
            data={"mas_weekly_csv": "true", "source_version": ""},
            files={"file": ("rates.csv", io.BytesIO(b"x"), "text/csv")},
            headers={"origin": _ORIGIN, "x-csrf-token": self.csrf},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_categories_page_reports_active_rule_count_for_archive_preview(self) -> None:
        category = "Lifestyle / Board Games"
        self.command("CATEGORY_CREATE", {"category": category}, {"category": category, "reason": "hobby"})
        compiled = self.post(
            "/api/v1/rules/compile",
            {"match": {"description_contains": "a"}, "category": category, "reason": "auto"},
            self.csrf,
        ).json()["command"]
        self.command(compiled["kind"], compiled["anchors"], compiled["payload"])
        item = next(
            row for row in self.client.get("/api/v1/categories").json()["categories"]
            if row["key"] == category
        )
        self.assertEqual(item["active_rule_count"], 1)

    def test_import_endpoint_rejects_more_than_the_file_limit(self) -> None:
        from fincl.intake import MAX_FILES
        files = [("files", (f"f{i}.pdf", io.BytesIO(csv_bytes(_ROWS)), "application/pdf")) for i in range(MAX_FILES + 1)]
        response = self.client.post(
            "/api/v1/imports", data={"adapter": "REVOLUT_PDF"}, files=files,
            headers={"origin": _ORIGIN, "x-csrf-token": self.csrf},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_batch_is_idempotent_for_a_repeated_request_id(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            anchor = connection.execute(
                "SELECT natural_anchor FROM ledger_entries WHERE economic_direction='OUT' ORDER BY natural_anchor LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": self.overview()["generation"],
            "reason": "Idempotent bulk",
            "items": [{"kind": "CATEGORY_OVERRIDE", "anchors": {"entry": anchor}, "payload": {"category": "Food & Dining / Groceries", "reason": "Idempotent bulk"}}],
        }
        first = self.post("/api/v1/batch", body, self.csrf)
        self.assertIn(first.status_code, {200, 202}, first.text)
        second = self.post("/api/v1/batch", body, self.csrf)
        self.assertIn(second.status_code, {200, 202}, second.text)
        self.assertEqual(first.json()["journal_seq"], second.json()["journal_seq"])
        self.assertTrue(first.json()["appended"])
        self.assertFalse(second.json()["appended"])

    def test_account_alias_rejects_a_missing_owned_account(self) -> None:
        generation = self.overview()["generation"]
        response = self.post(
            "/api/v1/preview",
            {
                "request_id": str(uuid.uuid4()),
                "expected_generation": generation,
                "kind": "ACCOUNT_ALIAS_SET",
                "anchors": {"source_identity": "bank:unknown"},
                "payload": {"account_id": "missing", "reason": "Bind source"},
            },
            self.csrf,
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_transaction_anchors_materializes_the_filtered_selection(self) -> None:
        overview = self.overview()
        query = (
            f"generation={overview['generation']}&start={overview['start']}"
            f"&end={overview['end']}&currency=SGD&kind=spending"
        )
        response = self.client.get(f"/api/v1/transactions/anchors?{query}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["truncated"])
        self.assertEqual(body["generation"], overview["generation"])
        # Same filter through the paged list yields the same total.
        page = self.client.get(f"/api/v1/transactions?{query}&limit=200").json()
        self.assertEqual(len(body["anchors"]), page["total"])
        self.assertEqual(sorted(body["anchors"]), sorted(e["anchor"] for e in page["entries"]))

    def test_transaction_anchors_rejects_a_stale_generation(self) -> None:
        overview = self.overview()
        response = self.client.get(
            f"/api/v1/transactions/anchors?generation=sha256:stale"
            f"&start={overview['start']}&end={overview['end']}&currency=SGD"
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_bulk_category_batch_remains_atomic(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            anchors = [row[0] for row in connection.execute(
                "SELECT natural_anchor FROM ledger_entries WHERE economic_direction='OUT' ORDER BY natural_anchor LIMIT 2"
            )]
        finally:
            connection.close()
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": self.overview()["generation"],
            "reason": "Bulk correction",
            "items": [
                {"kind": "CATEGORY_OVERRIDE", "anchors": {"entry": anchor}, "payload": {"category": "Food & Dining / Groceries", "reason": "Bulk correction"}}
                for anchor in anchors
            ],
        }
        response = self.post("/api/v1/batch", body, self.csrf)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["decision_kind"], "DECISION_BATCH")
