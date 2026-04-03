"""O.D.I.N. — Organizations Auth Routes (aggregator)

Sub-router responsibilities:
  routes_auth.py        — Login, logout, MFA, me/theme, ws-token, password reset, auth capabilities
  routes_oidc.py        — OIDC/SSO login, callback, code exchange, admin config
  routes_sessions.py    — Sessions, API tokens, quotas, GDPR export/erase
  routes_users.py       — Users CRUD, groups, CSV import
  routes_permissions.py — RBAC page/action permissions
"""

import logging
from fastapi import APIRouter

log = logging.getLogger("odin.api")

router = APIRouter()

from modules.organizations import (  # noqa: E402
    routes_auth,
    routes_oidc,
    routes_sessions,
    routes_users,
    routes_permissions,
)

router.include_router(routes_auth.router)
router.include_router(routes_oidc.router)
router.include_router(routes_sessions.router)
router.include_router(routes_users.router)
router.include_router(routes_permissions.router)
