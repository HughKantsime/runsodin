"""Explicit inspection inputs. Output quantities are pieces, never stock totals."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool, model_validator

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
    job_id: StrictInt | None = Field(default=None, gt=0)
    print_job_id: StrictInt | None = Field(default=None, gt=0)
    square_revision: str | None = Field(default=None, max_length=64)
    inspection_confirmed: bool
    outputs: list[InspectedOutput] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def one_source(self):
        if (self.job_id is None) == (self.print_job_id is None):
            raise ValueError('Choose exactly one job_id or print_job_id')
        return self

class OutsideAttestation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    started_outside_odin: StrictBool
    file_revision_confirmed: StrictBool
    statement: str | None = Field(default=None, max_length=2000)

class OutsideBindingInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recipe_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    attestation: OutsideAttestation
    attested_by: Literal['owner_in_app', 'owner_confirmed_to_operator'] = 'owner_in_app'


class OutsideQualificationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    printer_id: StrictInt = Field(gt=0)
    print_file_id: StrictInt | None = Field(default=None, gt=0)
    plate_index: StrictInt = Field(ge=0)
    name: str = Field(min_length=1, max_length=160)
    materials: str = Field(min_length=1, max_length=1000)
    qualification_confirmed: StrictBool
    outputs: list[Output] = Field(min_length=1, max_length=8)
    attestation: OutsideAttestation
