"""Proposing installment chains from the printed instalment block.

The chain-shape cases below mirror the shapes Mari statements produce: a final
instalment that absorbs the rounding remainder, several plans of the same term
running concurrently, and a genuine tie the printed ordinals cannot resolve.
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
from fincl.installments.candidates import (
    _chain_for,
    _label,
    _pair_conversions,
    installment_candidates,
)
from fincl.installments.validation import validate_installment_payload
from fincl.refresh import refresh_ledger
from tests.asgi_client import ASGITestClient
from tests.mari_card_pdf import conversion_statement, mari_card_pdf_bytes

_FIXTURE = Path(__file__).parent / "fixtures" / "mari_card.pdf"
_ORIGIN = "http://127.0.0.1:8765"


def _row(ordinal: int, term: int, amount: str, *, description: str | None = None):
    """One statement line, in the shape `_rows` hands the chain builder."""
    whole, _, fraction = amount.partition(".")
    return {
        "entry_anchor": f"anchor:{ordinal}:{amount}",
        "account_id": "mari-credit-card",
        "currency": "SGD",
        "effective_on": f"2026-{ordinal:02d}-21",
        "date_precision": "DAY",
        "coefficient": int(whole + fraction),
        "scale": len(fraction),
        "description": description,
        "family": "Split Payment",
        "ordinal": ordinal,
        "term": term,
        "statement_id": f"stmt:{ordinal}",
    }


def _conversion(amount: str, *, statement_id: str = "stmt:1", family: str = "Split Payment"):
    """A ``<family> Conversion`` credit, in the shape `_conversions` returns."""
    whole, _, fraction = amount.partition(".")
    return {
        "entry_anchor": f"conversion:{amount}:{statement_id}",
        "account_id": "mari-credit-card",
        "currency": "SGD",
        "effective_on": "2026-01-05",
        "coefficient": int(whole + fraction),
        "scale": len(fraction),
        "description": f"{family} Conversion",
        "family": family,
        "statement_id": statement_id,
    }


def _build(rows):
    """Run the greedy chain builder the way `installment_candidates` does."""
    chains: list[list[dict]] = []
    ambiguous = False
    for row in sorted(rows, key=lambda r: (r["ordinal"], r["effective_on"])):
        index, tied = _chain_for(chains, row)
        ambiguous = ambiguous or tied
        if index is None:
            chains.append([row])
        else:
            chains[index].append(row)
    return chains, ambiguous


class ChainShapeTests(unittest.TestCase):
    def test_final_instalment_absorbing_the_rounding_remainder_stays_one_chain(self) -> None:
        # The shape a real statement prints (illustrative amounts): 10.07 five
        # times, then 10.05 to settle the rounding. Splitting that into two plans
        # would double-count the purchase.
        rows = [_row(i, 6, "10.07") for i in range(1, 6)] + [_row(6, 6, "10.05")]
        chains, ambiguous = _build(rows)
        self.assertEqual(len(chains), 1)
        self.assertEqual(len(chains[0]), 6)
        self.assertFalse(ambiguous)

    def test_a_mid_chain_amount_change_is_not_absorbed(self) -> None:
        # Only the final ordinal may differ. A jump at ordinal 3 of 6 is not a
        # rounding remainder, so it starts a chain the operator has to judge.
        rows = [_row(1, 6, "10.07"), _row(2, 6, "10.07"), _row(3, 6, "10.50")]
        chains, _ = _build(rows)
        self.assertEqual([len(c) for c in chains], [2, 1])

    def test_concurrent_plans_of_the_same_term_are_kept_apart(self) -> None:
        # Two six-month plans running together, each with its own rounding
        # remainder. The amounts are far apart, so the tie-breaker must not fire.
        rows = (
            [_row(i, 6, "10.07") for i in range(1, 6)]
            + [_row(6, 6, "10.05")]
            + [_row(i, 6, "25.07") for i in range(1, 6)]
            + [_row(6, 6, "25.05")]
        )
        chains, ambiguous = _build(rows)
        self.assertFalse(ambiguous)
        self.assertEqual(sorted(len(c) for c in chains), [6, 6])
        for chain in chains:
            self.assertEqual(len({c["coefficient"] // 10 for c in chain}), 1)

    def test_two_chains_sharing_an_amount_are_reported_ambiguous(self) -> None:
        # Identical amount, identical term, overlapping ordinals: the printed
        # evidence genuinely does not say which line belongs to which plan.
        rows = [_row(1, 6, "10.00"), _row(1, 6, "10.00"), _row(2, 6, "10.00")]
        rows[1]["entry_anchor"] = "anchor:1:10.00:b"
        rows[1]["effective_on"] = "2026-02-21"
        _, ambiguous = _build(rows)
        self.assertTrue(ambiguous)

    def test_label_prefers_a_real_merchant_over_the_synthesized_description(self) -> None:
        chain = [
            _row(1, 6, "10.07", description="Split Payment 1/6 Instalment"),
            _row(2, 6, "10.07", description="MERCHANT DELTA SYNTHETIC"),
        ]
        self.assertEqual(_label(chain, "Split Payment", 6), "MERCHANT DELTA SYNTHETIC")

    def test_label_falls_back_to_the_monthly_amount_when_every_line_is_synthesized(self) -> None:
        # Every concurrent chain synthesizes the same text, so the scheme name
        # alone would not tell two plans apart; the amount is what identifies it.
        chain = [_row(i, 6, "10.07", description=f"Split Payment {i}/6 Instalment") for i in (1, 2)]
        self.assertEqual(_label(chain, "Split Payment", 6), "Split Payment · 10.07 × 6")


class ConversionPairingTests(unittest.TestCase):
    """The conversion credit is the principal; these pin when it may be claimed.

    Amounts are illustrative of the shape Mari statements print: 150.40
    converted over six months bills 25.07 five times and 25.05 last, 243.60 over
    twelve bills 20.30.
    """

    def test_a_running_chain_takes_the_conversion_printed_with_its_first_instalment(self) -> None:
        chain = [_row(i, 12, "20.30") for i in range(1, 4)]
        paired = _pair_conversions([(chain, 12)], [_conversion("243.60")])
        self.assertEqual(paired[0]["coefficient"], 24360)

    def test_the_final_instalment_must_be_the_exact_remainder(self) -> None:
        chain = [_row(i, 6, "25.07") for i in range(1, 6)] + [_row(6, 6, "25.05")]
        self.assertIn(0, _pair_conversions([(chain, 6)], [_conversion("150.40")]))
        # A remainder a cent off is a different principal, not this conversion.
        chain[-1] = _row(6, 6, "25.06")
        self.assertEqual(_pair_conversions([(chain, 6)], [_conversion("150.40")]), {})

    def test_a_conversion_on_another_statement_is_not_this_chains_principal(self) -> None:
        chain = [_row(1, 6, "25.07")]
        self.assertEqual(
            _pair_conversions([(chain, 6)], [_conversion("150.40", statement_id="stmt:2")]), {}
        )

    def test_a_chain_first_seen_mid_way_cannot_be_paired(self) -> None:
        # Without its 1/N line there is no statement to find the conversion on.
        chain = [_row(3, 6, "25.07")]
        chain[0]["statement_id"] = "stmt:1"
        self.assertEqual(_pair_conversions([(chain, 6)], [_conversion("150.40")]), {})

    def test_amount_that_does_not_divide_to_the_monthly_is_refused(self) -> None:
        chain = [_row(1, 6, "10.07")]
        self.assertEqual(_pair_conversions([(chain, 6)], [_conversion("150.40")]), {})

    def test_concurrent_conversions_on_one_statement_pair_by_amount(self) -> None:
        small = [_row(1, 6, "10.07")]
        large = [_row(1, 6, "25.07")]
        paired = _pair_conversions(
            [(small, 6), (large, 6)], [_conversion("150.40"), _conversion("60.40")]
        )
        self.assertEqual(paired[0]["coefficient"], 6040)
        self.assertEqual(paired[1]["coefficient"], 15040)

    def test_identical_conversions_leave_both_principals_unknown(self) -> None:
        # Two 150.40 conversions and two 25.07 chains: either pairing reads the
        # same, but a chain cannot claim a credit another chain also fits.
        first, second = [_row(1, 6, "25.07")], [_row(1, 6, "25.07")]
        conversions = [_conversion("150.40"), _conversion("150.40")]
        conversions[1]["entry_anchor"] += ":b"
        self.assertEqual(_pair_conversions([(first, 6), (second, 6)], conversions), {})

    def test_a_conversion_in_another_family_is_not_paired(self) -> None:
        chain = [_row(1, 6, "25.07")]
        self.assertEqual(
            _pair_conversions([(chain, 6)], [_conversion("150.40", family="Instant Checkout")]), {}
        )


class ConversionCandidateTests(unittest.TestCase):
    """End to end: a printed conversion credit becomes a running chain's principal."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "fincl.sqlite3"
        service = DocumentImportService.plaintext_phase2(self.database, self.root / "evidence")
        service.import_mari_card(_FIXTURE)
        second = self.root / "mari_card_aug.pdf"
        second.write_bytes(mari_card_pdf_bytes(conversion_statement()))
        service.import_mari_card(second)
        refresh_ledger(self.database)

    def _by_monthly(self) -> dict[str, dict]:
        return {
            c["occurrences"][0]["amount"]: c
            for c in installment_candidates(self.database)["candidates"]
        }

    def test_the_new_chain_carries_the_conversion_as_its_principal(self) -> None:
        candidate = self._by_monthly()["25.07"]
        self.assertFalse(candidate["complete"])
        self.assertEqual(candidate["known_principal"], {"value": "150.40", "currency": "SGD"})
        self.assertEqual(candidate["principal_source"], "conversion_credit")
        self.assertEqual(candidate["conversion"]["amount"], "150.40")
        self.assertEqual(candidate["conversion"]["effective_on"], "2026-08-12")

    def test_a_chain_whose_conversion_predates_the_ledger_stays_unknown(self) -> None:
        candidate = self._by_monthly()["15.00"]
        self.assertEqual(candidate["observed_count"], 2)
        self.assertIsNone(candidate["known_principal"])
        self.assertIsNone(candidate["principal_source"])
        self.assertIsNone(candidate["conversion"])

    def test_the_conversion_principal_confirms_as_a_valid_plan(self) -> None:
        candidate = self._by_monthly()["25.07"]
        payload = CandidateToPlanPayloadTests._plan_payload(
            candidate, known_principal=candidate["known_principal"]["value"]
        )
        validate_installment_payload("INSTALLMENT_PLAN_SET", {"plan": str(uuid.uuid4())}, payload)


class CandidateReadTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(
            self.database, self.root / "evidence"
        ).import_mari_card(_FIXTURE)
        refresh_ledger(self.database)
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            self.account_id = connection.execute(
                "SELECT account_id FROM statements LIMIT 1"
            ).fetchone()[0]
            self.installment_anchor = connection.execute(
                "SELECT e.natural_anchor FROM ledger_entries e "
                "JOIN document_records r ON r.id = e.document_record_id "
                "WHERE r.raw_fields_json LIKE '%installment%'"
            ).fetchone()[0]
        finally:
            connection.close()

    def test_the_statement_instalment_line_is_proposed_as_a_candidate(self) -> None:
        result = installment_candidates(self.database)
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["term_months"], 6)
        self.assertEqual(candidate["family"], "Split Payment")
        self.assertEqual(candidate["card_account_id"], self.account_id)
        self.assertEqual(
            [o["entry_anchor"] for o in candidate["occurrences"]], [self.installment_anchor]
        )

    def test_a_partial_chain_reports_no_known_principal(self) -> None:
        # One occurrence of six. `amount x term` would be an invention, so the
        # principal stays null and only the observed total is claimed.
        candidate = installment_candidates(self.database)["candidates"][0]
        self.assertFalse(candidate["complete"])
        self.assertIsNone(candidate["known_principal"])
        self.assertEqual(candidate["observed_total"]["value"], "15.00")

    def test_an_occurrence_already_claimed_by_a_plan_stops_being_proposed(self) -> None:
        payload = {
            "card_account_id": self.account_id,
            "currency": "SGD",
            "plan_reference": None,
            "merchant_label": "Confirmed already",
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
            "reason": "Confirming the chain from the proposed candidate",
        }
        anchors = {"plan": str(uuid.uuid4())}
        record_installment_decision(
            self.database,
            request_id=str(uuid.uuid4()),
            intent_hash=hashlib.sha256(b"claim").hexdigest(),
            decision_kind="INSTALLMENT_PLAN_SET",
            anchors=anchors,
            payload=payload,
        )
        result = installment_candidates(self.database)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["claimed_occurrence_count"], 1)

    def _client(self) -> ASGITestClient:
        application = create_app(
            self.database, port=8765, bootstrap_secret="test-secret",
            evidence_dir=self.root / "evidence",
        )
        client = ASGITestClient(application, base_url=_ORIGIN)
        self.addCleanup(client.close)
        client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": _ORIGIN, "content-type": "application/json"},
        )
        return client

    def test_the_route_serves_candidates_and_refuses_a_stale_generation(self) -> None:
        client = self._client()
        response = client.get("/api/v1/installments/candidates")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["candidates"]), 1)
        stale = client.get("/api/v1/installments/candidates?generation=sha256:nope")
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_candidates_is_not_read_as_a_plan_id(self) -> None:
        # `/installments/{plan_id}` is registered after the literal path; if that
        # order ever flips, this asks for plan "candidates" and 404s.
        response = self._client().get("/api/v1/installments/candidates")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("candidates", response.json())




