"""Bounded upload staging, with cleanup on rejection and cancellation."""
import hashlib
import os
import shutil
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from fastapi import HTTPException, Request
from fastapi.dependencies.utils import get_dependant, solve_dependencies
from fastapi.routing import APIRoute
from starlette.formparsers import MultiPartException

from core.print_file_limits import DISK_CHECK_THRESHOLD, MAX_COMPRESSED_BYTES as MAX_UPLOAD_BYTES
from core.print_upload_limits import (
    CHUNK_BYTES,
    MULTIPART_FRAMING_BYTES,
    InvalidContentLengthError,
    UploadContentLengthExceeded,
    check_upload_content_length,
    size_refusal,
)
from core.storage_space import existing_storage_directory
from modules.models_library.upload_storage import print_file_storage_root


def precheck_upload_request(request, *, max_upload_bytes=None):
    """Refuse oversized multipart requests on Content-Length before reading the body."""
    limit = MAX_UPLOAD_BYTES if max_upload_bytes is None else max_upload_bytes
    try:
        return check_upload_content_length(
            request.headers.get("content-length"), max_upload_bytes=limit
        )
    except InvalidContentLengthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except UploadContentLengthExceeded as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None


def _check_disk_space(size, storage_root):
    if size is None or size <= DISK_CHECK_THRESHOLD:
        return
    try:
        enough = all(shutil.disk_usage(existing_storage_directory(path)).free >= 4 * size
                     for path in (storage_root, tempfile.gettempdir()))
    except (OSError, RuntimeError):
        enough = False
    if not enough:
        raise HTTPException(status_code=507, detail="Not enough free disk space for this file")


class UploadPrecheckRoute(APIRoute):
    """Run Content-Length precheck before FastAPI parses multipart form bodies."""

    def get_route_handler(self):
        original = super().get_route_handler()
        if "POST" not in self.methods or not self.path.endswith(("/print-files/upload", "/prints")):
            return original

        from core.dependencies import _resolve_current_user
        credentials = get_dependant(path=self.path, call=_resolve_current_user)

        async def handler(request):
            # Resolve the same header/cookie credentials as get_current_user.
            # End the auth DB session before a potentially long multipart read.
            async with AsyncExitStack() as stack:
                auth_request = Request({**request.scope, "fastapi_inner_astack": stack,
                                        "fastapi_function_astack": stack})
                solved = await solve_dependencies(
                    request=auth_request, dependant=credentials, async_exit_stack=stack,
                    dependency_overrides_provider=request.app, embed_body_fields=False,
                )
                if solved.errors or not await _resolve_current_user(**solved.values):
                    raise HTTPException(status_code=401, detail="Not authenticated")
            declared = precheck_upload_request(request)
            root = print_file_storage_root()
            _check_disk_space(declared, root)
            received = 0
            refusal = None

            async def bounded_receive():
                nonlocal received, refusal
                message = await request.receive()
                received += len(message.get("body", b""))
                try:
                    if received > MAX_UPLOAD_BYTES + MULTIPART_FRAMING_BYTES:
                        raise HTTPException(status_code=413, detail=size_refusal(received, MAX_UPLOAD_BYTES))
                    if received > (declared or 0):
                        _check_disk_space(received, root)
                except HTTPException as exc:
                    # Starlette closes partially spooled files on multipart parse errors.
                    refusal = exc
                    raise MultiPartException(exc.detail) from None
                request.state.disk_checked_bytes = max(declared or 0, received)
                return message

            try:
                return await original(Request(request.scope, bounded_receive, request._send))
            except Exception:
                if refusal is not None:
                    raise refusal from None
                raise

        return handler


async def write_upload(upload, suffix, *, storage_root=None, disk_checked_bytes=0):
    root = storage_root if storage_root is not None else print_file_storage_root()
    known_size = getattr(upload, "size", None)
    if known_size is not None and known_size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=size_refusal(known_size, MAX_UPLOAD_BYTES))
    if known_size is not None and known_size > disk_checked_bytes:
        _check_disk_space(known_size, root)
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
                    raise HTTPException(status_code=413, detail=size_refusal(total, MAX_UPLOAD_BYTES))
                if total > max(known_size or 0, disk_checked_bytes):
                    _check_disk_space(total, root)
                destination.write(chunk)
                digest.update(chunk)
        return path, digest.hexdigest()
    except BaseException:
        if path is not None:
            os.unlink(path)
        raise


@asynccontextmanager
async def staged_upload(upload, suffix, *, storage_root=None):
    path, digest = await write_upload(upload, suffix, storage_root=storage_root)
    try:
        yield path, digest
    finally:
        os.unlink(path)
