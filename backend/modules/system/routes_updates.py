"""Platform administrator update settings, available on every tier."""
import asyncio

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, StrictBool
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_superadmin
from . import update_check

router = APIRouter(tags=["System"])


class UpdateSetting(BaseModel):
    enabled: StrictBool


@router.get("/system/updates")
def get_updates(db: Session = Depends(get_db), user: dict = Depends(require_superadmin(scope="admin"))):
    return update_check.status(db)


@router.put("/system/updates")
def set_updates(body: UpdateSetting, db: Session = Depends(get_db), user: dict = Depends(require_superadmin(scope="admin"))):
    return update_check.configure(db, body.enabled, user["id"])


@router.post("/system/updates/check")
async def check_now(db: Session = Depends(get_db), user: dict = Depends(require_superadmin(scope="admin"))):
    return await run_in_threadpool(update_check.run_check, db, admission_loop=asyncio.get_running_loop())