class CandidateToPlanPayloadTests(unittest.TestCase):
    """The seam the two test suites otherwise leave open.

    The browser tests mock `/preview` and `/commands`, so they prove the UI sends
    what it means to send but not that the backend accepts it. These build the
    exact payloads `features/installments/api.ts` constructs and run them through
    the real validator, so a renamed or dropped field fails here rather than at
    the first click on a real ledger.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(
            self.database, self.root / "evidence"
        ).import_mari_card(_FIXTURE)
        refresh_ledger(self.database)
        self.candidate = installment_candidates(self.database)["candidates"][0]

    @staticmethod
    def _plan_payload(candidate, *, known_principal):
        """Mirrors `confirmPlan` in features/installments/api.ts."""
        return {
            "card_account_id": candidate["card_account_id"],
            "currency": candidate["currency"],
            "plan_reference": None,
            "merchant_label": candidate["merchant_label"],
            "term_months": candidate["term_months"],
            "known_principal": known_principal,
            "financing_fee": None,
            "opened_on": candidate["opened_on"],
            "status": "CONFIRMED",
            "occurrences": [
                {
                    "occurrence_id": str(uuid.uuid4()),
                    "occurrence_index": o["occurrence_index"],
                    "occurrence_count": o["occurrence_count"],
                    "entry_anchor": o["entry_anchor"],
                    "effective_on": o["effective_on"],
                    "amount": o["amount"],
                    "date_precision": o["date_precision"],
                }
                for o in candidate["occurrences"]
            ],
            "schedule": [],
            "reason": "Confirmed from the proposed chain",
        }

    def test_the_confirm_payload_a_candidate_produces_is_valid(self) -> None:
        validate_installment_payload(
            "INSTALLMENT_PLAN_SET",
            {"plan": str(uuid.uuid4())},
            self._plan_payload(self.candidate, known_principal=None),
        )

    def test_a_complete_chain_sends_its_observed_total_as_the_principal(self) -> None:
        complete = dict(self.candidate, known_principal={"value": "15.00", "currency": "SGD"})
        validate_installment_payload(
            "INSTALLMENT_PLAN_SET",
            {"plan": str(uuid.uuid4())},
            self._plan_payload(complete, known_principal="15.00"),
        )

    def test_the_settlement_payload_the_form_builds_is_valid(self) -> None:
        validate_installment_payload(
            "INSTALLMENT_SETTLEMENT_SET",
            {"plan": str(uuid.uuid4()), "settlement": str(uuid.uuid4())},
            {
                "settled_on": "2026-08-21",
                "source_entry_anchor": self.candidate["occurrences"][0]["entry_anchor"],
                "principal_amount": "40.00",
                "fee_amount": "0",
                "waiver_amount": "0",
                "reason": "Paid the remaining balance off in one transfer",
            },
        )

    def test_the_allocation_and_retire_payloads_the_view_builds_are_valid(self) -> None:
        plan = {"plan": str(uuid.uuid4())}
        validate_installment_payload(
            "INSTALLMENT_ALLOCATION_SET",
            plan,
            {
                "allocations": [
                    {
                        "entry_anchor": self.candidate["occurrences"][0]["entry_anchor"],
                        "component_role": "principal",
                        "amount": "15.00",
                    }
                ],
                "reason": "The original purchase this plan splits",
            },
        )
        validate_installment_payload("INSTALLMENT_PLAN_RETIRE", plan, {"reason": "Wrong chain"})

    def test_a_schedule_without_a_principal_is_still_refused(self) -> None:
        # The UI always sends an empty schedule for exactly this reason; if that
        # ever changes silently, the validator is what catches it.
        payload = self._plan_payload(self.candidate, known_principal=None)
        payload["schedule"] = [{"due_on": "2026-08-21", "principal": "2.50", "fee": "0"}]
        with self.assertRaises(FinCLError):
            validate_installment_payload(
                "INSTALLMENT_PLAN_SET", {"plan": str(uuid.uuid4())}, payload
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
