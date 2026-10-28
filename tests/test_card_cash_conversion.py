"""Stage 2B — the card cash-conversion contract.

One ledger fixture carries all three partitions at once — an unpaid billed
statement, postings made since it closed, and modelled future purchases — because
the failures worth catching are failures *between* partitions: a total that
reconciles per partition can still settle the same money twice or drop a month of
spending entirely. `CardCashConversionFixtureTests` is that ledger; the classes
around it pin the rules that decide when the contract refuses to answer.
"""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fincl.cards.cash_conversion import (
    PARTITION_POSTED_UNBILLED,
    PARTITION_PROSPECTIVE,
    PARTITION_UNPAID_BILLED,
    additional_spending_capacity,
    card_cash_conversion,
    horizon_position,
    partition_card_liability,
    reconcile_settlements,
    repayment_behaviour,
    settlement_plan,
    shared_purchase_cash_effects,
    statement_cycle_geometry,
)
from fincl.db import connect_sqlite, initialize_schema

# The fixture's calendar. Cycles close on the 28th and fall due 15 days later.
# The as-of date is the last day the card's own export speaks for: an observed
# entry span cannot evidence what posted after its final row.
AS_OF = "2026-07-05"
HORIZON_END = "2027-01-05"

# Three statements. The first two are past due and were settled in full — that is
# the only thing that evidences a repayment behaviour. The third is issued but not
# yet due, so it is unpaid billed liability and *not* evidence of revolving.
_STATEMENTS = (
    # (id, period_start, period_end, statement_date, due_date, due, paid)
    ("stmt-a", "2026-03-29", "2026-04-28", "2026-04-28", "2026-05-13", "500.00", "500.00"),
    ("stmt-b", "2026-04-29", "2026-05-28", "2026-05-28", "2026-06-13", "400.00", "400.00"),
    ("stmt-c", "2026-05-29", "2026-06-28", "2026-06-28", "2026-07-13", "300.00", "0.00"),
)

# Posted after stmt-c closed, not yet on any statement.
_UNBILLED = (("2026-07-01", "120.00", "Bookshop"), ("2026-07-05", "80.00", "Pharmacy"))

# What a forecast would model. The second settles on 2027-01-12 — past the horizon.
_PROSPECTIVE = (
    {"id": "plan:sofa", "account_id": "card-1", "currency": "SGD",
     "on": "2026-09-15", "amount": "250.00"},
    {"id": "plan:flights", "account_id": "card-1", "currency": "SGD",
     "on": "2026-12-20", "amount": "400.00"},
)


def _evidence(name: str) -> str:
    """A distinct 64-hex evidence id per source blob."""
    return name.encode().hex().ljust(64, "0")[:64]


def _minor(amount: str) -> int:
    return int(Decimal(amount).scaleb(2))


