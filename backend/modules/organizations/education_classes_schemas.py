"""Bounded D2 request models; limits are estimates in seconds and grams."""
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, allow_inf_nan=False)


class ClassCreate(StrictBody):
    cost_center_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    term: str = Field(min_length=1, max_length=100)


class ClassUpdate(StrictBody):
    revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    term: str | None = Field(default=None, min_length=1, max_length=100)
    state: Literal['active', 'archived'] | None = None

    @model_validator(mode='after')
    def nonnull_changes(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError('Class fields cannot be null')
        return self


class AssignmentCreate(StrictBody):
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(default='', max_length=10000)
    due_at: datetime | None = None
    max_print_seconds: int | None = Field(default=None, gt=0, le=31536000)
    max_material_grams: float | None = Field(default=None, gt=0, le=1000000)
    allowed_materials: list[str] = Field(default_factory=list, max_length=30)
    submission_quota: int | None = Field(default=None, gt=0, le=10000)
    auto_print_seconds: int | None = Field(default=None, gt=0, le=31536000)
    auto_material_grams: float | None = Field(default=None, gt=0, le=1000000)
    auto_printer_ids: list[int] = Field(default_factory=list, max_length=100)
    state: Literal['active', 'closed'] = 'active'

    @field_validator('due_at')
    @classmethod
    def utc_due(cls, value):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError('Due date requires a timezone')
            return value.astimezone(timezone.utc)
        return value

    @field_validator('allowed_materials')
    @classmethod
    def materials(cls, values):
        if values is None:
            return values
        if any(not v.strip() or len(v) > 40 for v in values):
            raise ValueError('Materials must be nonempty names of at most 40 characters')
        return sorted(set(v.strip().upper() for v in values))

    @field_validator('auto_printer_ids')
    @classmethod
    def printers(cls, values):
        if values is None:
            return values
        if any(v <= 0 for v in values):
            raise ValueError('Printer IDs must be positive')
        return sorted(set(values))

    @model_validator(mode='after')
    def complete_rule(self):
        enabled = bool(self.auto_printer_ids)
        if enabled != (self.auto_print_seconds is not None) or enabled != (self.auto_material_grams is not None):
            raise ValueError('Auto-approval requires both limits and at least one printer')
        return self


class AssignmentUpdate(AssignmentCreate):
    # None is only an omission sentinel: exclude_unset drives the patch. The
    # merged record is validated as AssignmentCreate before it is persisted.
    revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    brief: str | None = Field(default=None, max_length=10000)
    allowed_materials: list[str] | None = Field(default=None, max_length=30)
    auto_printer_ids: list[int] | None = Field(default=None, max_length=100)
    state: Literal['active', 'closed'] | None = None

    @model_validator(mode='after')
    def complete_rule(self):
        for key in ('title', 'brief', 'allowed_materials', 'auto_printer_ids', 'state'):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(key + ' cannot be null')
        return self


class SubmissionLink(StrictBody):
    submission_id: int = Field(gt=0)
    revision: int = Field(ge=1)


class Assessment(StrictBody):
    revision: int = Field(ge=1)
    feedback: str | None = Field(default=None, max_length=10000)
    grade: str | None = Field(default=None, max_length=100)
    learning_status: Literal['submitted', 'reviewed', 'needs_revision', 'complete'] | None = None

    @model_validator(mode='after')
    def nonnull_changes(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError('Assessment fields cannot be null')
        return self
