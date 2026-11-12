"""Shared thumbnail validation: PNG/JPEG only, size and dimension capped, decoded once to prove it is an image.

Moved verbatim from modules/jobs/recent_terminal.py (behaviour unchanged) so the plate cache applies the same checks.
"""
import base64
import warnings
from io import BytesIO

from PIL import Image

MAX_THUMBNAIL_BYTES = 262144


def thumbnail_kind(content):
    """'png' or 'jpeg' when the bytes pass the thumbnail checks, else None."""
    if type(content) is not bytes or not 1 <= len(content) <= MAX_THUMBNAIL_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                kind = {'PNG': 'png', 'JPEG': 'jpeg'}.get(image.format)
                if (kind is None or not 1 <= image.width <= 1024 or not 1 <= image.height <= 1024
                        or image.width * image.height > 1048576 or getattr(image, 'n_frames', 1) != 1):
                    return None
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()
        return kind
    except (ValueError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def thumbnail_data(value):
    """data: URI for a base64 PNG/JPEG thumbnail that passes the checks, else None."""
    if type(value) is not str or not 1 <= len(value) <= 4 * ((MAX_THUMBNAIL_BYTES + 2) // 3):
        return None
    try:
        content = base64.b64decode(value, validate=True)
    except ValueError:
        return None
    kind = thumbnail_kind(content)
    if kind is None:
        return None
    return f'data:image/{kind};base64,' + base64.b64encode(content).decode('ascii')
