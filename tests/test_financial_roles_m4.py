"""Cash-flow usability M4 — safe bulk assignment.

The §3.2 precedence fix (a confirmed recurring occurrence outranks an active
role rule for cost behavior) and the §4.1 filter set (direction, behavior,
need, basis, unresolved-only) on both the transaction read and the frozen
anchor materialization that a bulk commit is built from.
"""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fincl.application import record_decision
from fincl.categorization import canonical_match, rule_id_for
from fincl.db import connect_readonly
from fincl.document_service import DocumentImportService
from fincl.financial_roles import effective_financial_role
from fincl.generation import current_generation
from fincl.queries import transaction_anchors, transactions_page
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes


def _revolut_ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "fincl.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


class PrecedenceTests(unittest.TestCase):
    """§3.2: rank 2 (bound recurring occurrence) beats rank 3 (role rule)."""

    def _entry(self, **over: object) -> dict[str, object]:
        base: dict[str, object] = {
            "bucket": "spending",
            "category": "Food & Dining / Groceries",
            "currency_pocket": "SGD",
            "amount_coefficient": "-90000",
            "amount_scale": "2",
            "account_id": "a1",
            "description_text": "NETFLIX",
        }
        base.update(over)
        return base

    def _rule(self, seq: int, **over: object) -> dict[str, object]:
        rule: dict[str, object] = {
            "journal_seq": seq,
            "match_json": '{"description_contains": "NETFLIX"}',
            "cost_behavior": "variable_routine",
            "need": None,
        }
        rule.update(over)
        return rule

    def test_a_role_rule_does_not_replace_a_confirmed_recurring_behavior(self) -> None:
        role = effective_financial_role(
            self._entry(recurring_confirmed=True), [self._rule(1)]
        )
        self.assertEqual(role["cost_behavior"], "fixed_recurring")
        self.assertEqual(role["cost_behavior_basis"], "recurring evidence")

    def test_a_role_rule_still_sets_need_when_recurring_fixes_only_behavior(self) -> None:
        role = effective_financial_role(
            self._entry(recurring_confirmed=True),
            [self._rule(1, cost_behavior=None, need="discretionary")],
        )
        self.assertEqual(role["cost_behavior"], "fixed_recurring")
        self.assertEqual(role["need"], "discretionary")
        self.assertEqual(role["need_basis"], "role rule")

    def test_the_latest_matching_rule_still_wins_within_a_dimension(self) -> None:
        role = effective_financial_role(
            self._entry(),
            [self._rule(1, cost_behavior="variable_routine"), self._rule(2, cost_behavior="irregular")],
        )
        self.assertEqual(role["cost_behavior"], "irregular")
        self.assertEqual(role["cost_behavior_basis"], "role rule")

    def test_a_manual_entry_override_still_outranks_everything(self) -> None:
        role = effective_financial_role(
            self._entry(recurring_confirmed=True, cost_behavior="irregular"),
            [self._rule(1)],
        )
        self.assertEqual(role["cost_behavior"], "irregular")
        self.assertEqual(role["cost_behavior_basis"], "manual")


