"""Models Library routes package — assembles all sub-routers."""

from fastapi import APIRouter
from .pricing import router as pricing_router, calculate_job_cost  # re-export for other modules
from .models_crud import router as models_crud_router
from .print_files import router as print_files_router

router = APIRouter()
# print_files first: /print-files/upload (static) and /models/{model_id}/mesh must
# register before models_crud's parameterized /{model_id} endpoints where path
# ordering could matter within the same prefix scope.
router.include_router(print_files_router)
router.include_router(models_crud_router)
router.include_router(pricing_router)

__all__ = ["router", "calculate_job_cost"]
