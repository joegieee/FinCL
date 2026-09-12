"""Step 06 (M3b.2): the read-only statement money normalizer.

Every case here inserts its own minimal, synthetic `ledger_entries` /
`document_records` row chain directly (no PDF rendering, no real evidence) so
each scenario can pin exactly the stored-vs-raw combination it means to
exercise. `read_statement_money_detail` is asserted to perform no write and
to leave every native field untouched.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fincl.common import canonical_json
from fincl.db import connect_sqlite, initialize_schema
from fincl.statement_money import (
    AMOUNT_BASIS_FEE_NOT_APPLICABLE,
    AMOUNT_BASIS_FEES_EXCLUDED,
    DETAIL_BASIS_DECODED,
    DETAIL_BASIS_STORED,
    DETAIL_BASIS_UNAVAILABLE,
    NORMALIZER_VERSION,
    VALIDATION_CONFLICT,
    VALIDATION_MATCHES,
    VALIDATION_MISMATCH,
    VALIDATION_NOT_CHECKABLE,
    read_statement_money_detail,
)

_SHA = "a" * 64


def _seed(
    connection: sqlite3.Connection,
    entry_id: str,
    *,
    source_type: str,
    currency_pocket: str = "SGD",
    amount_text: str = "-100.00",
    fee_text: str | None = None,
    source_currency: str | None = None,
    source_amount_text: str | None = None,
    raw_fields: dict[str, object] | None = None,
    raw_text: str = "synthetic evidence line",
    no_document_record: bool = False,
) -> None:
    """Insert one full FK chain: source_content -> extraction_runs ->
    accounts -> statements -> document_records -> ledger_entries.
    """
    now = "2026-01-01T00:00:00Z"
    sha = _SHA[:-2] + entry_id[-2:] if len(entry_id) >= 2 else _SHA
    connection.execute(
        """
        INSERT INTO source_content(
            sha256, byte_size, media_type, evidence_key, storage_kind,
            original_path, last_observed_path, first_seen_at, last_seen_at,
            integrity_status
        ) VALUES (?, 1, 'application/pdf', ?, 'PLAINTEXT', 'x', 'x', ?, ?, 'VERIFIED')
        """,
        (sha, f"key-{entry_id}", now, now),
    )
    extraction_run_id = f"ext_{entry_id}"
    connection.execute(
        """
        INSERT INTO extraction_runs(
            id, evidence_sha256, source_profile_version, parser_version,
            extraction_policy_version, started_at, completed_at, status
        ) VALUES (?, ?, 'v1', 'v1', 'v1', ?, ?, 'ACCEPTED')
        """,
        (extraction_run_id, sha, now, now),
    )
    account_id = f"acct_{entry_id}"
    connection.execute(
        """
        INSERT INTO accounts(id, institution, source_type, display_name, masked_identifier)
        VALUES (?, 'Test Bank', ?, 'Test Account', '0000')
        """,
        (account_id, source_type),
    )
    statement_id = f"stmt_{entry_id}"
    connection.execute(
        """
        INSERT INTO statements(
            id, extraction_run_id, evidence_sha256, account_id, institution,
            source_type, statement_date, period_start, period_end,
            statement_identity, selection_state, conflict_state
        ) VALUES (?, ?, ?, ?, 'Test Bank', ?, '2026-01-31', '2026-01-01', '2026-01-31', ?, 'SELECTED', 'NONE')
        """,
        (statement_id, extraction_run_id, sha, account_id, source_type, f"identity-{entry_id}"),
    )
    document_record_id = None
    raw_record_id = None
    if not no_document_record:
        document_record_id = f"doc_{entry_id}"
        connection.execute(
            """
            INSERT INTO document_records(
                id, extraction_run_id, evidence_sha256, statement_id,
                source_profile_version, parser_version, source_anchor,
                extraction_ordinal, page_number, record_kind, raw_text,
                raw_fields_json, confidence, required_fields_complete
            ) VALUES (?, ?, ?, ?, 'v1', 'v1', ?, 1, 3, 'TRANSACTION', ?, ?, 'HIGH', 1)
            """,
            (
                document_record_id,
                extraction_run_id,
                sha,
                statement_id,
                f"anchor-{entry_id}",
                raw_text,
                canonical_json(raw_fields or {}),
            ),
        )
    else:
        # A ledger entry with no linked statement evidence is a `source_family
        # = 'CSV'` row (the retired CSV import path), never a DOCUMENT row
        # with a null `document_record_id` -- the schema forbids that
        # combination outright.
        raw_record_id = f"raw_{entry_id}"
        connection.execute(
            """
            INSERT INTO raw_records(
                id, extraction_run_id, evidence_sha256, source_profile_version,
                parser_version, header_sha256, source_anchor, extraction_ordinal,
                physical_line_start, physical_line_end, record_kind,
                raw_record_text, raw_cells_json, confidence, required_fields_complete
            ) VALUES (?, ?, ?, 'v1', 'v1', 'h', ?, 1, 1, 1, 'TRANSACTION', ?, '[]', 'HIGH', 1)
            """,
            (raw_record_id, extraction_run_id, sha, f"anchor-{entry_id}", raw_text),
        )
    source_family = "CSV" if no_document_record else "DOCUMENT"
    entry_statement_id = None if no_document_record else statement_id
    amount = _split(amount_text)
    fee = _split(fee_text) if fee_text is not None else (None, None, None)
    source_amount = _split(source_amount_text) if source_amount_text is not None else (None, None, None)
    connection.execute(
        f"""
        INSERT INTO ledger_entries(
            id, source_family, extraction_run_id, evidence_sha256,
            raw_record_id, document_record_id, statement_id, natural_anchor,
            account_id, currency_pocket, effective_at, amount_text,
            amount_coefficient, amount_scale, event_kind, economic_direction,
            lifecycle_state, analytics_eligible, fee_text, fee_coefficient,
            fee_scale, source_currency, source_amount_text,
            source_amount_coefficient, source_amount_scale
        ) VALUES (
            ?, '{source_family}', ?, ?, ?, ?, ?, ?, ?, ?, '2026-01-15', ?, ?, ?, 'OTHER',
            CASE WHEN ? < 0 THEN 'OUT' WHEN ? > 0 THEN 'IN' ELSE 'ZERO' END,
            'COMPLETED', 1, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            entry_id,
            extraction_run_id,
            sha,
            raw_record_id,
            document_record_id,
            entry_statement_id,
            f"anchor-{entry_id}",
            account_id,
            currency_pocket,
            amount[0],
            amount[1],
            amount[2],
            amount[1],
            amount[1],
            fee[0],
            fee[1],
            fee[2],
            source_currency,
            source_amount[0],
            source_amount[1],
            source_amount[2],
        ),
    )
    connection.commit()


