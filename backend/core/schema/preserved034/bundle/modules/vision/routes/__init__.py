"""Vision routes package — assembles all sub-routers."""

from fastapi import APIRouter
from .detections import router as detections_router
from .vision_models import router as vision_models_router

router = APIRouter()
router.include_router(detections_router)
router.include_router(vision_models_router)
