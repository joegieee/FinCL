"""The "will I have enough?" answer and the reserve policy behind it.

Four claims are proven here.

*A missing answer is not a yes.* A currency whose forecast could not be built
reports its reasons and no verdict; it must never read as having enough.

*Running out and dipping into the reserve are different answers.* Each has its
own verdict and its own amount, and the reserve one is not reported as a
shortfall in cash.

*A shortfall names what caused it*, attributed from the provenance the forecast
already carries — never inferred from a merchant label, because a card purchase
and a bank purchase read identically by name.

*The reserve month count is a decision, and the amount it multiplies is not.*
The policy round-trips through the journal, the default is distinguishable from
the same number chosen deliberately, and no payload anywhere accepts an amount.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.planning.sufficiency import (
    VERDICT_BELOW_RESERVE,
    VERDICT_ENOUGH,
    VERDICT_SHORT,
    VERDICT_UNAVAILABLE,
    _case,
    _driver_origin,
    _drivers,
)
from fincl.refresh import refresh_ledger
from fincl.reserve import (
    DEFAULT_RESERVE_MONTHS,
    RESERVE_POLICY_VERSION,
    reserve_policy,
    validate_reserve_payload,
)
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes


def _event(
    *,
    label: str,
    on: str,
    amount: str,
    origin: str | None = None,
    kind: str | None = None,
    direction: str = "outflow",
    source_id: str = "src",
) -> dict:
    provenance: dict = {}
    if origin:
        provenance["origin"] = origin
    if kind:
        provenance["kind"] = kind
    return {
        "event_id": f"{label}:{on}", "source_id": source_id, "column": "lifestyle_costs",
        "direction": direction, "on": on, "amount": amount, "label": label,
        "conditional": False, "provenance": provenance,
    }


def _scenario(**over) -> dict:
    scenario = {
        "available": True,
        "limitations": [],
        "days": [{"date": "2026-02-01", "low_point": "500.00"}],
        "first_zero": {"occurs": False},
        "first_reserve_breach": {"available": True, "occurs": False, "reserve_target": "900.00"},
        "reserve_funding_gaps": {"available": True, "largest_gap": "0.00"},
        "events": {"items": []},
        "horizon": {"end": "2026-07-31"},
    }
    scenario.update(over)
    return scenario


class VerdictTests(unittest.TestCase):
    def test_a_solvent_path_above_the_reserve_answers_yes(self) -> None:
        case = _case(_scenario(), scale=2)

        self.assertEqual(case["verdict"], VERDICT_ENOUGH)
        self.assertIsNone(case["shortfall"])
        self.assertEqual(case["drivers"], [])

    def test_an_unbuildable_case_reports_its_reasons_and_no_verdict(self) -> None:
        """A missing answer must never be readable as "you have enough"."""
        case = _case(
            _scenario(available=False, limitations=["no reconciled liquid balance"]),
            scale=2,
        )

        self.assertEqual(case["verdict"], VERDICT_UNAVAILABLE)
        self.assertNotEqual(case["verdict"], VERDICT_ENOUGH)
        self.assertEqual(case["reasons"], ["no reconciled liquid balance"])

    def test_running_out_of_cash_reports_the_deepest_deficit_not_the_first_touch(self) -> None:
        case = _case(
            _scenario(
                first_zero={"occurs": True, "on": "2026-12-18"},
                days=[
                    {"date": "2026-12-18", "low_point": "-100.00"},
                    {"date": "2026-12-28", "low_point": "-1240.00"},
                    {"date": "2027-01-05", "low_point": "-400.00"},
                ],
            ),
            scale=2,
        )

        self.assertEqual(case["verdict"], VERDICT_SHORT)
        self.assertEqual(case["first_short_on"], "2026-12-18")
        # How short you actually get, which is the figure that answers
        # "how much would I need".
        self.assertEqual(case["shortfall"], "1240.00")
        self.assertEqual(case["shortfall_on"], "2026-12-28")

    def test_dipping_into_the_reserve_is_its_own_verdict_and_amount(self) -> None:
        case = _case(
            _scenario(
                first_reserve_breach={
                    "available": True, "occurs": True, "on": "2026-11-20",
                    "reserve_target": "3600.00",
                },
                reserve_funding_gaps={"available": True, "largest_gap": "740.00"},
            ),
            scale=2,
        )

        self.assertEqual(case["verdict"], VERDICT_BELOW_RESERVE)
        self.assertEqual(case["shortfall"], "740.00")
        self.assertEqual(case["reserve_target"], "3600.00")
        self.assertIn("below your reserve", case["headline"])

    def test_a_reserve_breach_never_outranks_running_out_of_cash(self) -> None:
        case = _case(
            _scenario(
                first_zero={"occurs": True, "on": "2026-12-18"},
                days=[{"date": "2026-12-18", "low_point": "-50.00"}],
                first_reserve_breach={
                    "available": True, "occurs": True, "on": "2026-10-01",
                    "reserve_target": "3600.00",
                },
                reserve_funding_gaps={"available": True, "largest_gap": "3650.00"},
            ),
            scale=2,
        )

        self.assertEqual(case["verdict"], VERDICT_SHORT)
        self.assertEqual(case["shortfall"], "50.00")


class DriverTests(unittest.TestCase):
    def _events(self) -> list[dict]:
        return [
            _event(label="BNPL plan 3/6", on="2026-12-05", amount="620.00",
                   origin="installment_plan"),
            _event(label="Loan to Sam", on="2026-12-10", amount="400.00",
                   origin="personal_debt"),
            _event(label="Annual insurance", on="2026-12-12", amount="480.00",
                   source_id="fund:insurance"),
            _event(label="Netflix", on="2026-12-15", amount="20.00",
                   origin="recurring_commitment"),
            _event(label="Routine residual", on="2026-12-15", amount="90.00",
                   kind="unscheduled_residual_allocation"),
            _event(label="Salary", on="2026-12-01", amount="4000.00",
                   direction="inflow", origin="recurring_commitment"),
            _event(label="Rent", on="2026-11-01", amount="1800.00",
                   origin="recurring_commitment"),
        ]

    def test_outflows_are_grouped_by_the_family_that_committed_them(self) -> None:
        rows = {row["origin"]: row for row in _drivers(self._events(), month="2026-12", scale=2)}

        self.assertEqual(rows["installment_plan"]["amount"], "620.00")
        self.assertEqual(rows["personal_debt"]["amount"], "400.00")
        self.assertEqual(rows["sinking_fund"]["amount"], "480.00")
        self.assertEqual(rows["recurring_commitment"]["amount"], "20.00")
        self.assertEqual(rows["baseline_spending"]["amount"], "90.00")

    def test_income_and_other_months_are_left_out(self) -> None:
        rows = _drivers(self._events(), month="2026-12", scale=2)
        total = sum(Decimal(row["amount"]) for row in rows)

        self.assertEqual(total, Decimal("1610.00"))

    def test_the_largest_driver_is_listed_first(self) -> None:
        rows = _drivers(self._events(), month="2026-12", scale=2)

        self.assertEqual(rows[0]["origin"], "installment_plan")
        self.assertEqual(rows[-1]["origin"], "recurring_commitment")

    def test_each_driver_names_the_obligations_behind_it(self) -> None:
        rows = {row["origin"]: row for row in _drivers(self._events(), month="2026-12", scale=2)}

        self.assertEqual(
            [item["label"] for item in rows["installment_plan"]["items"]], ["BNPL plan 3/6"]
        )
        # Residual spending is one slice per day; listing each would bury the
        # obligations a reader can act on.
        self.assertEqual(rows["baseline_spending"]["items"], [])

    def test_a_family_is_read_from_provenance_never_from_the_label(self) -> None:
        """A card purchase and a bank purchase read identically by name."""
        labelled = _event(label="Personal debt repayment to Sam", on="2026-12-01",
                          amount="100.00")

        self.assertEqual(_driver_origin(labelled), "other")


class ReservePolicyTests(unittest.TestCase):
    def _ledger(self) -> Path:
        root = Path(tempfile.mkdtemp())
        database = root / "ledger.sqlite3"
        source = root / "revolut.csv"
        source.write_bytes(csv_bytes([
            ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
             "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
        ]))
        DocumentImportService.plaintext_phase2(
            database, root / "evidence"
        ).import_revolut_pdf(source)
        refresh_ledger(database)
        return database

    def _policy(self, database: Path):
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            return reserve_policy(connection)
        finally:
            connection.close()

    def _set(self, database: Path, months: str) -> None:
        append_decision(
            default_journal_path(database),
            decision_kind="RESERVE_POLICY_SET",
            anchors={"policy": "reserve"},
            payload={"months": months, "reason": "six months suits my contract work"},
            policy_version=RESERVE_POLICY_VERSION,
            snapshot="reserve policy test",
        )
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            replay_journal(connection)
            connection.commit()
        finally:
            connection.close()

    def test_an_unset_policy_reads_as_the_framework_default(self) -> None:
        policy = self._policy(self._ledger())

        self.assertEqual(policy.months, DEFAULT_RESERVE_MONTHS)
        self.assertTrue(policy.is_default)
        self.assertIsNone(policy.decided_at)

    def test_a_chosen_policy_round_trips_through_the_journal(self) -> None:
        database = self._ledger()
        self._set(database, "6")

        policy = self._policy(database)

        self.assertEqual(policy.months, Decimal("6"))
        self.assertFalse(policy.is_default)
        self.assertIsNotNone(policy.decided_at)

    def test_choosing_the_default_number_is_still_a_decision(self) -> None:
        """Reviewing the default is not choosing a policy; the two must differ."""
        database = self._ledger()
        self._set(database, "3")

        policy = self._policy(database)

        self.assertEqual(policy.months, DEFAULT_RESERVE_MONTHS)
        self.assertFalse(policy.is_default)

    def test_the_latest_decision_wins(self) -> None:
        database = self._ledger()
        self._set(database, "6")
        self._set(database, "4.5")

        self.assertEqual(self._policy(database).months, Decimal("4.5"))


class ReserveValidationTests(unittest.TestCase):
    def _validate(self, payload: dict, anchors: dict | None = None) -> None:
        validate_reserve_payload(
            "RESERVE_POLICY_SET", anchors or {"policy": "reserve"}, payload
        )

    def test_a_well_formed_policy_validates(self) -> None:
        self._validate({"months": "6", "reason": "contract work"})

    def test_an_amount_cannot_be_smuggled_into_the_policy(self) -> None:
        """The month count is the operator's; the amount is always evidence."""
        with self.assertRaises(FinCLError):
            self._validate({"months": "6", "amount": "18000.00", "reason": "x"})

    def test_zero_and_negative_month_counts_are_refused(self) -> None:
        for months in ("0", "-1"):
            with self.subTest(months=months), self.assertRaises(FinCLError):
                self._validate({"months": months, "reason": "x"})

    def test_an_absurd_month_count_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._validate({"months": "60", "reason": "x"})

    def test_a_non_numeric_month_count_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._validate({"months": "a few", "reason": "x"})

    def test_a_second_policy_anchor_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._validate({"months": "6", "reason": "x"}, {"policy": str(uuid.uuid4())})

    def test_a_reason_is_required(self) -> None:
        with self.assertRaises(FinCLError):
            self._validate({"months": "6", "reason": "  "})


if __name__ == "__main__":
    unittest.main()
