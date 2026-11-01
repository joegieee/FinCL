"""Every read the app serves, compared with its recorded response.

The demo ledger is rebuilt for a fixed date, and each GET is compared with its
reference under ``tests/snapshots/api``, so a change no test thought about still
shows up: a dropped field, a reordered list, a reformatted amount. An intended
change is reviewed in the diff of the reference files:

    UPDATE_API_SNAPSHOTS=1 uv run pytest tests/test_api_snapshots.py -n0
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from fincl import clock
from fincl.api import create_app
from tests.test_api import _ORIGIN, ASGITestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from create_demo import create_demo  # noqa: E402

REFERENCES = Path(__file__).resolve().parent / "snapshots" / "api"
THROUGH = date(2026, 8, 31)
TODAY = date(2026, 9, 15)
PERIOD = "history_start=2026-08-01&history_end=2026-08-31"

# The queries the screens send, for routes that need one or default to a far
# larger response than any screen asks for.
QUERIES = {
    "/api/v1/overview": "currency=SGD",
    "/api/v1/money-flow": PERIOD,
    "/api/v1/reporting/analysis": PERIOD,
    "/api/v1/reporting/transactions": PERIOD,
    "/api/v1/spending-controls": "include_forecast=false",
    "/api/v1/reporting/workspaces/spending-controls": "include_forecast=false",
}
SKIPPED = {"/api/v1/health"}  # carries this process's CSRF token

# Values that differ between two builds of the same ledger. These match on the
# shape of the value, not the field's name, so no amount or date can hide here.
VOLATILE = (
    (re.compile(r"sha256:[0-9a-f]{64}"), "<digest>"),
    (re.compile(r"journal:\d+:[0-9a-f]{64}"), "<digest>"),
    (re.compile(r"\b[0-9a-f]{64}\b"), "<digest>"),
    (re.compile(r"\b(?:c|imp|cat)_[0-9a-f]{16,}\b"), "<id>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+\+00:00"), "<recorded-at>"),
    (re.compile(r"^eyJ[A-Za-z0-9+/=_-]{32,}$"), "<handle>"),
)


def _masked(value: Any, root: str) -> Any:
    if isinstance(value, dict):
        return {_masked(key, root): _masked(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item, root) for item in value]
    if isinstance(value, str):
        value = value.replace(root, "<root>")
        for pattern, replacement in VOLATILE:
            value = pattern.sub(replacement, value)
    return value


def _render(value: Any, depth: int = 0) -> str:
    """JSON with one line per object below the top levels, which keeps diffs small."""
    if depth >= 6 or not isinstance(value, dict | list) or not value:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    indent = " " * (depth + 1)
    if isinstance(value, dict):
        items = [f"{indent}{json.dumps(key)}: {_render(item, depth + 1)}" for key, item in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + " " * depth + "}"
    items = [indent + _render(item, depth + 1) for item in value]
    return "[\n" + ",\n".join(items) + "\n" + " " * depth + "]"


def _reference(path: str) -> Path:
    return REFERENCES / (path.removeprefix("/api/v1/").replace("/", "-") + ".json")


class ApiSnapshotTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.root = str(Path(temporary.name).resolve())
        cls.addClassCleanup(clock.pin, None)
        clock.pin(TODAY)
        database = create_demo(Path(cls.root), through=THROUGH)
        cls.app = create_app(
            database, port=8765, bootstrap_secret="test-secret", require_access_key=True,
            evidence_dir=Path(cls.root) / "evidence", today=TODAY,
        )
        cls.client = ASGITestClient(cls.app, base_url=_ORIGIN)
        cls.addClassCleanup(cls.client.close)
        cls.client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": _ORIGIN, "content-type": "application/json"},
        )

    def _paths(self) -> list[str]:
        paths = []
        for path, operations in self.app.openapi()["paths"].items():
            required = any(
                parameter.get("required") for parameter in operations.get("get", {}).get("parameters", [])
            )
            if "get" in operations and "{" not in path and path not in SKIPPED and (
                path in QUERIES or not required
            ):
                paths.append(path)
        return sorted(paths)

    def _response(self, path: str) -> str:
        query = QUERIES.get(path)
        response = self.client.get(f"{path}?{query}" if query else path, headers={"origin": _ORIGIN})
        self.assertEqual(response.status_code, 200, path)
        return _render(_masked(response.json(), self.root)) + "\n"

    def test_every_read_matches_its_reference(self) -> None:
        paths = self._paths()
        if os.environ.get("UPDATE_API_SNAPSHOTS"):
            REFERENCES.mkdir(parents=True, exist_ok=True)
            for stale in REFERENCES.glob("*.json"):
                stale.unlink()
            for path in paths:
                _reference(path).write_text(self._response(path), encoding="utf-8")
        changed = {}
        for path in paths:
            reference = _reference(path)
            self.assertTrue(reference.exists(), f"No reference for {path}: see this file's docstring")
            expected = reference.read_text(encoding="utf-8")
            actual = self._response(path)
            if actual != expected:
                changed[path] = difflib.unified_diff(
                    expected.splitlines(), actual.splitlines(), "reference", "response", lineterm="", n=0,
                )
        if changed:
            first, diff = next(iter(changed.items()))
            sample = "\n".join(line[:200] for _, line in zip(range(30), diff, strict=False))
            self.fail(f"{len(changed)} reads changed: {', '.join(changed)}\n\n{first}:\n{sample}")

    def test_no_reference_outlives_its_route(self) -> None:
        expected = {_reference(path).name for path in self._paths()}
        self.assertEqual({file.name for file in REFERENCES.glob("*.json")}, expected)
