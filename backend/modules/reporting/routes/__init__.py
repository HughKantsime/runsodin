"""Reporting routes package — assembles all sub-routers."""

from fastapi import APIRouter
from .analytics import router as analytics_router
from .exports import router as exports_router
from .reports import router as reports_router

router = APIRouter()
router.include_router(analytics_router)
router.include_router(exports_router)
router.include_router(reports_router)
