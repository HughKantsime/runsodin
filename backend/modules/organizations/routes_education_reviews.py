"""Private Education submission queue and teacher review endpoints."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from modules.organizations.education_access import require_education_principal
from modules.organizations.education_review_service import (
    approve_submission,
    list_visible_submissions,
    reject_submission,
)
from modules.organizations.education_schemas import (
    SubmissionApproval,
    SubmissionRejection,
)


router = APIRouter(prefix="/education/submissions", tags=["Education"])


@router.get("")
async def get_visible_submissions(
    status: Literal[
        "submitted",
        "pending",
        "scheduled",
        "printing",
        "completed",
        "failed",
        "rejected",
        "cancelled",
    ]
    | None = None,
    cost_center_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=100),
    principal: dict = Depends(require_education_principal()),
    db: Session = Depends(get_db),
):
    return list_visible_submissions(
        db,
        principal=principal,
        status=status,
        cost_center_id=cost_center_id,
        limit=limit,
    )


@router.post("/{submission_id}/approve")
async def approve_education_submission(
    submission_id: int,
    body: SubmissionApproval,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    return approve_submission(
        db,
        submission_id=submission_id,
        body=body,
        principal=principal,
    )


@router.post("/{submission_id}/reject")
async def reject_education_submission(
    submission_id: int,
    body: SubmissionRejection,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    return reject_submission(
        db,
        submission_id=submission_id,
        body=body,
        principal=principal,
    )
