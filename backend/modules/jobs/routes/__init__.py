"""Jobs routes package — assembles all sub-routers."""

from fastapi import APIRouter
from .jobs_crud import router as jobs_crud_router
from .jobs_lifecycle import router as jobs_lifecycle_router
from .jobs_agent import router as jobs_agent_router
from .presets import router as presets_router

router = APIRouter()
# jobs_crud first: static paths (/jobs/filament-check, /jobs/bulk, /jobs/batch,
# /jobs/reorder, /jobs/bulk-update) must register before parameterized /jobs/{job_id}
# in jobs_lifecycle_router / jobs_agent_router.
router.include_router(jobs_crud_router)
router.include_router(jobs_lifecycle_router)
# jobs_agent: agent-surface retrofits (cancel/approve/reject) — split out
# from jobs_lifecycle in v1.9.0 Phase 2 to stay under the 700-line cap.
router.include_router(jobs_agent_router)
router.include_router(presets_router)
