"""Optional local inspection text, shared by the request validator and HTTP retry cache."""
import json
import unicodedata

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from core.crypto import get_fernet

from pydantic import BaseModel, Field, field_validator


PRIVATE_FIELD = 'private_count_details'


def seal_text(value):
    key = get_fernet()
    if key is None:
        raise HTTPException(503, 'Ask an administrator to configure the installation encryption key before saving inspection details.')
    return key.encrypt(value.encode()).decode()


def open_text(value):
    key = get_fernet()
    if key is not None:
        try:
            return key.decrypt(value.encode()).decode()
        except InvalidToken:
            pass
    raise HTTPException(503, 'The original installation encryption key is needed to read these inspection details.')


def local_count_details(row):
    if not isinstance(row, dict) or PRIVATE_FIELD not in row:
        return row
    public = dict(row)
    private = json.loads(open_text(public.pop(PRIVATE_FIELD)))
    return {**public, **private}


class InspectionDetails(BaseModel):
    counted_by: str | None = Field(default=None, min_length=1, max_length=120)
    note: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator('counted_by', 'note', mode='before')
    @classmethod
    def clean_text(cls, value):
        if isinstance(value, str):
            if any(unicodedata.category(ch) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp') for ch in value):
                raise ValueError('Use a single line of plain text')
            return value.strip()
        return value

    def receipt_details(self):
        fields = self.model_dump(include={'counted_by', 'note'}, exclude_none=True)
        return {PRIVATE_FIELD: seal_text(json.dumps(fields))} if fields else {}

    def audit_details(self, owner):
        # Signed audit exports retain the canonical row. Resolve the counter on
        # its immutable receipt only when an authorized local audit view is read.
        return {'note_present': self.note is not None,
                **({'counted_by_receipt_owner': owner} if self.counted_by else {})}
