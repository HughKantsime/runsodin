"""Archives routes package — assembles all sub-routers."""

from fastapi import APIRouter
from .archives_crud import router as archives_crud_router
from .tags import router as tags_router
from .projects import router as projects_router

router = APIRouter()
router.include_router(archives_crud_router)
router.include_router(tags_router)
router.include_router(projects_router)
