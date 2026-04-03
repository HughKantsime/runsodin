"""Inventory routes package — assembles all sub-routers."""

from fastapi import APIRouter
from .spool_ops import router as spool_ops_router
from .spools import router as spools_router
from .filament_slots import router as filament_slots_router
from .drying_logs import router as drying_logs_router
from .spoolman import router as spoolman_router

router = APIRouter()
# spool_ops first: static paths (/spools/export, /spools/labels/batch, etc.)
# must register before parameterized /spools/{spool_id} in spools_router
router.include_router(spool_ops_router)
router.include_router(spools_router)
router.include_router(filament_slots_router)
router.include_router(drying_logs_router)
router.include_router(spoolman_router)
