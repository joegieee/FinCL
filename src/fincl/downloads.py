"""Confined, verified evidence downloads; no browser-supplied file paths."""
import hashlib
import os
import re
import tempfile
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from .db import connect_readonly
from .generation import current_generation


def evidence_response(database: Path, root: Path, entry_id: str, expected: str) -> StreamingResponse:
    connection = connect_readonly(database)
    try:
        connection.execute('BEGIN')
        generation = current_generation(connection)
        if generation is None or generation['generation_token'] != expected:
            raise HTTPException(409, 'STALE_GENERATION: reload the transaction before downloading')
        row = connection.execute('''SELECT x.evidence_sha256, e.source_family
            FROM ledger_entries e JOIN extraction_runs x ON x.id=e.extraction_run_id
            WHERE e.id=?''', (entry_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Source not found')
        digest, family = row['evidence_sha256'], row['source_family']
    finally:
        connection.close()
    if not re.fullmatch('[0-9a-f]{64}',digest):
        raise HTTPException(422, 'Invalid evidence identity')
    descriptors: list[int] = []
    # Closed by the streaming generator below, not this scope; no context manager fits.
    output = tempfile.SpooledTemporaryFile(max_size=8*1024*1024)  # noqa: SIM115
    try:
        descriptors.append(os.open(root.resolve(),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW))
        for component in ('sha256',digest[:2]):
            descriptors.append(os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptors[-1]))
        descriptor = os.open(digest,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=descriptors[-1])
        actual = hashlib.sha256()
        with os.fdopen(descriptor,'rb') as source:
            while chunk := source.read(1024*1024):
                actual.update(chunk)
                output.write(chunk)
        if actual.hexdigest() != digest:
            raise HTTPException(422,'Source verification failed; download withheld')
        output.seek(0)
    except BaseException:
        output.close()
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    def chunks():
        try:
            while chunk := output.read(1024*1024):
                yield chunk
        finally:
            output.close()
    extension = 'csv' if family=='CSV' else 'pdf'
    return StreamingResponse(chunks(),media_type='application/octet-stream',headers={'Content-Disposition':f'attachment; filename="statement-{digest[:12]}.{extension}"'})
