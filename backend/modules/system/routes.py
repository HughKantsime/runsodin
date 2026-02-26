"""O.D.I.N. — System Routes (aggregator)

Sub-router responsibilities:
  routes_health.py      — Health check, license management, config (spoolman/blackout), spoolman test
  routes_config.py      — IP allowlist, retention policy, quiet hours, MQTT republish, metrics, HMS codes
  routes_setup.py       — Setup wizard (status, admin, test-printer, printer, complete, network)
  routes_backup.py      — Backup/restore (create, list, download, delete, restore)
  routes_settings.py    — Branding (logo, favicon), education mode, language
  routes_maintenance.py — Maintenance tasks, maintenance logs, maintenance status, seed defaults
  routes_admin.py       — Admin logs, log stream, support bundle, global search
"""

import logging
from fastapi import APIRouter

log = logging.getLogger("odin.api")

router = APIRouter()

from modules.system import (  # noqa: E402
    routes_health,
    routes_config,
    routes_setup,
    routes_backup,
    routes_settings,
    routes_maintenance,
    routes_admin,
)

router.include_router(routes_health.router)
router.include_router(routes_config.router)
router.include_router(routes_setup.router)
router.include_router(routes_backup.router)
router.include_router(routes_settings.router)
router.include_router(routes_maintenance.router)
router.include_router(routes_admin.router)

# Re-export health_check so core/app.py can call system.health_check()
# (core/app.py registers a root /health handler that delegates here)
from modules.system.routes_health import health_check  # noqa: F401, E402
