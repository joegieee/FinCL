"""Bounded browser intake over the existing immutable-evidence importers."""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO

from .document_service import DocumentImportService
from .errors import FinCLError
from .locking import WriterContext

MAX_FILES = 20
MAX_FILE_BYTES = 50 * 1024 * 1024
ADAPTERS = frozenset(
    {
        "REVOLUT_PDF",
        "DBS_CONSOLIDATED",
        "DBS_PAYLAH",
        "MARI_SAVINGS",
        "MARI_CARD",
        "TRUST_SAVINGS",
    }
)
# Upload without naming the statement type: each parser reads the file and the
# one that accepts it is used. Nothing is guessed when that is not clear.
AUTO = "AUTO"


def detect_adapter(data: bytes) -> tuple[str | None, str]:
    """Which statement type a PDF is, from the parsers themselves.

    Every supported parser reads the bytes (they are pure; nothing is stored).
    Exactly one accepting it decides. If none accepts — a damaged or
    unbalanced statement — the parser that recognised the most of it with the
    fewest blocking problems is used only when it is a clear winner, so the
    file is imported and quarantined exactly as a manual choice would be. Two
    accepting, or no clear winner, is never resolved by a tie-break.
    """
    from .document_service import _DOCUMENT_PARSERS

    scores: list[tuple[bool, int, int, str]] = []
    for name, parse in _DOCUMENT_PARSERS.items():
        try:
            parsed = parse(data)
        except Exception:  # noqa: BLE001 - a parser refusing a foreign file is expected
            continue
        blocking = sum(1 for problem in parsed.problems if problem.severity == "BLOCKING")
        scores.append((parsed.accepted, -blocking, len(parsed.records), name))
    accepted = [score for score in scores if score[0]]
    if len(accepted) == 1:
        return accepted[0][3], "recognised"
    if len(accepted) > 1:
        return None, "it looks like more than one statement type"
    partial = sorted((score for score in scores if score[2] > 0), reverse=True)
    if partial and (len(partial) == 1 or partial[0][1:3] != partial[1][1:3]):
        return partial[0][3], "recognised, with problems"
    return None, "it doesn't match any supported statement"


def import_stream(
    database: Path,
    evidence_directory: Path,
    *,
    stream: BinaryIO,
    filename: str,
    adapter: str,
    writer: WriterContext | None = None,
) -> dict[str, object]:
    """Spool one bounded upload, then hand it to the existing importer.

    The temporary file is never authority and is removed after the importer has
    either durably captured evidence or recorded a known failure.
    """
    if adapter != AUTO and adapter not in ADAPTERS:
        raise FinCLError("Choose a supported source adapter")
    suffix = Path(filename).suffix.lower()
    if suffix != ".pdf":
        raise FinCLError("Statement intake requires a PDF file")
    descriptor, temp_name = tempfile.mkstemp(prefix="fincl-intake-", suffix=suffix)
    path = Path(temp_name)
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise FinCLError("Each imported file must be 50 MiB or smaller")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size == 0:
            raise FinCLError("The uploaded file is empty")
        detection: dict[str, object] = {}
        if adapter == AUTO:
            found, basis = detect_adapter(path.read_bytes())
            if found is None:
                return {
                    "filename": Path(filename).name, "adapter": None, "status": "UNRECOGNISED",
                    "error": f"We couldn't tell which bank this statement is from: {basis}. "
                             "Choose its type and upload it again.",
                }
            adapter, detection = found, {"detected": True, "detection": basis}
        service = DocumentImportService.plaintext_phase2(database, evidence_directory)
        method = {
            "DBS_CONSOLIDATED": service.import_dbs,
            "DBS_PAYLAH": service.import_paylah,
            "MARI_SAVINGS": service.import_maribank,
            "MARI_CARD": service.import_mari_card,
            "TRUST_SAVINGS": service.import_trust,
            "REVOLUT_PDF": service.import_revolut_pdf,
        }[adapter]
        result = method(path, writer=writer, source_name=Path(filename).name)
        return {**asdict(result), "filename": Path(filename).name, "adapter": adapter, **detection}
    finally:
        path.unlink(missing_ok=True)
