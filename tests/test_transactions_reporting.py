"""M3b.2 step 07: all-currency transaction API, exact pagination and selection.

Builds real ledgers through the Revolut import path (mirroring
``tests.test_analysis_reporting``), so every candidate row is a genuine
imported ledger entry with real eligibility/selection/statement-linkage
plumbing rather than a hand-poked row.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from fractions import Fraction
from pathlib import Path

from fincl.db import connect_readonly, connect_sqlite
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.fx import load_fx_rates
from fincl.refresh import refresh_ledger
from fincl.reporting_currency import REPORTING_CURRENCY_SET
from fincl.reporting_transactions import (
    decode_anchor,
    encode_anchor,
    reported_transaction_anchors_from,
    reported_transaction_detail_from,
    reported_transactions_page_from,
)
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes
from tests.test_api import ApiTestBase

_ANCHORS = {"ledger": "default"}


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _load_rate(database: Path, base: str, period: str, rate: str, *, quote: str = "SGD") -> None:
    connection = connect_sqlite(database)
    try:
        load_fx_rates(
            connection,
            {
                "source": "test", "source_version": "v1", "quote_currency": quote,
                "rates": [{"base_currency": base, "period": period, "rate": rate}],
            },
        )
    finally:
        connection.close()
    refresh_ledger(database)


def _set_reporting_currency(database: Path, currency: str, *, reason: str = "test") -> None:
    append_decision(
        default_journal_path(database), decision_kind=REPORTING_CURRENCY_SET, anchors=_ANCHORS,
        payload={"currency": currency, "reason": reason},
        policy_version="reporting-currency-v1", snapshot=currency,
    )
    connection = connect_sqlite(database)
    try:
        replay_journal(connection)
    finally:
        connection.close()
    refresh_ledger(database)


def _page(database: Path, **kwargs: object):
    connection = connect_readonly(database)
    try:
        return reported_transactions_page_from(connection, **kwargs)
    finally:
        connection.close()


def _anchors(database: Path, **kwargs: object):
    connection = connect_readonly(database)
    try:
        return reported_transaction_anchors_from(connection, **kwargs)
    finally:
        connection.close()


def _detail(database: Path, **kwargs: object):
    connection = connect_readonly(database)
    try:
        return reported_transaction_detail_from(connection, **kwargs)
    finally:
        connection.close()


_MIXED_ROWS = [
    ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
     "Seed SGD", "1000.00", "0.00", "SGD", "COMPLETED", "1000.00"],
    ["Topup", "Current", "2026-01-01 09:01:00", "2026-01-01 09:01:01",
     "Seed USD", "500.00", "0.00", "USD", "COMPLETED", "500.00"],
    ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "960.00"],
    ["Card Payment", "Current", "2026-01-06 10:00:00", "2026-01-06 10:00:01",
     "US Shop", "-20.00", "1.00", "USD", "COMPLETED", "479.00"],
    ["Refund", "Current", "2026-01-10 10:00:00", "2026-01-10 10:00:01",
     "Groceries", "5.00", "0.00", "SGD", "COMPLETED", "965.00"],
    ["Topup", "Current", "2026-01-31 23:00:00", "2026-01-31 23:00:01",
     "Month-end marker", "0.01", "0.00", "SGD", "COMPLETED", "965.01"],
]


class MixedPocketListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger(_MIXED_ROWS)
        _load_rate(self.database, "USD", "2026-01", "1.3600")

    def test_all_pockets_appear_by_default(self) -> None:
        page = _page(self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31")
        currencies = {row.entry["currency"] for row in page.rows}
        self.assertEqual(currencies, {"SGD", "USD"})
        self.assertEqual(page.total, len(_MIXED_ROWS))

    def test_pocket_currency_narrows_explicitly(self) -> None:
        page = _page(
            self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
            pocket_currency="USD",
        )
        self.assertTrue(all(row.entry["currency"] == "USD" for row in page.rows))
        self.assertEqual(page.total, 2)

    def test_identity_conversion_uses_native_settlement_not_reconverted_original(self) -> None:
        # The SGD row's reported amount must equal its native amount exactly
        # (identity valuation), never a foreign-original re-priced figure.
        page = _page(self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31")
        sgd_grocery = next(row for row in page.rows if row.entry["description"].startswith("Groceries") and row.entry["amount"] == "-40.00")
        native = sgd_grocery.amount_valuation.native
        self.assertEqual(sgd_grocery.amount_valuation.reported_exact, Fraction(native.coefficient, 10**native.scale))

    def test_reported_amount_and_fee_are_valued(self) -> None:
        page = _page(self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31")
        us_shop = next(row for row in page.rows if row.entry["description"] == "US Shop")
        self.assertIsNone(us_shop.amount_valuation.unavailable)
        assert us_shop.amount_valuation.reported_exact is not None
        # -20.00 USD * 1.3600 = -27.20 SGD, exactly.
        self.assertEqual(us_shop.amount_valuation.reported_exact, Fraction(-2720, 100))
        self.assertIsNotNone(us_shop.fee_valuation)

    def test_statement_detail_is_attached_and_native_fields_survive(self) -> None:
        page = _page(self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31")
        for row in page.rows:
            self.assertIsNotNone(row.statement_detail)
            self.assertEqual(row.statement_detail.settled.currency, row.entry["currency"])


class MultiPageAndTieBreakTests(unittest.TestCase):
    def _many_rows(self, count: int) -> list[list[str]]:
        rows = []
        balance = 1000
        for i in range(count):
            day = (i % 28) + 1
            balance -= 1
            rows.append(
                [
                    "Card Payment", "Current", f"2026-01-{day:02d} 10:00:00", f"2026-01-{day:02d} 10:00:01",
                    f"Merchant {i:03d}", "-1.00", "0.00", "SGD", "COMPLETED", f"{balance}.00",
                ]
            )
        return rows

    def test_multiple_pages_cover_every_row_exactly_once(self) -> None:
        rows = self._many_rows(25)
        database = _ledger(rows)
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page = _page(
                database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
                sort="merchant", cursor=cursor, limit=7,
            )
            seen.extend(row.entry["anchor"] for row in page.rows)
            self.assertEqual(page.total, 25)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        self.assertEqual(len(seen), 25)
        self.assertEqual(len(set(seen)), 25)

    def test_tie_break_orders_equal_amounts_by_anchor_then_id(self) -> None:
        # Every row has the identical native -1.00 SGD amount: with no FX
        # conversion involved, reported values tie exactly, so the anchor/id
        # tie-break alone must produce a stable, deterministic order.
        rows = self._many_rows(5)
        database = _ledger(rows)
        page = _page(
            database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
            sort="amount", limit=50,
        )
        anchors = [row.entry["anchor"] for row in page.rows]
        self.assertEqual(anchors, sorted(anchors))


class CrossCurrencyRankAndPrecisionTests(unittest.TestCase):
    def test_reported_amount_sort_can_reverse_native_rank(self) -> None:
        # Native SGD -50 outranks (is more negative than) native USD -10, but
        # at a rate of 6 SGD/USD the USD row reprices to -60 SGD -- larger in
        # magnitude than the SGD row. The reported "amount" sort must reflect
        # that reprice, not the native ordering.
        rows = [
            ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
             "Seed SGD", "1000.00", "0.00", "SGD", "COMPLETED", "1000.00"],
            ["Topup", "Current", "2026-01-01 09:01:00", "2026-01-01 09:01:01",
             "Seed USD", "500.00", "0.00", "USD", "COMPLETED", "500.00"],
            ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
             "SGD Big", "-50.00", "0.00", "SGD", "COMPLETED", "950.00"],
            ["Card Payment", "Current", "2026-01-06 10:00:00", "2026-01-06 10:00:01",
             "USD Small Native", "-10.00", "0.00", "USD", "COMPLETED", "490.00"],
        ]
        database = _ledger(rows)
        _load_rate(database, "USD", "2026-01", "6.0000")
        page = _page(
            database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
            sort="amount", limit=50,
        )
        merchants = [row.entry["description"] for row in page.rows if row.entry["description"].startswith(("SGD Big", "USD Small"))]
        # Ascending "amount" order = largest outgoing first: -60 SGD (USD row) before -50 SGD.
        self.assertEqual(merchants, ["USD Small Native", "SGD Big"])

    def test_exact_distinct_amounts_sort_correctly_even_if_display_rounds_equal(self) -> None:
        # Two USD debits reprice, at a rate carrying extra precision, to two
        # SGD amounts that round to the same 2-decimal display but are
        # exactly distinct -- the sort must use the exact Fraction, not the
        # rounded MoneyDTO text.
        rows = [
            ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
             "Seed USD", "1000.00", "0.00", "USD", "COMPLETED", "1000.00"],
            ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
             "A", "-100.00", "0.00", "USD", "COMPLETED", "900.00"],
            ["Card Payment", "Current", "2026-01-06 10:00:00", "2026-01-06 10:00:01",
             "B", "-100.01", "0.00", "USD", "COMPLETED", "799.99"],
        ]
        database = _ledger(rows)
        # 1.360049 rounds display-wise almost identically for these two
        # amounts (-136.0049 vs -136.018899...) but the exact Fractions differ.
        _load_rate(database, "USD", "2026-01", "1.360049")
        page = _page(
            database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
            sort="amount", limit=50,
        )
        a = next(row for row in page.rows if row.entry["description"] == "A")
        b = next(row for row in page.rows if row.entry["description"] == "B")
        self.assertNotEqual(a.amount_valuation.reported_exact, b.amount_valuation.reported_exact)
        merchants = [row.entry["description"] for row in page.rows if row.entry["description"] in ("A", "B")]
        self.assertEqual(merchants, ["B", "A"])  # -100.01 reprices more negative than -100.00


class UnpriceableRowTests(unittest.TestCase):
    def test_missing_fx_row_stays_visible_and_sorts_last(self) -> None:
        rows = [
            ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
             "Seed SGD", "1000.00", "0.00", "SGD", "COMPLETED", "1000.00"],
            ["Topup", "Current", "2026-01-01 09:01:00", "2026-01-01 09:01:01",
             "Seed GBP", "500.00", "0.00", "GBP", "COMPLETED", "500.00"],
            ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
             "SGD spend", "-40.00", "0.00", "SGD", "COMPLETED", "960.00"],
            ["Card Payment", "Current", "2026-01-06 10:00:00", "2026-01-06 10:00:01",
             "GBP spend, no fx", "-20.00", "0.00", "GBP", "COMPLETED", "480.00"],
        ]
        database = _ledger(rows)
        # No GBP rate is ever imported.
        page = _page(
            database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
            sort="amount", limit=50,
        )
        self.assertEqual(page.total, 4)
        gbp_row = next(row for row in page.rows if row.entry["currency"] == "GBP" and row.entry["amount"] == "-20.00")
        self.assertIsNotNone(gbp_row.amount_valuation.unavailable)
        # Unpriceable rows sort after every priced row.
        priced_seen = False
        for row in page.rows:
            if row.amount_valuation.unavailable is not None:
                priced_seen = True
            elif priced_seen:
                self.fail("a priced row appeared after an unpriceable one")

    def test_missing_fx_does_not_remove_the_candidate_from_count_or_date_sort(self) -> None:
        rows = [
            ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
             "Seed GBP", "500.00", "0.00", "GBP", "COMPLETED", "500.00"],
        ]
        database = _ledger(rows)
        page = _page(
            database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31", sort="date",
        )
        self.assertEqual(page.total, 1)
        self.assertEqual(len(page.rows), 1)
        self.assertIsNotNone(page.rows[0].amount_valuation.unavailable)


class CursorAndScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger(_MIXED_ROWS)
        _load_rate(self.database, "USD", "2026-01", "1.3600")

    def test_changed_filters_reject_the_old_cursor(self) -> None:
        page = _page(
            self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
            sort="amount", limit=2,
        )
        self.assertIsNotNone(page.next_cursor)
        with self.assertRaises(FinCLError):
            _page(
                self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
                sort="amount", limit=2, cursor=page.next_cursor, search="groceries",
            )

    def test_changed_sort_rejects_the_old_cursor(self) -> None:
        page = _page(
            self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
            sort="amount", limit=2,
        )
        with self.assertRaises(FinCLError):
            _page(
                self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
                sort="date", limit=2, cursor=page.next_cursor,
            )

    def test_malformed_cursor_is_rejected(self) -> None:
        with self.assertRaises(FinCLError):
            _page(
                self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
                cursor="not-a-real-cursor",
            )

    def test_reporting_policy_change_leaves_native_count_unchanged_but_invalidates_cursor(self) -> None:
        page = _page(
            self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
            sort="amount", limit=2,
        )
        total_before = page.total
        _set_reporting_currency(self.database, "USD")
        page_after = _page(
            self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
            sort="amount", limit=2,
        )
        self.assertEqual(page_after.total, total_before)
        self.assertEqual(page_after.context.reporting_currency, "USD")
        with self.assertRaises(FinCLError):
            _page(
                self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
                sort="amount", limit=2, cursor=page.next_cursor,
            )


class AnchorsAndSupersededTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger(_MIXED_ROWS)
        _load_rate(self.database, "USD", "2026-01", "1.3600")

    def test_all_matching_anchors_match_the_full_unpaginated_candidate_set(self) -> None:
        page = _page(
            self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31", limit=200,
        )
        anchors_result = _anchors(
            self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
        )
        self.assertEqual(set(anchors_result.anchors), {row.entry["anchor"] for row in page.rows})
        self.assertFalse(anchors_result.truncated)
        self.assertEqual(anchors_result.cap, 200)

    def test_superseded_extraction_is_excluded_from_list_count_and_anchors(self) -> None:
        # `selection.recompute_selection` (invoked by every `refresh_ledger`,
        # through `categorize`) is a pure function of each run's evidence blob
        # and (profile, parser, policy) triple -- a manual selection_state
        # edit alone would just get recomputed back. A genuinely superseded
        # run is one whose triple predates the current code (`selection.
        # CURRENT_EXTRACTION_TRIPLES`); insert one directly, exactly the shape
        # `recompute_selection` demotes to SUPERSEDED on the next refresh.
        page_before = _page(
            self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31", limit=200,
        )
        self.assertEqual(page_before.total, len(_MIXED_ROWS))

        connection = connect_sqlite(self.database)
        try:
            account_id = connection.execute("SELECT account_id FROM ledger_entries LIMIT 1").fetchone()[0]
            sha = "b" * 64
            connection.execute(
                """
                INSERT INTO source_content(
                    sha256, byte_size, media_type, evidence_key, storage_kind,
                    original_path, last_observed_path, first_seen_at, last_seen_at,
                    integrity_status
                ) VALUES (?, 1, 'application/pdf', 'old-key', 'PLAINTEXT', 'x', 'x', '2026-01-01T00:00:00Z',
                          '2026-01-01T00:00:00Z', 'VERIFIED')
                """,
                (sha,),
            )
            connection.execute(
                """
                INSERT INTO extraction_runs(
                    id, evidence_sha256, source_profile_version, parser_version,
                    extraction_policy_version, started_at, completed_at, status
                ) VALUES ('ext_old_version', ?, 'revolut-pdf-en-sg-OLD', 'old-parser', 'old-policy',
                          '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'ACCEPTED')
                """,
                (sha,),
            )
            connection.execute(
                """
                INSERT INTO statements(
                    id, extraction_run_id, evidence_sha256, account_id, institution, source_type,
                    statement_date, period_start, period_end, statement_identity,
                    selection_state, conflict_state
                ) VALUES ('stmt_old', 'ext_old_version', ?, ?, 'Revolut', 'REVOLUT_PDF',
                          '2026-01-31', '2026-01-01', '2026-01-31', 'old-statement-identity',
                          'SELECTED', 'NONE')
                """,
                (sha, account_id),
            )
            connection.execute(
                """
                INSERT INTO document_records(
                    id, extraction_run_id, evidence_sha256, statement_id,
                    source_profile_version, parser_version, source_anchor,
                    extraction_ordinal, page_number, record_kind, raw_text,
                    raw_fields_json, confidence, required_fields_complete
                ) VALUES ('doc_old', 'ext_old_version', ?, 'stmt_old', 'revolut-pdf-en-sg-OLD',
                          'old-parser', 'anchor-old', 1, 1, 'TRANSACTION', 'old evidence', '{}', 'HIGH', 1)
                """,
                (sha,),
            )
            connection.execute(
                """
                INSERT INTO ledger_entries(
                    id, source_family, extraction_run_id, evidence_sha256, document_record_id,
                    statement_id, natural_anchor, account_id, currency_pocket, effective_at,
                    amount_text, amount_coefficient, amount_scale, event_kind, economic_direction,
                    lifecycle_state, analytics_eligible
                ) VALUES ('entry_old', 'DOCUMENT', 'ext_old_version', ?, 'doc_old', 'stmt_old',
                          'anchor-old', ?, 'SGD', '2026-01-15', '-9.00', -900, 2, 'OTHER', 'OUT',
                          'COMPLETED', 1)
                """,
                (sha, account_id),
            )
            connection.commit()
        finally:
            connection.close()
        refresh_ledger(self.database)

        connection = connect_sqlite(self.database)
        try:
            state = connection.execute(
                "SELECT selection_state FROM extraction_runs WHERE id = 'ext_old_version'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(state, "SUPERSEDED")

        page_after = _page(
            self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31", limit=200,
        )
        self.assertEqual(page_after.total, len(_MIXED_ROWS))
        self.assertTrue(all(row.entry["anchor"] != "anchor-old" for row in page_after.rows))

        anchors_result = _anchors(
            self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31",
        )
        self.assertNotIn("anchor-old", anchors_result.anchors)
        self.assertEqual(len(anchors_result.anchors), len(_MIXED_ROWS))


class SingleEntryDetailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger(_MIXED_ROWS)
        _load_rate(self.database, "USD", "2026-01", "1.3600")

    def _an_anchor(self) -> str:
        page = _page(self.database, as_of=date(2026, 2, 1), history_start="2026-01-01", history_end="2026-01-31")
        return page.rows[0].entry["anchor"]

    def test_detail_matches_the_row_from_the_list(self) -> None:
        anchor = self._an_anchor()
        row = _detail(self.database, as_of=date(2026, 2, 1), anchor=anchor)
        self.assertEqual(row.entry["anchor"], anchor)
        self.assertIsNotNone(row.statement_detail)

    def test_anchor_url_encoding_round_trips(self) -> None:
        anchor = self._an_anchor()
        encoded = encode_anchor(anchor)
        self.assertEqual(decode_anchor(encoded), anchor)

    def test_read_only_connection_performs_no_write(self) -> None:
        anchor = self._an_anchor()
        connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            before = connection.execute("SELECT count(*) FROM ledger_entries").fetchone()[0]
            row = reported_transaction_detail_from(connection, as_of=date(2026, 2, 1), anchor=anchor)
            after = connection.execute("SELECT count(*) FROM ledger_entries").fetchone()[0]
            self.assertEqual(before, after)
            self.assertEqual(row.entry["anchor"], anchor)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("UPDATE ledger_entries SET amount_text = 'x'")
        finally:
            connection.close()

    def test_unknown_anchor_raises(self) -> None:
        with self.assertRaises(FinCLError):
            _detail(self.database, as_of=date(2026, 2, 1), anchor="does-not-exist")


class ApiRouteTests(ApiTestBase):
    """Route wiring only -- backend behavior is covered above directly."""

    def setUp(self) -> None:
        super().setUp()
        self.bootstrap()

    def test_reported_transactions_route_returns_all_pockets(self) -> None:
        response = self.client.get(
            "/api/v1/reporting/transactions",
            params={"history_start": "2026-01-01", "history_end": "2026-01-31"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total"], 4)
        self.assertEqual(len(body["rows"]), 4)
        first = body["rows"][0]
        self.assertIn("amount", first)
        self.assertIn("statement_detail", first)
        self.assertIn("entry", first)

    def test_reported_transaction_anchors_route(self) -> None:
        response = self.client.get(
            "/api/v1/reporting/transaction-anchors",
            params={"history_start": "2026-01-01", "history_end": "2026-01-31"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["anchors"]), 4)
        self.assertFalse(body["truncated"])

    def test_reported_transaction_detail_route_round_trips_the_encoded_anchor(self) -> None:
        listing = self.client.get(
            "/api/v1/reporting/transactions",
            params={"history_start": "2026-01-01", "history_end": "2026-01-31"},
        ).json()
        native_anchor = listing["rows"][0]["entry"]["anchor"]
        encoded = encode_anchor(native_anchor)
        response = self.client.get(f"/api/v1/reporting/transactions/{encoded}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["entry"]["anchor"], native_anchor)

    def test_stale_generation_is_a_409(self) -> None:
        response = self.client.get(
            "/api/v1/reporting/transactions",
            params={"history_start": "2026-01-01", "history_end": "2026-01-31", "generation": "not-the-real-one"},
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_legacy_transactions_route_is_unaffected(self) -> None:
        generation = self.client.get("/api/v1/reporting-currency").json()["generation"]
        response = self.client.get(
            "/api/v1/transactions",
            params={"generation": generation, "start": "2026-01-01", "end": "2026-01-31"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        # Legacy default scope is one pocket (SGD); unchanged by this step.
        self.assertTrue(all(row["currency"] == "SGD" for row in response.json()["entries"]))


if __name__ == "__main__":
    unittest.main()
