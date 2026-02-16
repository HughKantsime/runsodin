"""
Shared test helpers for the O.D.I.N. test suite.

Consolidates duplicate login/header helpers across test files.
"""

import requests


def login(base_url, username, password, api_key=None):
    """Login and return JWT token, or None on failure.

    Sends form-data to /api/auth/login (FastAPI OAuth2PasswordRequestForm).
    Includes X-API-Key header if provided.
    """
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    resp = requests.post(
        f"{base_url}/api/auth/login",
        data={"username": username, "password": password},
        headers=headers,
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json()
        return data.get("access_token") or data.get("token")
    return None


def auth_headers(token):
    """Return auth headers dict with Bearer token."""
    return {"Authorization": f"Bearer {token}"}
