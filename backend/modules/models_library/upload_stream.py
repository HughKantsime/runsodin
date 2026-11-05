"""Bounded upload staging, with cleanup on rejection and cancellation."""
import hashlib
import os
import tempfile
from contextlib import asynccontextmanager
from fastapi import HTTPException
from core.print_upload_limits import CHUNK_BYTES, MAX_UPLOAD_BYTES, size_refusal


async def write_upload(upload, suffix):
    path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as destination:
            path = destination.name
            total = 0
            digest = hashlib.sha256()
            while True:
                chunk = await upload.read(min(CHUNK_BYTES, MAX_UPLOAD_BYTES - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=size_refusal(total))
                destination.write(chunk)
                digest.update(chunk)
        return path, digest.hexdigest()
    except BaseException:
        if path is not None:
            os.unlink(path)
        raise


@asynccontextmanager
async def staged_upload(upload, suffix):
    path, digest = await write_upload(upload, suffix)
    try:
        yield path, digest
    finally:
        os.unlink(path)
