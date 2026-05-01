"""Programmatic auth — login as the App Review demo account, return a JWT
session cookie suitable for handing to a Playwright browser context.

Wake 2 deliverable: implements POST /api/v1/auth/login against the local
or demo target, captures the `session` cookie, and exposes it to browser.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionCookie:
    name: str
    value: str
    domain: str
    path: str = "/"


def login_demo(base_url: str, username: str, password: str) -> SessionCookie:
    """POST credentials, return the session cookie. Wake 2 deliverable."""
    raise NotImplementedError("login_demo: Wake 2 deliverable")
