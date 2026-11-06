"""Bounded request contracts. Answer keys never appear in student projections."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Question(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    prompt: str = Field(min_length=1, max_length=500)
    choices: list[str] = Field(min_length=2, max_length=8)
    correct: int = Field(ge=0, le=7, strict=True)

    @model_validator(mode='after')
    def valid_choices(self):
        if self.correct >= len(self.choices) or any(not c.strip() or len(c) > 300 for c in self.choices):
            raise ValueError('Each choice must be nonempty, at most 300 characters, and correct must index a choice')
        if len(set(self.choices)) != len(self.choices):
            raise ValueError('Choices must be distinct')
        return self


class TrainingDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    command_id: UUID
    school_wide: bool = False
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default='', max_length=4000)
    scope_kind: Literal['printer_group', 'machine_type', 'model'] = 'printer_group'
    scope_value: str = Field(default='', max_length=160)
    quiz: list[Question] = Field(default_factory=list, max_length=30)
    pass_mark: int = Field(default=80, ge=1, le=100, strict=True)
    instructor_required: bool = True
    expiry_days: int | None = Field(default=None, ge=1, le=3650, strict=True)
    active: bool = True

    @model_validator(mode='after')
    def valid_requirement(self):
        if not self.quiz and not self.instructor_required:
            raise ValueError('A quiz or instructor sign-off is required')
        if self.scope_kind != 'printer_group' and not self.scope_value:
            raise ValueError('A printer model or machine type is required')
        if self.scope_kind == 'printer_group' and self.scope_value:
            raise ValueError('Printer group uses the center authorized printer set')
        return self


class TrainingUpdate(TrainingDefinition):
    edit_version: int = Field(ge=1, strict=True)
    revision: int = Field(ge=1, strict=True)


class TrainingCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: UUID
    revision: int = Field(ge=1, strict=True)


class QuizAttempt(TrainingCommand):
    answers: list[int] = Field(max_length=30)


class SignoffCommand(TrainingCommand):
    reason: Literal['instructor_assessment', 'safety_review', 'expired', 'correction'] = 'instructor_assessment'
