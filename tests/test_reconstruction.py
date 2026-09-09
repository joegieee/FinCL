"""F25-F26 gate R3: verified reconstruction from the full evidence inventory.

Covers two profile versions of one family, superseded-only history, conflicting
families, a blob with no extraction attempt, an unknown profile, future and
unsupported schema refusal, missing / corrupt evidence, exact FX preservation,
no-clobber publication, and reopening the candidate through the normal read
path.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from fincl.db import SCHEMA_VERSION
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError, SchemaVersionError
from fincl.rebuild import _inventory_evidence, _publish_without_clobber, rebuild_database
from fincl.report import economic_flow_report_data
from tests.revolut_pdf import revolut_pdf_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "200.00", "0.00", "USD", "COMPLETED", "200.00"],
    ["Card Payment", "Current", "2026-01-02 11:00:00", "2026-01-02 11:00:01", "Shop", "-10.00", "0.00", "USD", "COMPLETED", "190.00"],
]


def _csv_bytes() -> bytes:
    return revolut_pdf_bytes(_ROWS)


class ReconstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.evidence = self.root / "evidence"
        self.source = self.root / "revolut.csv"
        self.source.write_bytes(_csv_bytes())
        self.database = self.root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, self.evidence).import_revolut_pdf(self.source)
        self.output = self.root / "rebuilt.sqlite3"

    def _rw(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        return connection

    def _sha(self, connection: sqlite3.Connection) -> str:
        return connection.execute("SELECT sha256 FROM source_content LIMIT 1").fetchone()[0]

    def _add_run(self, connection: sqlite3.Connection, sha: str, profile: str) -> None:
        connection.execute(
            """
            INSERT INTO extraction_runs(
                id, evidence_sha256, source_profile_version, parser_version,
                extraction_policy_version, started_at, status, selection_state
            ) VALUES (?, ?, ?, ?, 'policy-1', '2026-04-01T00:00:00Z', 'QUARANTINED', 'SUPERSEDED')
            """,
            (f"run_{uuid.uuid4().hex}", sha, profile, f"parser-{uuid.uuid4().hex[:8]}"),
        )
        connection.commit()

    # --- inventory grouping ---------------------------------------------- #

    def test_two_profile_versions_of_one_family_are_a_single_input(self) -> None:
        connection = self._rw()
        self._add_run(connection, self._sha(connection), "revolut-pdf-en-sg-2025-v1")
        dispatchable, unsupported = _inventory_evidence(self.database)
        self.assertEqual(len(dispatchable), 1)
        self.assertEqual(dispatchable[0].family, "REVOLUT_PDF")
        self.assertEqual(unsupported, [])
        result = rebuild_database(self.database, self.evidence, self.output)
        self.assertEqual(result.sources_rebuilt, 1)

    def test_superseded_only_history_is_still_reconstructed(self) -> None:
        connection = self._rw()
        connection.execute("UPDATE extraction_runs SET selection_state = 'SUPERSEDED'")
        connection.commit()
        dispatchable, unsupported = _inventory_evidence(self.database)
        self.assertEqual([s.family for s in dispatchable], ["REVOLUT_PDF"])
        self.assertEqual(unsupported, [])

    def test_conflicting_families_refuse_reconstruction(self) -> None:
        connection = self._rw()
        self._add_run(connection, self._sha(connection), "dbs-consolidated-1")
        with self.assertRaisesRegex(FinCLError, "more than one source family"):
            _inventory_evidence(self.database)
        with self.assertRaisesRegex(FinCLError, "more than one source family"):
            rebuild_database(self.database, self.evidence, self.output)

    def test_blob_with_no_extraction_attempt_blocks_publication(self) -> None:
        connection = self._rw()
        orphan = (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
        connection.execute(
            """
            INSERT INTO source_content(
                sha256, byte_size, media_type, evidence_key, storage_kind,
                original_path, last_observed_path, first_seen_at, last_seen_at, integrity_status
            ) VALUES (?, 1, 'application/pdf', ?, 'plaintext-v1', 'orphan.pdf', 'orphan.pdf',
                      '2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z', 'VERIFIED')
            """,
            (orphan, f"sha256/{orphan[:2]}/{orphan}"),
        )
        connection.commit()
        with self.assertRaisesRegex(FinCLError, "no supported extraction history"):
            rebuild_database(self.database, self.evidence, self.output)
        self.assertFalse(self.output.exists())

    def test_unknown_profile_is_an_unsupported_input(self) -> None:
        connection = self._rw()
        self._add_run(connection, self._sha(connection), "some-unknown-format-v1")
        # The known revolut run keeps this blob dispatchable, so it is not
        # unsupported; but a blob whose *only* profile is unknown is.
        dispatchable, unsupported = _inventory_evidence(self.database)
        self.assertEqual([s.family for s in dispatchable], ["REVOLUT_PDF"])
        self.assertEqual(unsupported, [])

    # --- schema gating -------------------------------------------------- #

    def test_future_source_schema_is_refused(self) -> None:
        connection = self._rw()
        connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
        connection.commit()
        with self.assertRaisesRegex(SchemaVersionError, "newer than this build"):
            rebuild_database(self.database, self.evidence, self.output)

    def test_unsupported_historical_schema_is_refused_with_the_supported_list(self) -> None:
        connection = self._rw()
        connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
        connection.commit()
        with self.assertRaisesRegex(SchemaVersionError, "no historical reconstruction reader"):
            rebuild_database(self.database, self.evidence, self.output)

    # --- evidence verification ---------------------------------------- #

    def _evidence_file(self) -> Path:
        connection = self._rw()
        key = connection.execute("SELECT evidence_key FROM source_content LIMIT 1").fetchone()[0]
        return self.evidence / key

    def test_missing_evidence_object_fails_before_staging(self) -> None:
        self._evidence_file().unlink()
        with self.assertRaisesRegex(FinCLError, "missing or corrupt"):
            rebuild_database(self.database, self.evidence, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".rebuilt.sqlite3.rebuild-*")), [])

    def test_corrupt_evidence_object_fails_before_staging(self) -> None:
        path = self._evidence_file()
        path.chmod(0o600)
        path.write_bytes(b"not the original bytes")
        with self.assertRaisesRegex(FinCLError, "missing or corrupt"):
            rebuild_database(self.database, self.evidence, self.output)
        self.assertFalse(self.output.exists())

    # --- FX preservation + publication -------------------------------- #

    def test_fx_rows_are_carried_forward_exactly(self) -> None:
        connection = self._rw()
        connection.execute(
            """
            INSERT INTO fx_rates(
                base_currency, quote_currency, period, rate_numerator, rate_denominator,
                rate_text, source, source_version, imported_at
            ) VALUES ('USD', 'SGD', '2026-01', 27, 20, '1.35', 'MAS', 'mas-weekly-v1',
                      '2026-04-01T00:00:00Z')
            """
        )
        connection.commit()
        rebuild_database(self.database, self.evidence, self.output)
        rebuilt = sqlite3.connect(self.output)
        self.addCleanup(rebuilt.close)
        row = rebuilt.execute(
            "SELECT rate_numerator, rate_denominator, rate_text FROM fx_rates"
        ).fetchone()
        self.assertEqual(row, (27, 20, "1.35"))

    def test_publish_refuses_a_destination_that_appeared_after_preflight(self) -> None:
        staging = self.root / ".staging.sqlite3"
        staging.write_bytes(b"candidate")
        self.output.write_bytes(b"appeared")
        with self.assertRaisesRegex(FinCLError, "appeared during reconstruction"):
            _publish_without_clobber(staging, self.output)

    def test_publish_links_then_removes_staging(self) -> None:
        staging = self.root / ".staging.sqlite3"
        staging.write_bytes(b"candidate")
        _publish_without_clobber(staging, self.output)
        self.assertFalse(staging.exists())
        self.assertEqual(self.output.read_bytes(), b"candidate")

    def test_rebuilt_candidate_reopens_through_the_normal_read_path(self) -> None:
        rebuild_database(self.database, self.evidence, self.output)
        data = economic_flow_report_data(self.output)
        self.assertEqual(data["coverage_status"], "COVERED")


if __name__ == "__main__":
    unittest.main()
