"""D3 bounded, explicit reporting inputs. No implied Perkins classifications."""
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReportFilters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cost_center_id: int | None = Field(None, gt=0)
    class_id: str | None = Field(None, min_length=1, max_length=36)
    term: str | None = Field(None, min_length=1, max_length=100)
    start_date: date
    end_date: date
    template: Literal['generic', 'perkins'] = 'generic'

    @model_validator(mode='after')
    def window(self):
        if self.end_date < self.start_date or (self.end_date - self.start_date).days > 366:
            raise ValueError('Select an inclusive date window of at most 367 days')
        if self.end_date == date.max:
            raise ValueError('End date is out of range')
        if self.template == 'perkins' and not (self.term and self.term.strip() or self.class_id):
            raise ValueError('Perkins requires a term or class')
        return self


class ProgramMark(BaseModel):
    model_config = ConfigDict(extra='forbid')
    participant: bool
    concentrator: bool
    revision: int = Field(0, ge=0)

    @model_validator(mode='after')
    def counts(self):
        if self.concentrator and not self.participant:
            raise ValueError('A concentrator must also be marked as a participant')
        return self


class InstructorHours(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    instructor_id: int = Field(gt=0)
    activity_date: date
    hours: float = Field(ge=0, le=24)
    revision: int = Field(0, ge=0)