def _split(text: str) -> tuple[str, int, int]:
    sign = -1 if text.startswith("-") else 1
    digits = text.lstrip("+-")
    if "." in digits:
        whole, frac = digits.split(".")
        scale = len(frac)
    else:
        whole, frac, scale = digits, "", 0
    coefficient = sign * int(whole + frac or "0")
    return text, coefficient, scale


class StatementMoneyNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "fincl.sqlite3"
        self.connection = connect_sqlite(self.db_path)
        initialize_schema(self.connection)

    def _snapshot(self) -> tuple[int, int]:
        ledger = self.connection.execute("SELECT count(*) FROM ledger_entries").fetchone()[0]
        docs = self.connection.execute("SELECT count(*) FROM document_records").fetchone()[0]
        return ledger, docs

    def test_no_document_record_is_unavailable_not_zero(self) -> None:
        _seed(self.connection, "e1", source_type="DBS_CONSOLIDATED", no_document_record=True)
        detail = read_statement_money_detail(self.connection, "e1")
        self.assertIsNone(detail.original)
        self.assertEqual(detail.provenance.detail_basis, DETAIL_BASIS_UNAVAILABLE)
        self.assertEqual(detail.validation.status, VALIDATION_NOT_CHECKABLE)

    def test_ordinary_decimal_purchase_has_no_foreign_detail(self) -> None:
        _seed(self.connection, "e2", source_type="DBS_CONSOLIDATED", amount_text="-42.50")
        detail = read_statement_money_detail(self.connection, "e2")
        self.assertIsNone(detail.original)
        self.assertIsNone(detail.printed_conversion)
        self.assertIsNone(detail.effective_rate)
        self.assertEqual(detail.settled.amount_text, "-42.50")
        self.assertEqual(detail.provenance.detail_basis, DETAIL_BASIS_UNAVAILABLE)

    def test_stored_columns_are_preferred_and_authoritative(self) -> None:
        _seed(
            self.connection,
            "e3",
            source_type="MARI_CARD",
            amount_text="-136.00",
            source_currency="USD",
            source_amount_text="100.00",
        )
        detail = read_statement_money_detail(self.connection, "e3")
        assert detail.original is not None
        self.assertEqual(detail.original.currency, "USD")
        self.assertEqual(detail.original.amount_text, "100.00")
        self.assertEqual(detail.provenance.detail_basis, DETAIL_BASIS_STORED)
        assert detail.effective_rate is not None
        self.assertEqual(detail.effective_rate.base_currency, "SGD")
        self.assertEqual(detail.effective_rate.quote_currency, "USD")

    def test_legacy_comma_grouped_stored_original_is_tolerated_not_a_crash(self) -> None:
        """A now-fixed write-time bug (`document_service._insert_document_entry`)
        once persisted a `_parse_printed_money`-decoded foreign amount's raw
        printed spelling -- thousands separator included -- into
        `source_amount_text`, a column meant to hold a canonical
        `format_decimal` string. This module never rewrites `ledger_entries`
        (its own read-only contract), so an already-accepted legacy row still
        carries the comma; ``ExactMoney.parse`` rejects it outright, which
        previously surfaced as a 500 through the reported Transactions route
        for any ledger with such a row. It must instead render correctly."""
        _seed(
            self.connection, "e3b", source_type="MARI_CARD", amount_text="-24000.00",
            source_currency="VND", source_amount_text="120000.00",
        )
        self.connection.execute(
            "UPDATE ledger_entries SET source_amount_text = ? WHERE id = ?", ("120,000.00", "e3b"),
        )
        self.connection.commit()

        detail = read_statement_money_detail(self.connection, "e3b")

        assert detail.original is not None
        self.assertEqual(detail.original.currency, "VND")
        self.assertEqual(detail.original.amount_text, "120000.00")
        self.assertEqual(detail.provenance.detail_basis, DETAIL_BASIS_STORED)
        assert detail.effective_rate is not None
        self.assertEqual(detail.effective_rate.quote_currency, "VND")

    def test_revolut_inr_symbol_decoded_from_raw_when_stored_absent(self) -> None:
        raw_fields = {
            "fx_line": "Revolut Rate S$1.00 = ₹60.00 (ECB rate* S$1.00 = ₹59.50) ₹1,000.00",
            "balance_delta": "-16.90",
            "fee": "0.25",
        }
        _seed(
            self.connection,
            "e4",
            source_type="REVOLUT_PDF",
            currency_pocket="SGD",
            amount_text="-16.65",
            fee_text="0.25",
            raw_fields=raw_fields,
        )
        detail = read_statement_money_detail(self.connection, "e4")
        assert detail.original is not None
        self.assertEqual(detail.original.currency, "INR")
        self.assertEqual(detail.original.amount_text, "1000.00")
        self.assertEqual(detail.provenance.detail_basis, DETAIL_BASIS_DECODED)
        assert detail.printed_conversion is not None
        self.assertEqual(detail.printed_conversion.base_currency, "SGD")
        self.assertEqual(detail.printed_conversion.quote_currency, "INR")
        self.assertEqual(detail.printed_conversion.quote_amount_text, "60.00")
        assert detail.statement_cash_movement is not None
        self.assertEqual(detail.statement_cash_movement.amount_text, "-16.90")
        assert detail.effective_rate is not None
        self.assertEqual(detail.effective_rate.amount_basis, AMOUNT_BASIS_FEES_EXCLUDED)

    def test_revolut_integer_grouped_vnd_decoded(self) -> None:
        raw_fields = {
            "fx_line": "Revolut Rate $1.00 = 25,000 VND (ECB rate* $1.00 = 24,800 VND) 250,000 VND",
        }
        _seed(
            self.connection,
            "e5",
            source_type="REVOLUT_PDF",
            currency_pocket="USD",
            amount_text="-30.00",
            raw_fields=raw_fields,
        )
        detail = read_statement_money_detail(self.connection, "e5")
        assert detail.original is not None
        self.assertEqual(detail.original.currency, "VND")
        self.assertEqual(detail.original.amount_text, "250000")
        assert detail.printed_conversion is not None
        self.assertEqual(detail.printed_conversion.quote_amount_text, "25000")
        self.assertEqual(detail.effective_rate.amount_basis, AMOUNT_BASIS_FEE_NOT_APPLICABLE)  # type: ignore[union-attr]

    def test_trust_structured_foreign_amount_and_printed_rate(self) -> None:
        raw_fields = {
            "source_columns": {"amount_sgd": "-136.00", "amount_fcy": "100.00", "foreign_currency": "USD"},
            "fx": {"currency": "USD", "rate_to_sgd": "1.3600", "fx_charge_percent": "2.50"},
        }
        _seed(
            self.connection,
            "e6",
            source_type="TRUST_SAVINGS",
            amount_text="-136.00",
            source_currency="USD",
            source_amount_text="100.00",
            raw_fields=raw_fields,
        )
        detail = read_statement_money_detail(self.connection, "e6")
        assert detail.original is not None
        self.assertEqual(detail.original.amount_text, "100.00")
        self.assertEqual(detail.provenance.detail_basis, DETAIL_BASIS_STORED)
        assert detail.printed_conversion is not None
        self.assertEqual(detail.printed_conversion.base_currency, "USD")
        self.assertEqual(detail.printed_conversion.quote_currency, "SGD")
        self.assertEqual(detail.printed_charge_percent, "2.50%")
        # 100.00 USD -> 136.00 SGD exactly matches the printed 1 USD = 1.36 SGD.
        self.assertEqual(detail.validation.status, VALIDATION_MATCHES)

    def test_refund_flow_preserves_credit_direction(self) -> None:
        raw_fields = {"fx_line": "Revolut Rate S$1.00 = $0.74 (ECB rate* S$1.00 = $0.73) $74.00"}
        _seed(
            self.connection,
            "e7",
            source_type="REVOLUT_PDF",
            currency_pocket="SGD",
            amount_text="100.00",
            raw_fields=raw_fields,
        )
        detail = read_statement_money_detail(self.connection, "e7")
        self.assertEqual(detail.settled.amount_text, "100.00")
        assert detail.original is not None
        self.assertEqual(detail.original.currency, "USD")
        self.assertEqual(detail.original.amount_text, "74.00")

    def test_explicit_revolut_fee_is_kept_separate_no_universal_equation(self) -> None:
        raw_fields = {"balance_delta": "-11.00", "fee": "1.00"}
        _seed(
            self.connection,
            "e8",
            source_type="REVOLUT_PDF",
            amount_text="-10.00",
            fee_text="1.00",
            raw_fields=raw_fields,
        )
        detail = read_statement_money_detail(self.connection, "e8")
        assert detail.explicit_fee is not None
        self.assertEqual(detail.explicit_fee.amount_text, "1.00")
        assert detail.statement_cash_movement is not None
        self.assertEqual(detail.statement_cash_movement.amount_text, "-11.00")
        # No original/foreign detail on this row: no effective rate is invented.
        self.assertIsNone(detail.effective_rate)

    def test_zero_original_is_a_real_zero_not_unavailable(self) -> None:
        _seed(
            self.connection,
            "e9",
            source_type="MARI_CARD",
            amount_text="0.00",
            source_currency="USD",
            source_amount_text="0.00",
        )
        detail = read_statement_money_detail(self.connection, "e9")
        assert detail.original is not None
        self.assertEqual(detail.original.amount_text, "0.00")
        self.assertEqual(detail.provenance.detail_basis, DETAIL_BASIS_STORED)
        # A zero original yields no meaningful ratio, but that is a distinct,
        # honestly-unavailable derived fact -- the original itself stays 0.00.
        self.assertIsNone(detail.effective_rate)

    def test_benchmark_and_provider_rate_are_kept_distinct(self) -> None:
        # The ECB benchmark rate (59.50) differs from the provider rate
        # (60.00) actually applied; only the provider rate is surfaced.
        raw_fields = {"fx_line": "Revolut Rate S$1.00 = ₹60.00 (ECB rate* S$1.00 = ₹59.50) ₹1,000.00"}
        _seed(self.connection, "e10", source_type="REVOLUT_PDF", amount_text="-16.67", raw_fields=raw_fields)
        detail = read_statement_money_detail(self.connection, "e10")
        assert detail.printed_conversion is not None
        self.assertEqual(detail.printed_conversion.quote_amount_text, "60.00")
        self.assertNotIn("59.50", detail.printed_conversion.quote_amount_text)
        self.assertIn("59.50", detail.printed_conversion.raw_text)

    def test_ambiguous_decoded_field_stays_unavailable(self) -> None:
        raw_fields = {"fx_line": "Revolut Rate S$1.00 = 60.00 XYZ (ECB rate* S$1.00 = 59.50 XYZ) garbled"}
        _seed(self.connection, "e11", source_type="REVOLUT_PDF", amount_text="-40.00", raw_fields=raw_fields)
        detail = read_statement_money_detail(self.connection, "e11")
        self.assertIsNone(detail.original)
        self.assertEqual(detail.provenance.detail_basis, DETAIL_BASIS_UNAVAILABLE)
        self.assertEqual(detail.validation.status, VALIDATION_NOT_CHECKABLE)

    def test_conflicting_stored_and_decoded_fields_are_disclosed_not_replaced(self) -> None:
        raw_fields = {"fx_line": "Revolut Rate S$1.00 = ₹60.00 (ECB rate* S$1.00 = ₹59.50) ₹1,100.00"}
        _seed(
            self.connection,
            "e12",
            source_type="REVOLUT_PDF",
            amount_text="-16.67",
            source_currency="INR",
            source_amount_text="1000.00",
            raw_fields=raw_fields,
        )
        detail = read_statement_money_detail(self.connection, "e12")
        assert detail.original is not None
        # The stored value is kept exactly -- never silently replaced.
        self.assertEqual(detail.original.amount_text, "1000.00")
        self.assertEqual(detail.provenance.detail_basis, DETAIL_BASIS_STORED)
        self.assertEqual(detail.validation.status, VALIDATION_CONFLICT)
        assert detail.validation.reason is not None
        self.assertIn("1100.00", detail.validation.reason)

    def test_printed_rate_mismatch_beyond_rounding_is_flagged(self) -> None:
        raw_fields = {
            "source_columns": {"amount_fcy": "100.00", "foreign_currency": "USD"},
            "fx": {"currency": "USD", "rate_to_sgd": "1.5000", "fx_charge_percent": "0"},
        }
        _seed(
            self.connection,
            "e13",
            source_type="TRUST_SAVINGS",
            amount_text="-136.00",
            source_currency="USD",
            source_amount_text="100.00",
            raw_fields=raw_fields,
        )
        detail = read_statement_money_detail(self.connection, "e13")
        self.assertEqual(detail.validation.status, VALIDATION_MISMATCH)

    def test_no_writes_and_native_fields_unchanged(self) -> None:
        raw_fields = {"fx_line": "Revolut Rate S$1.00 = ₹60.00 (ECB rate* S$1.00 = ₹59.50) ₹1,000.00"}
        _seed(self.connection, "e14", source_type="REVOLUT_PDF", amount_text="-16.65", raw_fields=raw_fields)
        before = self._snapshot()
        before_row = self.connection.execute(
            "SELECT amount_text, source_currency, source_amount_text FROM ledger_entries WHERE id='e14'"
        ).fetchone()
        read_statement_money_detail(self.connection, "e14")
        after = self._snapshot()
        after_row = self.connection.execute(
            "SELECT amount_text, source_currency, source_amount_text FROM ledger_entries WHERE id='e14'"
        ).fetchone()
        self.assertEqual(before, after)
        self.assertEqual(tuple(before_row), tuple(after_row))
        self.assertIsNone(after_row["source_currency"])

    def test_read_only_connection_cannot_write(self) -> None:
        raw_fields = {"fx_line": "Revolut Rate S$1.00 = $0.74 (ECB rate* S$1.00 = $0.73) $74.00"}
        _seed(self.connection, "e15", source_type="REVOLUT_PDF", amount_text="-70.00", raw_fields=raw_fields)
        self.connection.close()
        readonly = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        readonly.row_factory = sqlite3.Row
        readonly.execute("PRAGMA query_only = ON")
        detail = read_statement_money_detail(readonly, "e15")
        self.assertIsNotNone(detail.original)
        with self.assertRaises(sqlite3.OperationalError):
            readonly.execute("UPDATE ledger_entries SET amount_text = 'x' WHERE id = 'e15'")
        readonly.close()

    def test_provenance_carries_normalizer_version_and_location(self) -> None:
        _seed(self.connection, "e16", source_type="MARI_CARD", amount_text="-5.00")
        detail = read_statement_money_detail(self.connection, "e16")
        self.assertEqual(detail.provenance.normalizer_version, NORMALIZER_VERSION)
        self.assertEqual(detail.provenance.document_record_id, "doc_e16")
        self.assertEqual(detail.provenance.source_location, "Page 3")

    def test_unknown_ledger_entry_raises(self) -> None:
        from fincl.errors import FinCLError

        with self.assertRaises(FinCLError):
            read_statement_money_detail(self.connection, "does-not-exist")


if __name__ == "__main__":
    unittest.main()
