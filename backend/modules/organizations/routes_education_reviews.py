"""Private Education submission queue and teacher review endpoints."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from core.db import get_db
from modules.organizations.education_access import require_education_principal
from modules.organizations.education_review_service import (
    approve_submission,
    list_visible_submissions,
    preview_submission_compatibility,
    reject_submission,
)
from modules.organizations.education_schemas import (
    SubmissionApproval,
    TeacherAMSRead,
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
    cursor: str | None = None,
    archived: bool = False,
    principal: dict = Depends(require_education_principal()),
    db: Session = Depends(get_db),
):
    return list_visible_submissions(
        db,
        principal=principal,
        status=status,
        cost_center_id=cost_center_id,
        limit=limit,
        cursor=cursor,
        archived=archived,
    )


@router.get("/{submission_id}/compatibility")
async def get_submission_compatibility(
    submission_id: int,
    printer_id: int = Query(gt=0),
    revision: int = Query(ge=1),
    principal: dict = Depends(require_education_principal()),
    db: Session = Depends(get_db),
):
    return preview_submission_compatibility(
        db,
        submission_id=submission_id,
        printer_id=printer_id,
        revision=revision,
        principal=principal,
    )


@router.post("/{submission_id}/approve")
async def approve_education_submission(
    submission_id: int,
    body: SubmissionApproval,
    request: Request,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    if body.ams_mapping is not None:
        from modules.organizations.education_ams_approval_service import approve_with_explicit_ams
        return await approve_with_explicit_ams(request, db=db, principal=principal,
                                              submission_id=submission_id, body=body)
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


@router.post('/{submission_id}/ams-read')
async def read_education_submission_ams(submission_id: int, body: TeacherAMSRead, request: Request,
    principal: dict = Depends(require_education_principal(write=True)), db: Session = Depends(get_db)):
    # Release the cached authentication dependency's read transaction too.
    db.rollback()
    db.close()
    from modules.organizations.education_ams_read_service import acquire_teacher_ams_report
    return await acquire_teacher_ams_report(request,principal=principal,submission_id=submission_id,body=body)


from pydantic import BaseModel, Field
from uuid import UUID


class SubmissionArchive(BaseModel):
    revision: int = Field(ge=1)
    reason: str = Field(default='', max_length=1000)
    command_id: UUID


@router.post('/{submission_id}/archive')
async def archive_education_submission(submission_id: int, body: SubmissionArchive,
    principal: dict = Depends(require_education_principal(write=True)), db: Session = Depends(get_db)):
    from modules.organizations.education_submission_archive import archive_submission
    return archive_submission(db, submission_id=submission_id, body=body, principal=principal)
