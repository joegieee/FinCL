"""docs/openapi.json is the contract the frontend types are generated from."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from fincl.api import create_app


class OpenApiContractTests(unittest.TestCase):
    def test_committed_schema_matches_the_app(self) -> None:
        committed = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
        schema = create_app(Path("/nonexistent/ledger.sqlite3")).openapi()
        self.assertEqual(
            committed.read_text(encoding="utf-8"),
            json.dumps(schema, indent=2) + "\n",
            "Run scripts/dump_openapi.py, then npm run types --prefix frontend",
        )
