from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator

import pytest


def pytest_configure(config: pytest.Config) -> None:
    # Workers are CPU-bound for minutes; let the editor and browser run first.
    if hasattr(config, "workerinput"):
        os.nice(10)


@pytest.fixture(scope="session", autouse=True)
def _session_tempdir() -> Iterator[None]:
    # Many ledger helpers call tempfile.mkdtemp() and hand back only the database
    # path, so nothing removes the directory. /tmp is RAM-backed here; root every
    # temporary file in one per-worker directory and drop it when the session ends.
    root = tempfile.mkdtemp(prefix="fincl-tests-")
    previous = tempfile.tempdir
    tempfile.tempdir = root
    try:
        yield
    finally:
        tempfile.tempdir = previous
        shutil.rmtree(root, ignore_errors=True)
