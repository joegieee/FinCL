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
from fractions import Fraction
from pathlib import Path

from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.planning.sufficiency import (
    COUNTED_AS_IS,
    COUNTED_IGNORED,
    COUNTED_NOT,
    COUNTED_PROJECTED,
    VERDICT_BELOW_RESERVE,
    VERDICT_ENOUGH,
    VERDICT_SHORT,
    VERDICT_UNAVAILABLE,
    _case,
    _driver_origin,
    _drivers,
    combined_answer,
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

    def test_a_yes_still_names_what_the_horizon_already_pays_for(self) -> None:
        """A debt recorded today must be visibly counted even when nothing breaches."""
        case = _case(
            _scenario(events={"items": [
                _event(label="Loan from Alex", on="2026-11-15", amount="2000.00",
                       origin="personal_debt"),
                _event(label="Annual invoice", on="2027-02-15", amount="3000.00",
                       origin="personal_debt"),
                _event(label="Salary", on="2026-11-01", amount="4000.00",
                       direction="inflow", origin="recurring_commitment"),
            ]}),
            scale=2,
        )

        self.assertEqual(case["verdict"], VERDICT_ENOUGH)
        self.assertEqual(case["drivers"], [])
        [debts] = case["accounted_for"]
        self.assertEqual(debts["origin"], "personal_debt")
        self.assertEqual(debts["amount"], "5000.00")
        self.assertEqual([item["label"] for item in debts["items"]], ["Loan from Alex", "Annual invoice"])

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


def _days(*rows: tuple[str, str]) -> list[dict]:
    return [{"date": on, "low_point": low, "closing": low} for on, low in rows]


def _pocket(days: list[dict], *, events: list | None = None, available: bool = True,
            balance: str = "100.00", reserve: str | None = None, cautious: list[dict] | None = None,
            reason: str | None = None) -> dict:
    case = {"days": days, "events": {"items": events or []}, "horizon": {"end": "2026-03-31"}}
    return {
        "available": available, "reason": reason, "scale": 2, "starting_balance": balance,
        "reserve_readiness": {"available": reserve is not None, "target": reserve},
        "base": case, "conservative": {**case, "days": cautious if cautious is not None else days},
    }


def _timeline(by_currency: dict, action_items: list | None = None) -> dict:
    return {"by_currency": by_currency, "action_items": action_items or [],
            "horizon": {"supported_cash_cutoff": "2026-01-31", "end": "2026-03-31"}}


class CombinedAnswerTests(unittest.TestCase):
    """All pockets are one pool for one purpose: one answer, in one currency."""

    def _answer(self, timeline: dict, rates: dict, **kw) -> dict:
        return combined_answer(timeline, rates=rates, reporting_currency="SGD", scale=2, **kw)

    def test_a_short_pocket_covered_by_another_is_one_yes(self) -> None:
        """SGD alone runs out; USD income more than covers it on the same days."""
        answer = self._answer(_timeline({
            "SGD": _pocket(_days(("2026-02-01", "50.00"), ("2026-02-02", "-200.00"))),
            "USD": _pocket(_days(("2026-02-01", "300.00"), ("2026-02-02", "300.00"))),
        }), {"SGD": Fraction(1), "USD": Fraction(13, 10)})

        self.assertEqual(answer["verdict"], VERDICT_ENOUGH)
        self.assertTrue(answer["complete"])
        self.assertEqual(answer["base"]["lowest"], "190.00")  # -200 + 300 x 1.3
        self.assertEqual(answer["base"]["lowest_on"], "2026-02-02")

    def test_lows_are_summed_on_the_same_day_never_across_days(self) -> None:
        """Each pocket's worst day differs; adding the two worsts would invent a breach."""
        answer = self._answer(_timeline({
            "SGD": _pocket(_days(("2026-02-01", "-100.00"), ("2026-02-02", "500.00"))),
            "USD": _pocket(_days(("2026-02-01", "500.00"), ("2026-02-02", "-100.00"))),
        }), {"SGD": Fraction(1), "USD": Fraction(1)})

        self.assertEqual(answer["verdict"], VERDICT_ENOUGH)
        self.assertEqual(answer["base"]["lowest"], "400.00")

    def test_running_out_states_the_first_day_and_the_deepest_gap(self) -> None:
        answer = self._answer(_timeline({
            "SGD": _pocket(_days(("2026-02-01", "-10.00"), ("2026-02-02", "-80.00"),
                                 ("2026-02-03", "-20.00"))),
        }), {"SGD": Fraction(1)})

        self.assertEqual(answer["verdict"], VERDICT_SHORT)
        self.assertEqual(answer["base"]["first_short_on"], "2026-02-01")
        self.assertEqual(answer["base"]["shortfall"], "80.00")
        self.assertEqual(answer["base"]["shortfall_on"], "2026-02-02")

    def test_the_reserve_is_stated_only_when_every_pocket_has_one(self) -> None:
        days = _days(("2026-02-01", "100.00"))
        partial = self._answer(_timeline({
            "SGD": _pocket(days, reserve="500.00"), "USD": _pocket(days),
        }), {"SGD": Fraction(1), "USD": Fraction(1)})
        self.assertFalse(partial["reserve"]["available"])
        self.assertEqual(partial["verdict"], VERDICT_ENOUGH)

        full = self._answer(_timeline({
            "SGD": _pocket(days, reserve="500.00"), "USD": _pocket(days, reserve="100.00"),
        }), {"SGD": Fraction(1), "USD": Fraction(1)})
        self.assertEqual(full["reserve"]["target"], "600.00")
        self.assertEqual(full["verdict"], VERDICT_BELOW_RESERVE)
        self.assertEqual(full["base"]["shortfall"], "400.00")

    def test_a_pocket_with_no_rate_is_named_and_the_answer_is_incomplete(self) -> None:
        answer = self._answer(_timeline({
            "SGD": _pocket(_days(("2026-02-01", "100.00"))),
            "THB": _pocket(_days(("2026-02-01", "250.00")), balance="250.00"),
        }), {"SGD": Fraction(1), "THB": None})

        self.assertFalse(answer["complete"])
        thb = next(p for p in answer["pockets"] if p["currency"] == "THB")
        self.assertEqual(thb["counted"], COUNTED_NOT)
        self.assertIn("pocket-THB", [todo["key"] for todo in answer["todos"]])
        self.assertEqual(answer["starting_total"], "100.00")

    def test_a_quiet_pocket_under_one_unit_is_ignored_even_without_a_rate(self) -> None:
        """THB 0.50 with nothing scheduled: too small to matter, so it neither
        blocks the answer nor becomes a to-do."""
        answer = self._answer(_timeline({
            "SGD": _pocket(_days(("2026-02-01", "100.00"))),
            "THB": _pocket(_days(("2026-02-01", "0.50")), available=False, balance="0.50",
                           reason="no spending history"),
        }), {"SGD": Fraction(1), "THB": None})

        self.assertTrue(answer["complete"])
        thb = next(p for p in answer["pockets"] if p["currency"] == "THB")
        self.assertEqual(thb["counted"], COUNTED_IGNORED)
        self.assertNotIn("pocket-THB", [todo["key"] for todo in answer["todos"]])
        self.assertEqual(answer["base"]["lowest"], "100.00")

    def test_a_small_pocket_with_scheduled_money_is_not_ignored(self) -> None:
        answer = self._answer(_timeline({
            "SGD": _pocket(_days(("2026-02-01", "100.00"))),
            "THB": _pocket(_days(("2026-02-01", "0.50")), available=False, balance="0.50",
                           events=[_event(label="Fee", on="2026-02-01", amount="500.00")]),
        }), {"SGD": Fraction(1), "THB": None})

        self.assertFalse(answer["complete"])

    def test_a_quiet_pocket_with_no_history_is_held_as_it_stands(self) -> None:
        """Nothing scheduled and nothing to project: its balance is simply there."""
        answer = self._answer(_timeline({
            "SGD": _pocket(_days(("2026-02-01", "100.00"))),
            "THB": _pocket(_days(("2026-02-01", "10.00")), available=False, balance="10.00",
                           reason="no spending history"),
        }), {"SGD": Fraction(1), "THB": Fraction(1, 25)})

        self.assertTrue(answer["complete"])
        self.assertEqual(
            {p["currency"]: p["counted"] for p in answer["pockets"]},
            {"SGD": COUNTED_PROJECTED, "THB": COUNTED_AS_IS},
        )
        self.assertEqual(answer["base"]["lowest"], "100.40")

    def test_an_unprojected_pocket_with_scheduled_money_is_not_counted(self) -> None:
        """Counting only its known events would understate what leaves it."""
        answer = self._answer(_timeline({
            "SGD": _pocket(_days(("2026-02-01", "100.00"))),
            "EUR": _pocket(_days(("2026-02-01", "10.00")), available=False,
                           events=[_event(label="Rent", on="2026-02-01", amount="5.00")]),
        }), {"SGD": Fraction(1), "EUR": Fraction(3, 2)})

        self.assertFalse(answer["complete"])
        self.assertEqual(answer["base"]["lowest"], "100.00")

    def test_income_says_whether_the_cautious_case_counts_it(self) -> None:
        salary = _event(label="Salary", on="2026-02-27", amount="1000.00", direction="inflow")
        salary["provenance"] = {"confidence": "likely"}
        answer = self._answer(_timeline({
            "USD": _pocket(_days(("2026-02-01", "100.00")), events=[salary, {**salary, "on": "2026-03-27"}]),
        }), {"USD": Fraction(13, 10)})

        [income] = answer["income"]
        self.assertEqual((income["count"], income["native_total"], income["total"]), (2, "2000.00", "2600.00"))
        self.assertFalse(income["in_cautious_case"])

    def test_where_the_money_goes_is_one_list_across_pockets(self) -> None:
        answer = self._answer(_timeline({
            "SGD": _pocket(_days(("2026-02-01", "100.00")), events=[
                _event(label="Loan", on="2026-02-05", amount="60.00", origin="personal_debt")]),
            "USD": _pocket(_days(("2026-02-01", "100.00")), events=[
                _event(label="IOU", on="2026-02-06", amount="20.00", origin="personal_debt")]),
        }), {"SGD": Fraction(1), "USD": Fraction(2)})

        [debts] = answer["where_it_goes"]
        self.assertEqual(debts["total"], "100.00")
        self.assertEqual([item["label"] for item in debts["items"]], ["Loan", "IOU"])

    def test_todos_group_one_fix_into_one_item_and_point_at_its_page(self) -> None:
        items = [
            {"output": "cash", "account_id": "a1", "start": "2026-02-01", "end": "2026-02-20"},
            {"output": "cash", "account_id": "a2", "start": "2026-02-01", "end": "2026-02-20"},
            {"output": "cash", "account_id": "a1", "start": "2025-08-31", "end": "2026-01-31"},
            {"output": "obligation_timing", "origin": "personal_debt", "label": "Loan from Alex"},
            {"output": "obligation_timing", "origin": "installment_plan", "label": "Phone"},
            {"output": "reserve", "currency": "SGD"},
            {"output": "card_settlement", "detail": "card c SGD has no reconciled exactly-once settlement plan"},
        ]
        answer = self._answer(
            _timeline({"SGD": _pocket(_days(("2026-02-01", "100.00")))}, items),
            {"SGD": Fraction(1)}, account_names={"a1": "Everyday", "a2": "Savings"},
        )

        self.assertEqual(
            [(todo["key"], todo["page"]) for todo in answer["todos"]],
            [("recent-statements", "Setup"), ("debt-dates", "Debts"), ("plan-dates", "Installments"),
             ("statement-gaps", "Setup"), ("essential", "Categorization")],
        )
        self.assertIn("Everyday, Savings", answer["todos"][0]["detail"])

    def test_an_action_item_can_name_its_own_fix_and_shares_it_by_key(self) -> None:
        fix = {"key": "fund-due", "page": "Annual funds", "title": "Did it happen?"}
        items = [
            {"output": "planned", "todo": {**fix, "detail": "Boiler was due on 2026-01-10."}},
            {"output": "planned", "todo": {**fix, "detail": "Roof was due on 2026-01-20."}},
        ]
        answer = self._answer(
            _timeline({"SGD": _pocket(_days(("2026-02-01", "100.00")))}, items), {"SGD": Fraction(1)},
        )

        self.assertEqual(
            answer["todos"],
            [{"key": "fund-due", "page": "Annual funds", "title": "Did it happen?",
              "detail": "Boiler was due on 2026-01-10. Roof was due on 2026-01-20."}],
        )


if __name__ == "__main__":
    unittest.main()


class CardPlanTodoTests(unittest.TestCase):
    def _keys(self, detail: str) -> list[str]:
        timeline = _timeline({"SGD": _pocket(_days(("2026-02-01", "100.00")))},
                             [{"output": "card_settlement", "detail": detail}])
        answer = combined_answer(timeline, rates={"SGD": Fraction(1)}, reporting_currency="SGD", scale=2)
        return [todo["key"] for todo in answer["todos"]]

    def test_an_unknown_repayment_asks_how_the_card_is_paid(self) -> None:
        self.assertIn("card-plan", self._keys("at least one evidenced statement was not settled in full, so the balance revolves"))

    def test_a_card_with_one_statement_asks_for_another(self) -> None:
        timeline = _timeline({"SGD": _pocket(_days(("2026-02-01", "100.00")))}, [{
            "output": "card_settlement", "requirement": "cycle_geometry_unevidenced",
            "label": "Rewards Card", "detail": "1 statement(s) carry both a period end and a due date",
        }])
        answer = combined_answer(timeline, rates={"SGD": Fraction(1)}, reporting_currency="SGD", scale=2)

        todo = next(todo for todo in answer["todos"] if todo["key"] == "card-history")
        self.assertEqual(todo["page"], "Setup")
        self.assertIn("Rewards Card", todo["detail"])

    def test_missing_card_bills_keep_a_yes_from_being_plain(self) -> None:
        pocket = _pocket(_days(("2026-02-01", "100.00")))
        pocket["missing_outflows"] = [{
            "kind": "card_statement", "account_id": "c1", "label": "Rewards Card",
            "reason": "its bills can't be dated or sized from the statements, so none are counted",
        }]
        answer = combined_answer(_timeline({"SGD": pocket}), rates={"SGD": Fraction(1)},
                                 reporting_currency="SGD", scale=2)

        self.assertEqual((answer["verdict"], answer["answer"]), ("enough", "Yes, but not every bill is counted"))
        self.assertFalse(answer["complete"])
        self.assertEqual(answer["incomplete_because"], [
            "Rewards Card (SGD): its bills can't be dated or sized from the statements, so none are counted.",
        ])

    def test_card_settlements_are_their_own_family(self) -> None:
        self.assertEqual(
            _driver_origin({"source_id": "posted_unbilled:2026-01-31", "provenance": {"origin": "card_statement"}}),
            "card_statement",
        )

    def test_a_declared_plan_leaves_only_the_missing_statement(self) -> None:
        self.assertNotIn("card-plan", self._keys("card c SGD has no reconciled exactly-once settlement plan"))


class NotInThePathTests(unittest.TestCase):
    """A debt with no due date is not in the forecast; the answer must say so."""

    def test_undated_debts_are_totalled_with_what_repaying_now_would_do(self) -> None:
        timeline = _timeline({"SGD": _pocket(_days(("2026-02-01", "100.00"), ("2026-02-02", "40.00")))},
                             [{"output": "obligation_timing", "obligation_id": "debt:a", "label": "A"},
                              {"output": "obligation_timing", "obligation_id": "plan:z", "label": "Done plan"}])
        owed = [
            {"obligation_id": "debt:a", "origin": "personal_debt", "label": "A", "direction": "outflow",
             "remaining_amount": {"value": "50.00", "currency": "USD"}},
            {"obligation_id": "plan:z", "origin": "installment_plan", "label": "Done plan", "direction": "outflow",
             "remaining_amount": {"value": "0.00", "currency": "SGD"}},
            {"obligation_id": "debt:dated", "origin": "personal_debt", "label": "Dated", "direction": "outflow",
             "remaining_amount": {"value": "999.00", "currency": "SGD"}},
        ]
        answer = combined_answer(timeline, rates={"SGD": Fraction(1), "USD": Fraction(13, 10)},
                                 reporting_currency="SGD", scale=2, owed=owed)
        missing = answer["not_in_the_path"]
        self.assertEqual(answer["verdict"], VERDICT_ENOUGH)
        self.assertEqual(missing["total"], "65.00")
        self.assertEqual([item["label"] for item in missing["items"]], ["A"])
        # Lowest point 40.00; repaying 65.00 now leaves it 25.00 below zero.
        self.assertEqual(missing["short_if_repaid_now"], "25.00")

    def test_nothing_undated_means_nothing_to_add(self) -> None:
        answer = combined_answer(_timeline({"SGD": _pocket(_days(("2026-02-01", "100.00")))}),
                                 rates={"SGD": Fraction(1)}, reporting_currency="SGD", scale=2, owed=[])
        self.assertEqual(answer["not_in_the_path"]["items"], [])
        self.assertIsNone(answer["not_in_the_path"]["short_if_repaid_now"])
