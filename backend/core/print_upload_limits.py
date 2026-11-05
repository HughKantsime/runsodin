"""Fixed print-file limits shared by upload entry points."""
MAX_UPLOAD_BYTES = 160 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def size_refusal(size):
    return f'File too large ({size} bytes received; limit {MAX_UPLOAD_BYTES} bytes / 160 MiB)'