def _rows() -> list[list[str]]:
    rows: list[list[str]] = [
        ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
         "Seed", "5000.00", "0.00", "SGD", "COMPLETED", "5000.00"],
        ["Salary", "Current", "2026-01-25 09:00:00", "2026-01-25 09:00:01",
         "Acme Payroll", "3000.00", "0.00", "SGD", "COMPLETED", "8000.00"],
    ]
    balance = Decimal("8000.00")
    for month in ("01", "02", "03"):
        for day, merchant, amount in (
            ("05", "Corner Shop", "20.00"),
            ("12", "Corner Shop", "20.00"),
            ("19", "Weird Vendor", "20.00"),
        ):
            balance -= Decimal(amount)
            rows.append([
                "Card Payment", "Current", f"2026-{month}-{day} 10:00:00",
                f"2026-{month}-{day} 10:00:01", merchant, f"-{amount}",
                "0.00", "SGD", "COMPLETED", f"{balance}",
            ])
    balance -= Decimal("1500.00")
    rows.append([
        "Card Payment", "Current", "2026-03-28 10:00:00", "2026-03-28 10:00:01",
        "Weird Vendor", "-1500.00", "0.00", "SGD", "COMPLETED", f"{balance}",
    ])
    return rows


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _revolut_ledger(_rows())
        connection = connect_readonly(self.database)
        try:
            self.token = current_generation(connection)["generation_token"]
        finally:
            connection.close()

    def _page(self, **kw: object) -> dict:
        return transactions_page(
            self.database, generation=self.token, start="2026-01-01", end="2026-03-31",
            currency="SGD", sort="amount", limit=200, **kw,
        )

    def test_direction_filter_splits_inflow_from_outflow(self) -> None:
        inflow = self._page(direction="in")["entries"]
        self.assertTrue(inflow)
        self.assertTrue(all(e["amount"][0] != "-" for e in inflow))
        outflow = self._page(direction="out")["entries"]
        self.assertTrue(all(e["amount"][0] == "-" for e in outflow))
        self.assertEqual(
            self._page()["total"], self._page(direction="in")["total"] + self._page(direction="out")["total"]
        )

    def test_unresolved_only_keeps_rows_with_an_unresolved_dimension(self) -> None:
        page = self._page(unresolved_only=True)
        self.assertTrue(page["entries"])
        for entry in page["entries"]:
            self.assertIn("unresolved", (entry["cost_behavior"], entry["need"]))
        self.assertEqual(page["total"], len(page["entries"]))

    def test_behavior_filter_returns_the_outlier_and_its_basis(self) -> None:
        page = self._page(behavior="irregular")
        self.assertEqual([e["amount"] for e in page["entries"]], ["-1500.00"])
        by_basis = self._page(basis="outlier review")
        self.assertEqual([e["amount"] for e in by_basis["entries"]], ["-1500.00"])

    def test_the_frozen_anchor_list_matches_the_filtered_page_exactly(self) -> None:
        for extra in ({"direction": "out"}, {"unresolved_only": True}, {"behavior": "irregular"}):
            page = self._page(**extra)
            frozen = transaction_anchors(
                self.database, generation=self.token, start="2026-01-01", end="2026-03-31",
                currency="SGD", **extra,
            )
            self.assertEqual(
                sorted(frozen["anchors"]), sorted(e["anchor"] for e in page["entries"]), extra
            )


class RoleRuleLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _revolut_ledger(_rows())
        self.match = canonical_match({"description_contains": "Weird Vendor"})
        self.rule_id = rule_id_for(self.match)

    def _token(self) -> str:
        connection = connect_readonly(self.database)
        try:
            return current_generation(connection)["generation_token"]
        finally:
            connection.close()

    def _set_rule(self) -> None:
        record_decision(
            self.database, request_id="rr-set", intent_hash="rr-set",
            decision_kind="ROLE_RULE_SET", anchors={"rule_id": self.rule_id},
            payload={"match": self.match, "cost_behavior": "irregular",
                     "need": "discretionary", "reason": "vendor is lumpy"},
            policy_version="roles-v1", snapshot="rr", refresh=True,
        )

    def _rule_rows(self) -> list:
        connection = connect_readonly(self.database)
        try:
            return connection.execute("SELECT * FROM role_rules").fetchall()
        finally:
            connection.close()

    def test_a_saved_role_rule_can_be_retired_and_replays_identically(self) -> None:
        self._set_rule()
        self.assertEqual(len(self._rule_rows()), 1)
        record_decision(
            self.database, request_id="rr-retire", intent_hash="rr-retire",
            decision_kind="ROLE_RULE_RETIRE", anchors={"rule_id": self.rule_id},
            payload={"reason": "no longer wanted"},
            policy_version="roles-v1", snapshot="rr", refresh=True,
        )
        self.assertEqual(self._rule_rows(), [])
        refresh_ledger(self.database)
        self.assertEqual(self._rule_rows(), [])

    def test_a_retired_rule_stops_affecting_the_effective_behavior(self) -> None:
        self._set_rule()
        page = transactions_page(
            self.database, generation=self._token(), start="2026-01-01", end="2026-03-31",
            currency="SGD", sort="amount", limit=200, basis="role rule",
        )
        self.assertTrue(page["entries"])
        record_decision(
            self.database, request_id="rr-retire", intent_hash="rr-retire",
            decision_kind="ROLE_RULE_RETIRE", anchors={"rule_id": self.rule_id},
            payload={"reason": "no longer wanted"},
            policy_version="roles-v1", snapshot="rr", refresh=True,
        )
        page = transactions_page(
            self.database, generation=self._token(), start="2026-01-01", end="2026-03-31",
            currency="SGD", sort="amount", limit=200, basis="role rule",
        )
        self.assertEqual(page["entries"], [])


if __name__ == "__main__":
    unittest.main()
