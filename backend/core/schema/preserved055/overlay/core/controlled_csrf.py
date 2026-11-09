"""Explicit non-simple header and same-origin checks for acknowledged actions."""
from fastapi import Request, HTTPException
from urllib.parse import urlsplit


def require_controlled_ack(request: Request):
    origin = request.headers.get('origin')
    from core.config import settings
    expected = urlsplit(settings.odin_public_url or str(request.url))
    if (request.headers.get('x-odin-controlled-ack') != 'true'
            or request.headers.get('sec-fetch-site') == 'cross-site'
            or origin and (urlsplit(origin).scheme, urlsplit(origin).netloc) != (expected.scheme, expected.netloc)):
        raise HTTPException(403, {'code': 'controlled_part_csrf_required'})
