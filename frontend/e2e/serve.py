"""Launch a throwaway demo ledger under the real HTTP app for Playwright.

Playwright's ``webServer`` runs this. It builds an isolated synthetic ledger in a
temp directory, then serves ``fincl.api`` with a fixed bootstrap secret on
127.0.0.1:8799 so the browser test can open ``/#bootstrap=e2e-secret``. Nothing
here touches a real ledger.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import uvicorn

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from fincl.api import create_app  # noqa: E402

workdir = Path(tempfile.mkdtemp(prefix="fincl-e2e-"))
subprocess.run(
    [sys.executable, str(REPO / "scripts" / "create_demo.py"), str(workdir)],
    check=True, cwd=REPO,
)
database = workdir / "demo.sqlite3"
app = create_app(
    database, port=8799, bootstrap_secret="e2e-secret",
    evidence_dir=workdir / "evidence", demo=True,
)
uvicorn.run(app, host="127.0.0.1", port=8799, log_level="warning")
