"""Push module routes package."""
from fastapi import APIRouter
from .push import router as push_router
from .biometric import router as biometric_router

router = APIRouter()
router.include_router(push_router)
router.include_router(biometric_router)
