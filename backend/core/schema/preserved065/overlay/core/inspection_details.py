"""Optional counter and note on local inspection records."""
import unicodedata

from pydantic import BaseModel, Field, field_validator


class InspectionDetails(BaseModel):
    counted_by: str | None = Field(default=None, min_length=1, max_length=120)
    note: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator('counted_by', 'note', mode='before')
    @classmethod
    def clean_text(cls, value):
        if isinstance(value, str):
            chars = []
            for ch in value:
                category = unicodedata.category(ch)
                if category in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp') and ch not in ('\u200c', '\u200d'):
                    raise ValueError('Use a single line of plain text')
                chars.append(' ' if category == 'Zs' else ch)
            return ''.join(chars).strip()
        return value

    def receipt_details(self):
        return self.model_dump(include={'counted_by', 'note'}, exclude_none=True)

    def audit_details(self):
        return {'counted_by_present': self.counted_by is not None, 'note_present': self.note is not None}
