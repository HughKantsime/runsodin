"""Explicit inspection inputs. Output quantities are pieces, never stock totals."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt

class Output(BaseModel):
    model_config = ConfigDict(extra='forbid')
    product_id: StrictInt = Field(gt=0)
    kind: Literal['main', 'flush']
    expected: StrictInt = Field(gt=0, le=10000)

class RecipeInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reference_job_id: StrictInt = Field(gt=0)
    name: str = Field(min_length=1, max_length=160)
    materials: str = Field(min_length=1, max_length=1000)
    qualification_confirmed: bool = False
    outputs: list[Output] = Field(min_length=1, max_length=8)

class InspectedOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    product_id: StrictInt = Field(gt=0)
    kind: Literal['main', 'flush']
    accepted: StrictInt = Field(ge=0, le=10000)
    rejected: StrictInt = Field(ge=0, le=10000)
    baseline_disposition: Literal['in_baseline', 'send'] | None = None

class InspectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recipe_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    job_id: StrictInt = Field(gt=0)
    square_revision: str | None = Field(default=None, max_length=64)
    inspection_confirmed: bool
    outputs: list[InspectedOutput] = Field(min_length=1, max_length=8)
