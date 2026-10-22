"""Typed request and response contracts for Education administration."""

from __future__ import annotations

from typing import Literal, Annotated
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class EducationCapabilities(BaseModel):
    education_enabled: bool
    student: bool
    manager: bool
    tenant_admin: bool
    student_cost_center_ids: list[int]
    managed_cost_center_ids: list[int]


class CostCenterCreate(BaseModel):
    org_id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    command_id: UUID

    @field_validator("name", "code")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class CostCenterUpdate(BaseModel):
    org_id: int | None = None
    revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    command_id: UUID

    @field_validator("name", "code")
    @classmethod
    def reject_blank_optional(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value


class CostCenterLifecycle(BaseModel):
    org_id: int | None = None
    revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    command_id: UUID

    @field_validator("reason")
    @classmethod
    def reject_blank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class GrantInput(BaseModel):
    user_id: int = Field(gt=0)
    roles: list[Literal["student", "manager"]] = Field(min_length=1, max_length=2)

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("roles must be unique")
        return sorted(value)


class GrantReplacement(BaseModel):
    org_id: int | None = None
    revision: int = Field(ge=1)
    grants: list[GrantInput] = Field(max_length=1000)
    command_id: UUID


class PrinterReplacement(BaseModel):
    org_id: int | None = None
    revision: int = Field(ge=1)
    printer_ids: list[int] = Field(max_length=250)
    command_id: UUID

    @field_validator("printer_ids")
    @classmethod
    def valid_unique_printers(cls, value: list[int]) -> list[int]:
        if any(printer_id <= 0 for printer_id in value):
            raise ValueError("printer_ids must be positive")
        if len(value) != len(set(value)):
            raise ValueError("printer_ids must be unique")
        return value


class AMSMappingEntry(BaseModel):
    model_config = {'extra': 'forbid'}
    source_slot: Annotated[int, Field(strict=True, ge=0, le=15)]
    target_slot: Annotated[int, Field(strict=True, ge=1, le=16)]


class SubmissionApproval(BaseModel):
    revision: int = Field(ge=1)
    printer_id: int = Field(gt=0)
    command_id: UUID
    ams_mapping: list[AMSMappingEntry] | None = Field(default=None, min_length=1, max_length=16)
    ams_generation_id: Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')] | None = None
    plate_index: Annotated[int, Field(strict=True, ge=0, le=15)] | None = None

    @model_validator(mode='after')
    def require_complete_ams_selection(self):
        if self.ams_mapping is not None:
            if self.ams_generation_id is None or self.plate_index is None:
                raise ValueError('AMS selection requires a current report and explicit plate')
            if len({x.source_slot for x in self.ams_mapping}) != len(self.ams_mapping) or len({x.target_slot for x in self.ams_mapping}) != len(self.ams_mapping):
                raise ValueError('AMS selection must not duplicate sources or trays')
        elif self.ams_generation_id is not None or self.plate_index is not None:
            raise ValueError('AMS report and plate require an explicit mapping')
        return self


class SubmissionRejection(BaseModel):
    revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    command_id: UUID

    @field_validator("reason")
    @classmethod
    def reject_blank_rejection_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class TeacherAMSRead(BaseModel):
    model_config = {'extra': 'forbid'}
    revision: Annotated[int, Field(strict=True, ge=1)]
    printer_id: Annotated[int, Field(strict=True, gt=0)]
    selected_slots: list[Annotated[int, Field(strict=True, ge=1, le=16)]] = Field(min_length=1, max_length=16)

    @field_validator('selected_slots')
    @classmethod
    def unique_selected_slots(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('Selected trays must be unique')
        return value
