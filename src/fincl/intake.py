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
    if adapter not in ADAPTERS:
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
        service = DocumentImportService.plaintext_phase2(database, evidence_directory)
        method = {
            "DBS_CONSOLIDATED": service.import_dbs,
            "DBS_PAYLAH": service.import_paylah,
            "MARI_SAVINGS": service.import_maribank,
            "MARI_CARD": service.import_mari_card,
            "TRUST_SAVINGS": service.import_trust,
            "REVOLUT_PDF": service.import_revolut_pdf,
        }[adapter]
        result = method(path, writer=writer)
        return {**asdict(result), "filename": Path(filename).name, "adapter": adapter}
    finally:
        path.unlink(missing_ok=True)
