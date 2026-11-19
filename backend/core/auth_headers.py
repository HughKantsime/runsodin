"""Select the request credential without authenticating it."""
import hmac
import os

from fastapi.security.utils import get_authorization_scheme_param

from core.config import settings


def bearer_token(request) -> str | None:
    scheme, value = get_authorization_scheme_param(request.headers.get("Authorization"))
    return value if scheme.lower() == "bearer" else None


def scoped_token(request, bearer: str | None = None) -> str | None:
    """X-API-Key wins when both headers carry a personal token."""
    value = request.headers.get("X-API-Key") or bearer or bearer_token(request)
    return value if value and value.startswith("odin_") else None


def jwt_bearer(request, bearer: str | None) -> str | None:
    if not bearer or bearer.startswith("odin_"):
        return None
    key = request.headers.get("X-API-Key", "")
    # A client can send the configured perimeter key alongside its session JWT.
    if key:
        perimeter_key = os.getenv("API_KEY") or settings.api_key or ""
        if not hmac.compare_digest(key.encode(), perimeter_key.encode()):
            return None
    return bearer
