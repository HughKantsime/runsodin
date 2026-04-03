"""Notifications routes package — assembles all sub-routers."""

from fastapi import APIRouter
from .webhooks import router as webhooks_router
from .alerts import router as alerts_router
from .providers import router as providers_router

router = APIRouter()
router.include_router(webhooks_router)
router.include_router(alerts_router)
router.include_router(providers_router)
