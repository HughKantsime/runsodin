"""Models Library routes package — assembles all sub-routers."""

from fastapi import APIRouter
from .pricing import router as pricing_router
from .models_crud import router as models_crud_router
from .print_files import router as print_files_router
from modules.models_library.services import calculate_job_cost  # noqa: F401 — re-exported for backwards compat

router = APIRouter()
# print_files first: /print-files/upload (static) and /models/{model_id}/mesh must
# register before models_crud's parameterized /{model_id} endpoints where path
# ordering could matter within the same prefix scope.
router.include_router(print_files_router)
router.include_router(models_crud_router)
router.include_router(pricing_router)

__all__ = ["router", "calculate_job_cost"]
