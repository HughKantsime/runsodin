"""O.D.I.N. — Printer Routes (aggregator)

Sub-router responsibilities:
  route_utils.py        — Shared utilities: SSRF blocklist, camera URL helpers, go2rtc sync, adapter commands
  routes_crud.py        — Printer CRUD, filament slots, test-connection, bulk-update
  routes_status.py      — Live status, telemetry, HMS error history
  routes_controls.py    — Commands: pause, resume, cancel, lights, speed, fans, bed level, movement
  routes_ams.py         — AMS sync (Moonraker MMU + Bambu)
  routes_bambu.py       — Bambu test-connection, filament types, manual slot assignment
  routes_ams_env.py     — AMS environment, current telemetry, AMS slot updates
  routes_smart_plug.py  — Smart plug power control
  routes_nozzle.py      — Nozzle lifecycle tracking
"""

import logging
from fastapi import APIRouter

log = logging.getLogger("odin.api")

router = APIRouter()

from modules.printers import (  # noqa: E402
    routes_crud,
    routes_status,
    routes_controls,
    routes_ams,
    routes_bambu,
    routes_ams_env,
    routes_smart_plug,
    routes_nozzle,
)

router.include_router(routes_crud.router)
router.include_router(routes_status.router)
router.include_router(routes_controls.router)
router.include_router(routes_ams.router)
router.include_router(routes_bambu.router)
router.include_router(routes_ams_env.router)
router.include_router(routes_smart_plug.router)
router.include_router(routes_nozzle.router)

# Re-export shared utilities so camera_routes.py can continue to import from here
from modules.printers.route_utils import get_camera_url, sync_go2rtc_config  # noqa: F401, E402
