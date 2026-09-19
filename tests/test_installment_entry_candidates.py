"""Source-transaction picker for purchase attribution and early settlement.

Both purposes offer outflows in the plan's currency that no other domain has
already claimed. Purchase attribution is scoped to the plan's own card, because
that is where the charge it splits sat; settlement is not, because the money that
pays a card plan off usually comes from a bank account.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from fincl.api import create_app
from fincl.application import record_installment_decision
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.installments.queries import entry_candidates
from fincl.refresh import refresh_ledger
from tests.asgi_client import ASGITestClient

_FIXTURE = Path(__file__).parent / "fixtures" / "mari_card.pdf"
_ORIGIN = "http://127.0.0.1:8765"


class EntryCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "fincl.sqlite3"
        self.evidence = self.root / "evidence"
        DocumentImportService.plaintext_phase2(
            self.database, self.evidence
        ).import_mari_card(_FIXTURE)
        refresh_ledger(self.database)
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            self.account_id = connection.execute(
                "SELECT account_id FROM statements LIMIT 1"
            ).fetchone()[0]
            self.anchors = {
                anchor.rsplit(":", 1)[1]: anchor
                for (anchor,) in connection.execute(
                    "SELECT natural_anchor FROM ledger_entries"
                )
            }
        finally:
            connection.close()
        self.installment_anchor = self.anchors["5"]  # -15.00 split payment row
        self.purchase_anchor = self.anchors["2"]  # -20.00 card purchase
        self.plan_id = self._plan()

    def _plan(self) -> str:
        plan_id = str(uuid.uuid4())
        record_installment_decision(
            self.database,
            request_id=str(uuid.uuid4()),
            intent_hash=hashlib.sha256(plan_id.encode()).hexdigest(),
            decision_kind="INSTALLMENT_PLAN_SET",
            anchors={"plan": plan_id},
            payload={
                "card_account_id": self.account_id,
                "currency": "SGD",
                "plan_reference": None,
                "merchant_label": "Split Payment Merchant",
                "term_months": 6,
                "known_principal": None,
                "financing_fee": None,
                "opened_on": "2026-07-21",
                "status": "CONFIRMED",
                "occurrences": [
                    {
                        "occurrence_id": str(uuid.uuid4()),
                        "occurrence_index": 3,
                        "occurrence_count": 6,
                        "entry_anchor": self.installment_anchor,
                        "effective_on": "2026-07-21",
                        "amount": "15.00",
                        "date_precision": "DAY",
                    }
                ],
                "schedule": [],
                "reason": "Confirmed from the proposed chain",
            },
        )
        return plan_id

    def _allocate(self, anchor: str, amount: str) -> None:
        record_installment_decision(
            self.database,
            request_id=str(uuid.uuid4()),
            intent_hash=hashlib.sha256(f"alloc{anchor}{amount}".encode()).hexdigest(),
            decision_kind="INSTALLMENT_ALLOCATION_SET",
            anchors={"plan": self.plan_id},
            payload={
                "allocations": [
                    {"entry_anchor": anchor, "component_role": "principal", "amount": amount}
                ],
                "reason": "The original purchase this plan splits",
            },
        )

    def test_purchase_candidates_are_outflows_on_the_plans_own_card(self) -> None:
        result = entry_candidates(self.database, plan_id=self.plan_id, purpose="purchase")
        self.assertEqual(result["purpose"], "purchase")
        self.assertEqual(result["currency"], "SGD")
        self.assertTrue(result["candidates"])
        for candidate in result["candidates"]:
            self.assertEqual(candidate["account_id"], self.account_id)
        anchors = {c["entry_anchor"] for c in result["candidates"]}
        self.assertIn(self.purchase_anchor, anchors)

    def test_every_candidate_reports_what_is_still_unclaimed(self) -> None:
        result = entry_candidates(self.database, plan_id=self.plan_id, purpose="purchase")
        row = next(c for c in result["candidates"] if c["entry_anchor"] == self.purchase_anchor)
        self.assertEqual(row["amount"], "20.00")
        self.assertEqual(row["open_amount"], "20.00")

    def test_an_entry_already_claimed_in_full_stops_being_offered(self) -> None:
        # The shared component registry is the single source of truth here; a
        # source spent elsewhere must not be offered again as if it were free.
        self._allocate(self.purchase_anchor, "20.00")
        result = entry_candidates(self.database, plan_id=self.plan_id, purpose="purchase")
        anchors = {c["entry_anchor"] for c in result["candidates"]}
        self.assertNotIn(self.purchase_anchor, anchors)

    def test_a_partly_claimed_entry_is_still_offered_for_its_remainder(self) -> None:
        self._allocate(self.purchase_anchor, "12.00")
        result = entry_candidates(self.database, plan_id=self.plan_id, purpose="purchase")
        row = next(c for c in result["candidates"] if c["entry_anchor"] == self.purchase_anchor)
        self.assertEqual(row["open_amount"], "8.00")
        self.assertEqual(row["amount"], "20.00")

    def test_anchor_narrows_to_one_transaction_and_keeps_every_other_rule(self) -> None:
        """The classification editor's hand-off asks about exactly one row."""
        result = entry_candidates(self.database, plan_id=self.plan_id, purpose="purchase",
                                  anchor=self.purchase_anchor)
        self.assertEqual([c["entry_anchor"] for c in result["candidates"]], [self.purchase_anchor])
        # An instalment line is never a purchase, however it is asked for.
        withheld = entry_candidates(self.database, plan_id=self.plan_id, purpose="purchase",
                                    anchor=self.installment_anchor)
        self.assertEqual(withheld["candidates"], [])

    def test_settlement_offers_other_accounts_but_purchase_does_not(self) -> None:
        # A card plan is usually settled by a transfer from a bank account, so
        # scoping settlement to the card would hide the ordinary case. The
        # purchase it splits, by contrast, was a charge on the card itself.
        DocumentImportService.plaintext_phase2(
            self.database, self.evidence
        ).import_maribank(Path(__file__).parent / "fixtures" / "maribank_savings.pdf")
        refresh_ledger(self.database)

        purchase = entry_candidates(self.database, plan_id=self.plan_id, purpose="purchase")
        settlement = entry_candidates(self.database, plan_id=self.plan_id, purpose="settlement")

        self.assertEqual(
            {c["account_id"] for c in purchase["candidates"]}, {self.account_id}
        )
        off_card = {
            c["account_id"] for c in settlement["candidates"] if c["account_id"] != self.account_id
        }
        self.assertTrue(off_card, "settlement should reach beyond the plan's own card")

    def test_search_narrows_by_description(self) -> None:
        everything = entry_candidates(self.database, plan_id=self.plan_id, purpose="purchase")
        target = next(c for c in everything["candidates"] if c["description"])
        needle = str(target["description"])[:6]
        narrowed = entry_candidates(
            self.database, plan_id=self.plan_id, purpose="purchase", search=needle
        )
        self.assertTrue(narrowed["candidates"])
        for candidate in narrowed["candidates"]:
            self.assertIn(needle.lower(), str(candidate["description"]).lower())

    def test_an_unknown_plan_and_a_bad_purpose_are_both_refused(self) -> None:
        with self.assertRaises(FinCLError):
            entry_candidates(self.database, plan_id=str(uuid.uuid4()), purpose="purchase")
        with self.assertRaises(FinCLError):
            entry_candidates(self.database, plan_id=self.plan_id, purpose="anything-else")

    def test_a_corrupt_cursor_is_refused_rather_than_silently_ignored(self) -> None:
        with self.assertRaises(FinCLError):
            entry_candidates(self.database, plan_id=self.plan_id, cursor="not-a-cursor")

    def _client(self) -> ASGITestClient:
        application = create_app(
            self.database, port=8765, bootstrap_secret="test-secret",
            evidence_dir=self.evidence,
        )
        client = ASGITestClient(application, base_url=_ORIGIN)
        self.addCleanup(client.close)
        client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": _ORIGIN, "content-type": "application/json"},
        )
        return client

    def test_the_route_serves_both_purposes(self) -> None:
        client = self._client()
        for purpose in ("purchase", "settlement"):
            response = client.get(
                f"/api/v1/installments/entry-candidates?plan={self.plan_id}&purpose={purpose}"
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["purpose"], purpose)

    def test_entry_candidates_is_not_read_as_a_plan_id(self) -> None:
        # Registered before `/installments/{plan_id}`; if that order flips this
        # asks for a plan literally named "entry-candidates".
        response = self._client().get(
            f"/api/v1/installments/entry-candidates?plan={self.plan_id}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("candidates", response.json())

    def test_the_single_transaction_read_serves_the_list_shape(self) -> None:
        from urllib.parse import quote
        client = self._client()
        response = client.get(f"/api/v1/transactions/entry?anchor={quote(self.purchase_anchor)}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["entry"]["anchor"], self.purchase_anchor)
        self.assertIn("debt_allocation", body["entry"])
        missing = client.get("/api/v1/transactions/entry?anchor=no-such-anchor")
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_the_route_refuses_a_stale_generation(self) -> None:
        response = self._client().get(
            f"/api/v1/installments/entry-candidates?plan={self.plan_id}&generation=sha256:nope"
        )
        self.assertEqual(response.status_code, 409, response.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class OccurrenceExclusionTests(EntryCandidateTests):
    """An instalment repays a purchase; it is never the purchase or a settlement.

    The claim registry tracks purchase allocations and settlements but not
    occurrences, so an occurrence reads as fully unclaimed. The backend does not
    refuse it either, which would let one statement line count as an instalment
    and again as the cost it repays -- so the picker is what withholds it.
    """

    def test_an_occurrence_is_never_offered_as_a_purchase(self) -> None:
        result = entry_candidates(self.database, plan_id=self.plan_id, purpose="purchase")
        anchors = {c["entry_anchor"] for c in result["candidates"]}
        self.assertNotIn(self.installment_anchor, anchors)

    def test_an_occurrence_is_never_offered_as_a_settlement_source(self) -> None:
        result = entry_candidates(self.database, plan_id=self.plan_id, purpose="settlement")
        anchors = {c["entry_anchor"] for c in result["candidates"]}
        self.assertNotIn(self.installment_anchor, anchors)

    def test_a_printed_instalment_of_an_unconfirmed_chain_is_also_withheld(self) -> None:
        # The fixture's instalment line is printed as one whether or not a plan
        # claims it, and the write path would accept it as a purchase.
        import sqlite3

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            printed = {
                str(row["natural_anchor"])
                for row in connection.execute(
                    "SELECT e.natural_anchor FROM ledger_entries e "
                    "JOIN document_records r ON r.id = e.document_record_id "
                    "WHERE r.raw_fields_json LIKE '%installment%'"
                )
            }
        finally:
            connection.close()
        self.assertTrue(printed)
        offered = {
            c["entry_anchor"]
            for c in entry_candidates(
                self.database, plan_id=self.plan_id, purpose="purchase"
            )["candidates"]
        }
        self.assertEqual(printed & offered, set())

    def test_the_ordinary_purchase_is_still_offered(self) -> None:
        # The exclusion must be narrow: only occurrences, not every card charge.
        result = entry_candidates(self.database, plan_id=self.plan_id, purpose="purchase")
        anchors = {c["entry_anchor"] for c in result["candidates"]}
        self.assertIn(self.purchase_anchor, anchors)