def _build_ledger(
    root: Path, statements: tuple[tuple[str, ...], ...] = _STATEMENTS,
    credits: tuple[tuple[str, str], ...] = (),
) -> Path:
    """A card with three statements, unbilled postings and a projected generation.

    Built directly against the schema rather than through a PDF import: the
    contract needs three statements on one consistent cycle plus card-channel
    activity after the last one closed, and stating that shape outright is what
    makes the arithmetic in the assertions checkable by eye.
    """
    database = root / "fincl.sqlite3"
    connection = connect_sqlite(database)
    try:
        initialize_schema(connection)
        connection.execute(
            "INSERT INTO accounts(id, institution, source_type, display_name, "
            "masked_identifier) VALUES ('card-1','Mari','MARI_CARD','Mari Card','**1234')"
        )
        for blob in [item[0] for item in statements] + ["csv"]:
            connection.execute(
                "INSERT INTO source_content(sha256, byte_size, media_type, evidence_key, "
                "storage_kind, original_path, last_observed_path, first_seen_at, "
                "last_seen_at, integrity_status) VALUES (?, 10, 'application/pdf', ?, "
                "'PLAINTEXT', ?, ?, '2026-07-10T00:00:00Z', '2026-07-10T00:00:00Z', "
                "'VERIFIED')",
                (_evidence(blob), f"key-{blob}", f"{blob}.pdf", f"{blob}.pdf"),
            )
        for index, item in enumerate(statements):
            statement_id, start, end, statement_date, due_date, due, paid = item
            run = f"run-{index}"
            connection.execute(
                "INSERT INTO extraction_runs(id, evidence_sha256, source_profile_version, "
                "parser_version, extraction_policy_version, started_at, completed_at, "
                "status, selection_state) VALUES (?, ?, 'mari-card-v1', "
                "'p1', 'x1', '2026-07-10T00:00:00Z', '2026-07-10T00:00:01Z', "
                "'ACCEPTED', 'SELECTED')",
                (run, _evidence(statement_id)),
            )
            connection.execute(
                "INSERT INTO statements(id, extraction_run_id, evidence_sha256, account_id, "
                "institution, source_type, statement_date, period_start, period_end, "
                "statement_identity, selection_state, conflict_state) "
                "VALUES (?, ?, ?, 'card-1', 'Mari', 'MARI_CARD', ?, ?, ?, ?, "
                "'SELECTED', 'NONE')",
                (statement_id, run, _evidence(statement_id), statement_date, start, end,
                 statement_id),
            )
            for role, kind, text, coefficient, scale in (
                ("payment_due", "money", due, _minor(due), 2),
                ("payment_due_date", "date", due_date, None, None),
                ("statement_period_start", "date", start, None, None),
                ("statement_period_end", "date", end, None, None),
            ):
                connection.execute(
                    "INSERT INTO card_statement_facts(statement_id, extraction_run_id, "
                    "currency_pocket, field_role, value_kind, value_text, "
                    "value_coefficient, value_scale, source_locator, parser_version, "
                    "role_version) VALUES (?, ?, 'SGD', ?, ?, ?, ?, ?, 'p1', 'v1', 'r1')",
                    (statement_id, run, role, kind, text, coefficient, scale),
                )
            if Decimal(paid) > 0:
                connection.execute(
                    "INSERT INTO card_payment_allocations(card_entry_anchor, seq_in_entry, "
                    "statement_id, entry_id, amount_coefficient, amount_scale, currency, "
                    "journal_seq, bound) VALUES (?, 1, ?, NULL, ?, 2, 'SGD', ?, 1)",
                    (f"pay:{statement_id}", statement_id, _minor(paid), index + 1),
                )

        # A CSV export of the card's own activity, running to the as-of date. This
        # is what makes the unbilled window *known* rather than assumed: without
        # it the contract has no evidence of what posted since stmt-c closed.
        connection.execute(
            "INSERT INTO extraction_runs(id, evidence_sha256, source_profile_version, "
            "parser_version, extraction_policy_version, started_at, completed_at, "
            "status, selection_state) VALUES ('run-csv', ?, "
            "'revolut-csv-v1', 'p2', 'x1', '2026-07-10T00:00:00Z', "
            "'2026-07-10T00:00:01Z', 'ACCEPTED', 'SELECTED')",
            (_evidence("csv"),),
        )
        connection.execute(
            "INSERT INTO projection_generation(id, generation_token, manifest_json, "
            "completed_at) VALUES (1, 'gen-1', '{}', '2026-07-10T00:00:02Z')"
        )
        # Card activity across the whole exported span; only what posted after
        # 2026-06-28 is unbilled.
        activity = (("2026-06-20", "45.00", "Older, already billed"),) + _UNBILLED
        for index, (day, amount, label) in enumerate(activity):
            entry_id = f"entry-{index}"
            connection.execute(
                "INSERT INTO raw_records(id, extraction_run_id, evidence_sha256, "
                "source_profile_version, parser_version, header_sha256, source_anchor, "
                "extraction_ordinal, physical_line_start, physical_line_end, "
                "record_kind, raw_record_text, raw_cells_json, description_raw, "
                "confidence, required_fields_complete) "
                "VALUES (?, 'run-csv', ?, 'revolut-csv-v1', 'p2', 'h1', ?, ?, ?, ?, "
                "'TRANSACTION', ?, '[]', ?, 'HIGH', 1)",
                (f"raw-{index}", _evidence("csv"), f"anchor-{index}", index + 1,
                 index + 1, index + 1, label, label),
            )
            connection.execute(
                "INSERT INTO ledger_entries(id, extraction_run_id, statement_id, "
                "raw_record_id, document_record_id, account_id, source_family, "
                "natural_anchor, effective_at, effective_precision, currency_pocket, "
                "amount_text, amount_coefficient, amount_scale, economic_direction, "
                "event_kind, evidence_sha256, analytics_eligible, lifecycle_state) "
                "VALUES (?, 'run-csv', NULL, ?, NULL, 'card-1', 'CSV', ?, ?, 'DAY', "
                "'SGD', ?, ?, 2, 'OUT', 'CARD_PAYMENT', ?, 1, 'COMPLETED')",
                (entry_id, f"raw-{index}", f"anchor-{index}", f"{day}T10:00:00Z", amount,
                 -_minor(amount), _evidence("csv")),
            )
            component_id = f"c-{index}"
            connection.execute(
                "INSERT INTO analysis_components(component_id, origin_component_id, "
                "generation_token, lens, origin_kind, origin_anchor, entry_id, role, "
                "bucket, signed_coefficient, scale, currency, represented_start, "
                "represented_end, date_precision, period, category_key, merchant_key, "
                "merchant_display, account, source_location, evidence_sha256, "
                "decision_seqs, nets, eligible) VALUES (?, ?, 'gen-1', 'recorded_flow', "
                "'ENTRY', ?, ?, 'amount', 'spending', ?, 2, 'SGD', ?, ?, 'DAY', ?, "
                "'Shopping / General Shopping', ?, ?, 'Mari Card', 'CSV row', "
                "?, '[]', 1, 1)",
                (component_id, f"oc-{index}", f"anchor-{index}", entry_id,
                 -_minor(amount), day, day, day[:7], label.upper(), label,
                 _evidence("csv")),
            )
            connection.execute(
                "INSERT INTO component_provenance(component_id, generation_token, "
                "entry_id, source_account_id, account_source_type, payment_channel) "
                "VALUES (?, 'gen-1', ?, 'card-1', 'MARI_CARD', 'card')",
                (component_id, entry_id),
            )
        # Credits posted to the card itself (repayments, refunds): money in.
        for index, (day, amount) in enumerate(credits):
            connection.execute(
                "INSERT INTO raw_records(id, extraction_run_id, evidence_sha256, "
                "source_profile_version, parser_version, header_sha256, source_anchor, "
                "extraction_ordinal, physical_line_start, physical_line_end, "
                "record_kind, raw_record_text, raw_cells_json, description_raw, "
                "confidence, required_fields_complete) "
                "VALUES (?, 'run-csv', ?, 'revolut-csv-v1', 'p2', 'h1', ?, ?, ?, ?, "
                "'TRANSACTION', 'PAYMENT', '[]', 'PAYMENT', 'HIGH', 1)",
                (f"raw-credit-{index}", _evidence("csv"), f"credit-{index}", 100 + index,
                 100 + index, 100 + index),
            )
            connection.execute(
                "INSERT INTO ledger_entries(id, extraction_run_id, statement_id, "
                "raw_record_id, document_record_id, account_id, source_family, "
                "natural_anchor, effective_at, effective_precision, currency_pocket, "
                "amount_text, amount_coefficient, amount_scale, economic_direction, "
                "event_kind, evidence_sha256, analytics_eligible, lifecycle_state) "
                "VALUES (?, 'run-csv', NULL, ?, NULL, 'card-1', 'CSV', ?, ?, 'DAY', "
                "'SGD', ?, ?, 2, 'IN', 'TRANSFER', ?, 0, 'COMPLETED')",
                (f"credit-{index}", f"raw-credit-{index}", f"credit-{index}", f"{day}T10:00:00Z",
                 amount, _minor(amount), _evidence("csv")),
            )
        connection.commit()
    finally:
        connection.close()
    return database


