"""Fixed print-file limits shared by upload entry points."""
from core.print_file_limits import MAX_COMPRESSED_BYTES as MAX_UPLOAD_BYTES, MIB

CHUNK_BYTES = 1024 * 1024
MULTIPART_FRAMING_BYTES = 1024 * 1024


class InvalidContentLengthError(ValueError):
    """Content-Length header is not a valid non-negative integer."""


class UploadContentLengthExceeded(ValueError):
    """Declared Content-Length exceeds the upload limit plus multipart framing."""


def check_upload_content_length(content_length, *, max_upload_bytes=MAX_UPLOAD_BYTES):
    """Validate Content-Length before reading a multipart body.

    Returns the parsed byte count, or None when no header was sent. Raises
    InvalidContentLengthError on malformed values and UploadContentLengthExceeded
    when declared length exceeds ``max_upload_bytes + MULTIPART_FRAMING_BYTES``.
    """
    if content_length is None or content_length == "":
        return None
    try:
        parsed = int(content_length)
    except (TypeError, ValueError) as exc:
        raise InvalidContentLengthError("Invalid Content-Length") from exc
    if parsed < 0:
        raise InvalidContentLengthError("Invalid Content-Length")
    limit = max_upload_bytes + MULTIPART_FRAMING_BYTES
    if parsed > limit:
        raise UploadContentLengthExceeded(
            f"Multipart upload too large ({parsed} bytes; limit {limit} bytes including framing)"
        )
    return parsed


def size_refusal(size, limit=MAX_UPLOAD_BYTES):
    return f'File too large ({size} bytes received; limit {limit} bytes / {limit // MIB} MiB)'
