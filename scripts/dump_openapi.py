"""Write docs/openapi.json from the FastAPI app so the frontend DTOs stay in sync.

Run after changing any request/response model in ``fincl.api``:

    uv run python scripts/dump_openapi.py
    npm run types --prefix frontend
"""
from __future__ import annotations

import json
from pathlib import Path

from fincl.api import create_app

target = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
schema = create_app(Path("/nonexistent/ledger.sqlite3")).openapi()
target.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
print(f"wrote {target}")