class CardCashConversionFixtureTests(unittest.TestCase):
    """All three partitions on one card, reconciled to bank settlement.

    Billed 300 (stmt-c, due 2026-07-13) + unbilled 200 (posted since stmt-c
    closed, billed by the 2026-07-28 cycle, due 2026-08-12) + prospective 650
    (250 due 2026-10-13, 400 due 2027-01-12).
    """

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(tempfile.mkdtemp())
        cls.database = _build_ledger(root)
        cls.result = card_cash_conversion(
            cls.database,
            as_of=AS_OF,
            prospective_purchases=list(_PROSPECTIVE),
            cash_available="2000.00",
        )
        cls.card = cls.result["cards"][0]

    def _partition(self, name: str) -> dict:
        return next(
            item
            for item in self.card["partitions"]["partitions"]
            if item["partition"] == name
        )

    def test_the_cycle_and_the_repayment_behaviour_are_both_evidenced(self) -> None:
        geometry = self.card["cycle_geometry"]
        self.assertTrue(geometry["available"], geometry)
        self.assertEqual(geometry["cycle_day"], 28)
        self.assertEqual(geometry["due_offset_days"], 15)
        self.assertEqual(geometry["latest_period_end"], "2026-06-28")
        behaviour = self.card["repayment_behaviour"]
        # Only stmt-a and stmt-b are past due; stmt-c being unpaid is not
        # evidence of revolving, it is simply not due yet.
        self.assertEqual(behaviour["pattern"], "pays_statement_in_full")
        self.assertEqual(behaviour["observed_statements"], 2)
        self.assertTrue(behaviour["supports_settlement_schedule"])

    def test_each_partition_holds_what_belongs_to_it_and_nothing_else(self) -> None:
        billed = self._partition(PARTITION_UNPAID_BILLED)
        self.assertEqual(billed["amount"], "300.00")
        self.assertEqual([item["statement_id"] for item in billed["items"]], ["stmt-c"])

        unbilled = self._partition(PARTITION_POSTED_UNBILLED)
        self.assertTrue(unbilled["known"], unbilled)
        self.assertEqual(unbilled["amount"], "200.00")
        self.assertEqual(unbilled["window"], {"after": "2026-06-28", "through": AS_OF})
        # The 45.00 posted on 2026-06-20 fell inside stmt-c's period: it is
        # already inside the billed 300 and must not appear again here.
        self.assertEqual([item["on"] for item in unbilled["items"]],
                         ["2026-07-01", "2026-07-05"])

        prospective = self._partition(PARTITION_PROSPECTIVE)
        self.assertEqual(prospective["amount"], "650.00")

    def test_all_three_partitions_settle_to_the_bank_exactly_once(self) -> None:
        plan = self.card["plan"]
        self.assertTrue(plan["available"], plan["unresolved"])
        self.assertEqual(plan["unresolved"], [])
        self.assertEqual(
            [(row["settle_on"], row["amount"], row["partition"])
             for row in plan["settlements"]],
            [
                ("2026-07-13", "300.00", PARTITION_UNPAID_BILLED),
                ("2026-08-12", "200.00", PARTITION_POSTED_UNBILLED),
                ("2026-10-13", "250.00", PARTITION_PROSPECTIVE),
                ("2027-01-12", "400.00", PARTITION_PROSPECTIVE),
            ],
        )
        # The billed statement settles on its own printed due date; everything
        # not yet billed settles on a date derived from the observed cycle.
        self.assertEqual(plan["settlements"][0]["basis"], "evidenced_statement_due_date")
        for row in plan["settlements"][1:]:
            self.assertEqual(row["basis"], "projected_cycle_due_date")

    def test_the_reconciliation_reports_no_omission_and_no_duplication(self) -> None:
        reconciliation = self.card["reconciliation"]
        self.assertTrue(reconciliation["reconciled"], reconciliation)
        self.assertEqual(reconciliation["duplicated_source_ids"], [])
        self.assertEqual(reconciliation["omitted_source_ids"], [])
        self.assertEqual(
            {row["partition"]: (row["liability"], row["settled"])
             for row in reconciliation["per_partition"]},
            {
                PARTITION_UNPAID_BILLED: ("300.00", "300.00"),
                PARTITION_POSTED_UNBILLED: ("200.00", "200.00"),
                PARTITION_PROSPECTIVE: ("650.00", "650.00"),
            },
        )
        # Every settlement traces to one source item, and every source item to
        # one settlement: the totals above cannot be balancing by coincidence.
        ids = [row["source_id"] for row in self.card["plan"]["settlements"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_card_baseline_is_never_also_an_immediate_bank_debit(self) -> None:
        self.assertFalse(self.card["plan"]["immediate_bank_debit"]["included"])
        # Cash leaving inside the horizon is exactly the settlements — never the
        # settlements plus the purchases that produced them.
        horizon = self.card["horizon"]
        self.assertEqual(horizon["in_horizon_bank_outflow"], "750.00")
        self.assertEqual(
            Decimal(horizon["in_horizon_bank_outflow"])
            + Decimal(horizon["ending_unpaid_liability"]),
            Decimal("300.00") + Decimal("200.00") + Decimal("650.00"),
        )

    def test_a_purchase_settling_past_the_horizon_is_liability_not_free_money(self) -> None:
        horizon = self.card["horizon"]
        self.assertEqual(horizon["horizon_end"], HORIZON_END)
        self.assertEqual(
            [row["source_id"] for row in horizon["settling_beyond_horizon"]],
            ["plan:flights"],
        )
        self.assertEqual(horizon["ending_unpaid_liability"], "400.00")
        capacity = self.card["additional_spending_capacity"]
        self.assertTrue(capacity["available"], capacity)
        self.assertEqual(capacity["value"], "850.00")

    def test_moving_spend_onto_the_card_cannot_raise_capacity_at_the_boundary(self) -> None:
        on_card = self.card["additional_spending_capacity"]["value"]
        # The same 400 paid from the bank on the purchase date: the cash leaves
        # inside the horizon (750 + 400) and no liability stands at the boundary.
        from_bank = additional_spending_capacity(
            cash_available="2000.00",
            position={
                "in_horizon_bank_outflow": "1150.00",
                "ending_unpaid_liability": "0.00",
            },
            unresolved=[],
            currency="SGD",
        )
        self.assertEqual(Decimal(from_bank["value"]), Decimal("850.00"))
        self.assertEqual(Decimal(on_card), Decimal(from_bank["value"]))


class SharedPurchaseCashTests(unittest.TestCase):
    """A shared bill of 120 with an owner share of 40, repaid 80 next month."""

    def setUp(self) -> None:
        self.effects = shared_purchase_cash_effects(
            gross="120.00",
            owner_share="40.00",
            paid_on="2026-07-05",
            currency="SGD",
            reimbursement_expected_on="2026-08-05",
        )

    def test_the_full_gross_of_120_is_needed_before_any_repayment(self) -> None:
        self.assertEqual(self.effects["cash_required_on_payment_date"], "120.00")
        debits = [item for item in self.effects["cash_effects"] if item["kind"] == "debit"]
        self.assertEqual(len(debits), 1)
        self.assertEqual(debits[0]["amount"], "120.00")
        self.assertEqual(debits[0]["on"], "2026-07-05")
        # Never the owner share, and never the gross net of what is expected back.
        self.assertNotEqual(debits[0]["amount"], "40.00")
        self.assertFalse(self.effects["netting"]["applied"])
        self.assertEqual(self.effects["netting"]["would_have_shown"], "40.00")

    def test_the_80_cannot_fund_the_payment_it_reimburses(self) -> None:
        reimbursement = self.effects["reimbursement"]
        self.assertEqual(reimbursement["amount"], "80.00")
        self.assertFalse(reimbursement["can_fund_the_payment"])
        self.assertIn("must be available first", reimbursement["reason"])

    def test_an_expected_repayment_is_not_cash_until_it_is_received(self) -> None:
        self.assertEqual(self.effects["reimbursement"]["status"], "expected_not_received")
        self.assertFalse(self.effects["reimbursement"]["is_cash"])
        self.assertEqual(
            [item["kind"] for item in self.effects["cash_effects"]], ["debit"]
        )
        self.assertEqual(
            [item["requirement"] for item in self.effects["unresolved"]],
            ["reimbursement_not_received"],
        )

    def test_a_received_repayment_enters_cash_on_the_date_it_arrived(self) -> None:
        received = shared_purchase_cash_effects(
            gross="120.00", owner_share="40.00", paid_on="2026-07-05",
            currency="SGD", reimbursement_expected_on="2026-08-05",
            reimbursement_received_on="2026-08-19",
        )
        receipt = [item for item in received["cash_effects"] if item["kind"] == "receipt"]
        self.assertEqual([(item["on"], item["amount"]) for item in receipt],
                         [("2026-08-19", "80.00")])
        # It arrived later than expected; the cash date follows the receipt, and
        # the gross debit in July is untouched by it.
        self.assertEqual(received["cash_required_on_payment_date"], "120.00")
        self.assertFalse(received["reimbursement"]["can_fund_the_payment"])
        self.assertEqual(received["unresolved"], [])


class UnresolvedSettlementTests(unittest.TestCase):
    """The contract refuses rather than guesses, and an unknown is never a zero."""

    _STATEMENTS = [
        {"statement_id": "s1", "period_end": "2026-05-28", "payment_due_date": "2026-06-13",
         "due": "400.00", "paid": "400.00"},
        {"statement_id": "s2", "period_end": "2026-06-28", "payment_due_date": "2026-07-13",
         "due": "300.00", "paid": "150.00"},
    ]

    def _plan(self, *, behaviour: dict, unbilled: dict) -> tuple[dict, dict]:
        geometry = statement_cycle_geometry(
            self._STATEMENTS, as_of="2026-08-01", project_until="2027-02-01"
        )
        partitions = partition_card_liability(
            billed=[], unbilled=unbilled, prospective=[],
            as_of="2026-08-01", latest_period_end="2026-06-28",
        )
        return partitions, settlement_plan(
            partitions=partitions, geometry=geometry, behaviour=behaviour,
            currency="SGD", as_of="2026-08-01",
        )

    def test_statements_existing_is_not_evidence_of_full_repayment(self) -> None:
        behaviour = repayment_behaviour(self._STATEMENTS, as_of="2026-08-01")
        self.assertEqual(behaviour["pattern"], "partial_or_revolving")
        self.assertFalse(behaviour["supports_settlement_schedule"])
        self.assertEqual(
            [item["requirement"] for item in behaviour["unresolved"]],
            ["repayment_behaviour_partial"],
        )
        _, plan = self._plan(
            behaviour=behaviour, unbilled={"known": True, "amount": "200.00", "items": []}
        )
        # No schedule is invented for a balance that revolves.
        self.assertFalse(plan["available"])
        self.assertEqual(plan["settlements"], [])
        self.assertIn("repayment_behaviour_partial",
                      [item["requirement"] for item in plan["unresolved"]])

    def test_one_statement_alone_does_not_establish_a_cycle(self) -> None:
        geometry = statement_cycle_geometry(
            self._STATEMENTS[:1], as_of="2026-08-01", project_until="2027-02-01"
        )
        self.assertFalse(geometry["available"])
        self.assertEqual(geometry["projected_cycles"], [])
        self.assertEqual(
            [item["requirement"] for item in geometry["unresolved"]],
            ["cycle_geometry_unevidenced"],
        )

    def test_an_unknown_unbilled_balance_is_unresolved_and_never_zero(self) -> None:
        partitions, plan = self._plan(
            behaviour=repayment_behaviour(
                [self._STATEMENTS[0]] * 2, as_of="2026-08-01"
            ),
            unbilled={"known": False, "reason": "no card activity covers the window"},
        )
        unbilled = next(
            item for item in partitions["partitions"]
            if item["partition"] == PARTITION_POSTED_UNBILLED
        )
        self.assertIsNone(unbilled["amount"])
        self.assertNotEqual(unbilled["amount"], "0.00")
        self.assertEqual(
            [item["requirement"] for item in unbilled["unresolved"]],
            ["unbilled_balance_unknown"],
        )
        # It propagates: the plan and the reconciliation both stay open about it.
        self.assertFalse(plan["available"])
        row = next(
            item for item in reconcile_settlements(partitions, plan)["per_partition"]
            if item["partition"] == PARTITION_POSTED_UNBILLED
        )
        self.assertFalse(row["reconciled"])
        self.assertIsNone(row["liability"])

    def test_capacity_is_withheld_while_a_requirement_is_unresolved(self) -> None:
        _, plan = self._plan(
            behaviour=repayment_behaviour(self._STATEMENTS, as_of="2026-08-01"),
            unbilled={"known": False, "reason": "no card activity covers the window"},
        )
        capacity = additional_spending_capacity(
            cash_available="5000.00",
            position=horizon_position(plan, as_of="2026-08-01"),
            unresolved=plan["unresolved"],
            currency="SGD",
        )
        self.assertFalse(capacity["available"])
        self.assertIsNone(capacity["value"])


if __name__ == "__main__":
    unittest.main()


class DeclaredRepaymentPlanTests(unittest.TestCase):
    """A revolving card is scheduled only once its owner says how it is repaid.

    stmt-b was part-paid (100 of 400), so the balance revolves; stmt-c's 600
    already carries what stmt-b left unpaid. Settling both would pay the same
    300 twice.
    """

    _REVOLVING = (
        ("stmt-a", "2026-03-29", "2026-04-28", "2026-04-28", "2026-05-13", "500.00", "500.00"),
        ("stmt-b", "2026-04-29", "2026-05-28", "2026-05-28", "2026-06-13", "400.00", "100.00"),
        ("stmt-c", "2026-05-29", "2026-06-28", "2026-06-28", "2026-07-13", "600.00", "0.00"),
    )

    def setUp(self) -> None:
        self.database = _build_ledger(Path(tempfile.mkdtemp()), self._REVOLVING)

    def _card(self, as_of: str = AS_OF) -> dict:
        return card_cash_conversion(self.database, as_of=as_of)["cards"][0]

    def _declare(self) -> None:
        connection = connect_sqlite(self.database)
        try:
            connection.execute(
                "INSERT INTO card_repayment_plans(account_id, rule, reason, journal_seq) "
                "VALUES ('card-1', 'pay_in_full', 'I clear it each month now', 1)"
            )
            connection.commit()
        finally:
            connection.close()

    def test_without_a_plan_a_revolving_card_has_no_schedule(self) -> None:
        card = self._card()
        self.assertEqual(card["repayment_behaviour"]["pattern"], "partial_or_revolving")
        self.assertFalse(card["plan"]["available"])

    def test_without_a_plan_the_unpaid_balance_is_still_billed_once(self) -> None:
        billed = next(item for item in self._card()["partitions"]["partitions"]
                      if item["partition"] == PARTITION_UNPAID_BILLED)
        self.assertEqual(billed["amount"], "600.00")
        self.assertEqual([item["statement_id"] for item in billed["items"]], ["stmt-c"])

    def test_a_declared_plan_settles_the_latest_statement_once(self) -> None:
        self._declare()
        card = self._card()
        behaviour = card["repayment_behaviour"]
        self.assertEqual(behaviour["pattern"], "declared_pays_in_full")
        # What the ledger shows is still reported beside what was declared.
        self.assertEqual(behaviour["evidenced_pattern"], "partial_or_revolving")
        billed = [row for row in card["plan"]["settlements"] if row["partition"] == PARTITION_UNPAID_BILLED]
        self.assertEqual([(row["amount"], row["settle_on"], row["basis"]) for row in billed],
                         [("600.00", "2026-07-13", "declared_plan_statement_due_date")])
        self.assertTrue(card["plan"]["available"], card["plan"]["unresolved"])
        self.assertTrue(card["reconciliation"]["reconciled"], card["reconciliation"])
        self.assertEqual(card["reconciliation"]["duplicated_source_ids"], [])

    def test_an_overdue_balance_is_paid_the_next_day_never_in_the_past(self) -> None:
        self._declare()
        billed = next(item for item in self._card("2026-07-20")["partitions"]["partitions"]
                      if item["partition"] == PARTITION_UNPAID_BILLED)
        [item] = billed["items"]
        self.assertEqual((item["amount"], item["due_on"], item["settle_basis"]),
                         ("600.00", "2026-07-21", "declared_plan_overdue_paid_next_day"))


class ObservedCardCreditTests(unittest.TestCase):
    """A statement is paid by what the card itself shows arriving after it closed.

    No manual allocation is recorded: stmt-a and stmt-b are settled by credits
    posted after each period ended and by its due date, so the card is read as
    paid in full each month without anyone matching a payment by hand.
    """

    _UNMATCHED = tuple(item[:6] + ("0.00",) for item in _STATEMENTS)

    def _card(self, credits: tuple[tuple[str, str], ...]) -> dict:
        database = _build_ledger(Path(tempfile.mkdtemp()), self._UNMATCHED, credits)
        return card_cash_conversion(database, as_of=AS_OF)["cards"][0]

    def test_credits_in_each_payment_window_settle_the_statement(self) -> None:
        # stmt-a (closed 04-28, due 05-13): a refund and a repayment; stmt-b: one repayment.
        card = self._card((("2026-04-30", "20.00"), ("2026-05-10", "480.00"), ("2026-06-12", "400.00")))
        behaviour = card["repayment_behaviour"]
        self.assertEqual(behaviour["pattern"], "pays_statement_in_full")
        self.assertEqual([item["paid"] for item in behaviour["observations"]], ["500.00", "400.00"])

    def test_a_credit_after_the_as_of_date_has_not_paid_anything_yet(self) -> None:
        card = self._card((("2026-05-10", "500.00"), ("2026-06-12", "400.00"), ("2026-07-10", "300.00")))
        billed = next(item for item in card["partitions"]["partitions"]
                      if item["partition"] == PARTITION_UNPAID_BILLED)
        self.assertEqual(billed["amount"], "300.00")

    def test_a_credit_after_the_due_date_is_not_a_settlement_of_that_statement(self) -> None:
        card = self._card((("2026-05-20", "500.00"), ("2026-06-12", "400.00")))
        self.assertEqual(card["repayment_behaviour"]["pattern"], "partial_or_revolving")

    def test_no_credit_is_counted_toward_two_statements(self) -> None:
        """A due date past the next period's end still stops at that end."""
        statements = (
            ("stmt-a", "2026-03-29", "2026-04-28", "2026-04-28", "2026-06-13", "500.00", "0.00"),
            ("stmt-b", "2026-04-29", "2026-05-28", "2026-05-28", "2026-06-13", "400.00", "0.00"),
        )
        database = _build_ledger(Path(tempfile.mkdtemp()), statements, (("2026-06-01", "900.00"),))
        connection = connect_sqlite(database)
        try:
            from fincl.cards.cash_conversion import _statement_rows
            rows = {row["statement_id"]: row for row in _statement_rows(connection, "card-1", "SGD", as_of=AS_OF)}
        finally:
            connection.close()
        self.assertEqual((rows["stmt-a"]["paid"], rows["stmt-a"]["paid_basis"]), ("0.00", "none"))
        self.assertEqual((rows["stmt-b"]["paid"], rows["stmt-b"]["paid_basis"]), ("900.00", "card_credits_in_payment_window"))

    def test_a_manual_allocation_still_wins(self) -> None:
        database = _build_ledger(Path(tempfile.mkdtemp()), _STATEMENTS, (("2026-05-10", "1.00"),))
        connection = connect_sqlite(database)
        try:
            from fincl.cards.cash_conversion import _statement_rows
            row = next(r for r in _statement_rows(connection, "card-1", "SGD", as_of=AS_OF) if r["statement_id"] == "stmt-a")
        finally:
            connection.close()
        self.assertEqual((row["paid"], row["paid_basis"]), ("500.00", "manual_allocation"))

    def test_the_obligations_read_counts_the_same_credits(self) -> None:
        """A bill the card shows repaid is not still owed on Obligations."""
        credits = (("2026-05-10", "500.00"), ("2026-06-12", "400.00"), ("2026-07-03", "300.00"))
        database = _build_ledger(Path(tempfile.mkdtemp()), self._UNMATCHED, credits)
        connection = connect_sqlite(database)
        try:
            from fincl.obligations.queries import _card_obligations
            [obligation] = _card_obligations(connection, AS_OF)
        finally:
            connection.close()
        self.assertEqual(obligation["source"]["id"], "stmt-c")
        self.assertEqual(obligation["actual_payments"]["total"], "300.00")
        self.assertEqual(obligation["remaining_amount"]["value"], "0.00")
