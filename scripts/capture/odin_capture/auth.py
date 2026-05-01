"""Programmatic auth — bootstrap an admin and log in against a freshly-booted
ODIN backend, returning a session cookie suitable for installing into a
Playwright browser context.

Two modes:

  * `bootstrap_or_login`  — preferred default. Calls `GET /api/setup/status`
    first; if a fresh stack is detected (`needs_setup=true`), creates the
    capture-pipeline admin via `POST /api/setup/admin`. Then logs in via
    `POST /api/auth/login` and returns the session cookie.

  * `login`               — login-only path for an already-provisioned stack
    (e.g. the App Review reviewer account on `--target demo`).

Network surface is `urllib` only — no extra runtime dep beyond the Python
stdlib + the `requirements.txt` set already declared by Wake 1.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Optional

logger = logging.getLogger("odin_capture.auth")


# Deterministic credentials for the local capture pipeline. These never
# leave the per-run docker volume — the stack is wiped between scenes.
DEFAULT_CAPTURE_USERNAME = "capture"
DEFAULT_CAPTURE_EMAIL = "capture@local-demo"
DEFAULT_CAPTURE_PASSWORD = "Capture-Pipeline-2026!"  # noqa: S105 (per-run, ephemeral)


@dataclass
class SessionCookie:
    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = False
    http_only: bool = True

    def to_playwright_cookie(self) -> dict:
        """Shape Playwright's `BrowserContext.add_cookies` expects."""
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "secure": self.secure,
            "httpOnly": self.http_only,
            "sameSite": "Lax",
        }


class AuthError(RuntimeError):
    """Raised when setup-admin or login fails."""


def _domain_of(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    return parsed.hostname or "localhost"


def _post_form(url: str, body: dict[str, str], *, timeout: float = 10.0) -> tuple[dict, list[str]]:
    """POST x-www-form-urlencoded; return (json body, raw Set-Cookie headers)."""
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            set_cookies = resp.headers.get_all("Set-Cookie") or []
            return payload, set_cookies
    except urllib.error.HTTPError as e:
        raise AuthError(f"POST {url} failed: {e.code} {e.read().decode('utf-8', 'replace')[:300]}")


def _post_json(url: str, body: dict, *, timeout: float = 10.0) -> dict:
    """POST application/json; return parsed body."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AuthError(f"POST {url} failed: {e.code} {e.read().decode('utf-8', 'replace')[:300]}")


def _get_json(url: str, *, timeout: float = 10.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AuthError(f"GET {url} failed: {e.code} {e.read().decode('utf-8', 'replace')[:300]}")


def _extract_session_cookie(set_cookies: list[str], domain: str) -> Optional[SessionCookie]:
    """Find the `session` cookie in a list of Set-Cookie header strings."""
    for raw in set_cookies:
        jar = SimpleCookie()
        jar.load(raw)
        if "session" in jar:
            morsel = jar["session"]
            return SessionCookie(
                name="session",
                value=morsel.value,
                domain=domain,
                path=morsel["path"] or "/",
                secure=bool(morsel["secure"]),
                http_only=bool(morsel["httponly"]),
            )
    return None


def setup_admin(
    base_url: str,
    username: str = DEFAULT_CAPTURE_USERNAME,
    email: str = DEFAULT_CAPTURE_EMAIL,
    password: str = DEFAULT_CAPTURE_PASSWORD,
) -> str:
    """Create the first admin via `POST /api/setup/admin`.

    Returns the access_token from the setup response. The caller still
    needs to follow up with `login()` to get the `session` cookie that
    Playwright installs into its context.
    """
    body = {"username": username, "email": email, "password": password}
    payload = _post_json(f"{base_url}/api/setup/admin", body)
    token = payload.get("access_token")
    if not token:
        raise AuthError(f"setup/admin response missing access_token: {payload}")
    logger.info("provisioned capture admin user=%s on %s", username, base_url)
    return token


def login(
    base_url: str,
    username: str = DEFAULT_CAPTURE_USERNAME,
    password: str = DEFAULT_CAPTURE_PASSWORD,
) -> SessionCookie:
    """Login via `POST /api/auth/login` (form-encoded). Returns session cookie."""
    payload, set_cookies = _post_form(
        f"{base_url}/api/auth/login",
        {"username": username, "password": password},
    )
    token = payload.get("access_token")
    if not token:
        raise AuthError(f"login response missing access_token: {payload}")
    cookie = _extract_session_cookie(set_cookies, _domain_of(base_url))
    if cookie is None:
        # Backend always sets the session cookie on success per
        # backend/modules/organizations/routes_auth.py; if it didn't, the
        # frontend would never authenticate either.
        raise AuthError("login succeeded but no `session` cookie returned")
    logger.info("logged in user=%s on %s", username, base_url)
    return cookie


def bootstrap_or_login(
    base_url: str,
    username: str = DEFAULT_CAPTURE_USERNAME,
    email: str = DEFAULT_CAPTURE_EMAIL,
    password: str = DEFAULT_CAPTURE_PASSWORD,
) -> SessionCookie:
    """Provision the admin if the stack is fresh, then login. Idempotent."""
    status = _get_json(f"{base_url}/api/setup/status")
    if status.get("needs_setup", False):
        setup_admin(base_url, username=username, email=email, password=password)
    return login(base_url, username=username, password=password)
