"""Select the request credential without authenticating it."""
from fastapi.security.utils import get_authorization_scheme_param


def bearer_token(request) -> str | None:
    scheme, value = get_authorization_scheme_param(request.headers.get("Authorization"))
    return value if scheme.lower() == "bearer" else None


def scoped_token_carrier(request) -> str:
    """Which header to inspect for a personal token before validating its value."""
    return "api_key" if request.headers.get("X-API-Key", "").startswith("odin_") else "bearer"


def scoped_token(request, bearer: str | None = None) -> str | None:
    """X-API-Key wins when both headers carry a personal token."""
    if scoped_token_carrier(request) == "api_key":
        return request.headers["X-API-Key"]
    value = bearer or bearer_token(request)
    return value if value and value.startswith("odin_") else None


def jwt_bearer(bearer: str | None) -> str | None:
    return bearer if bearer and not bearer.startswith("odin_") else None
