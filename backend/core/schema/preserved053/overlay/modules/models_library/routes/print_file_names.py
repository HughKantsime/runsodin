"""Print-file rename for files without a model (display name fallback); operator, audited."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_action

router = APIRouter()


class PrintFileRename(BaseModel):
    original_filename: str = Field(min_length=1, max_length=255)

    @field_validator('original_filename')
    @classmethod
    def plain_filename(cls, value):
        if not value.strip() or '/' in value or '\\' in value or any(ord(c) < 32 for c in value):
            raise ValueError('Use a plain filename')
        return value.strip()


@router.patch('/print-files/{file_id}/name')
def rename_print_file(file_id: int, body: PrintFileRename,
                      actor=Depends(require_action('models.edit', scope='write:models')),
                      db: Session=Depends(get_db)):
    from core.model_preview_access import accessible_preview_file
    from modules.models_library.file_management import _assert_generic_payload
    row = accessible_preview_file(db, file_id, actor)
    _assert_generic_payload(db, row)
    if row.model_id is not None:
        raise HTTPException(409, 'Rename the model for this print file')
    db.execute(text('UPDATE print_files SET original_filename=:name WHERE id=:id'),
               {'name':body.original_filename, 'id':file_id})
    log_audit(db, 'print_file.renamed', 'print_file', file_id,
              {'before':row.original_filename, 'after':body.original_filename})
    db.commit()
    from core.display_names import human_name
    return {'id':file_id, 'original_filename':body.original_filename,
            'display_name':human_name(original_filename=body.original_filename)}
