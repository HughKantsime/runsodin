"""Strict retained evidence shapes used by print-next; never coerce identities."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Positive = Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]
Count = Annotated[int, Field(strict=True, ge=0)]
RecordID = Annotated[str, Field(strict=True, pattern=r'^[a-f0-9]{32}$')]
Digest = Annotated[str, Field(strict=True, pattern=r'^[a-f0-9]{64}$')]
Name = Annotated[str, Field(min_length=1)]


class Evidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Payload(Evidence):
    model_id: Positive
    print_file_id: Positive
    printer_id: Positive
    plate_index: Count
    payload_sha256: Digest
    configuration_key: Name


class Output(Evidence):
    product_id: Positive
    kind: Literal['main', 'flush']
    expected: Positive
    product_name: Name
    bom_model_id: Positive | None = None


class Attestation(Evidence):
    started_outside_odin: bool
    file_revision_confirmed: bool
    statement: str | None = None


class OutsideReference(Evidence):
    actor_user_id: Positive
    confirmed_at: Name
    auth_kind: str | None
    actor_token_id: Positive | None
    print_job_id: Positive
    attestation: Attestation
    matched_by: Literal['explicit', 'filename']
    observed_filename: str | None
    observed_job_name: str | None


class Recipe(Evidence):
    # Some stored copies include an id. It must agree with the authoritative column.
    id: RecordID = None
    version: Annotated[int, Field(ge=1, le=2)]
    name: Name
    materials: Name
    qualified: bool
    created_at: Name
    reference_job_id: Positive | None
    reference_claim_token: RecordID | None
    reference_observation_uid: Name
    payload: Payload
    outputs: list[Output] = Field(min_length=1, max_length=8)
    qualified_by_token: bool = False
    operator_id: Positive = None
    api_token_id: Positive | None = None
    qualified_from_outside_print: bool = False
    outside_print_evidence: OutsideReference | None = None

    @model_validator(mode='after')
    def source(self):
        if self.qualified_from_outside_print:
            if self.outside_print_evidence is None:
                raise ValueError('Missing outside print evidence')
        elif self.reference_job_id is None or self.reference_claim_token is None:
            raise ValueError('Missing reference job')
        keys = [(o.product_id, o.kind) for o in self.outputs]
        if len(set(keys)) != len(keys):
            raise ValueError('Duplicate outputs')
        return self


class Baseline(Evidence):
    version: Name
    at: Name
    quantity: Count
    attested_by: Positive


class AcceptedOutput(Output):
    accepted: Count
    rejected: Count
    disposition: Literal['send', 'in_baseline'] = 'send'
    send_quantity: Count = None
    observation_ended_at: Name = None
    baseline_version: Name | None = None
    stock_baseline: Baseline | None = None

    @model_validator(mode='after')
    def quantities(self):
        if self.accepted + self.rejected != self.expected:
            raise ValueError('Output count differs from yield')
        if self.send_quantity is not None and self.send_quantity > self.accepted:
            raise ValueError('Stock addition exceeds accepted output')
        return self


class RestoredState(Evidence):
    status: Name
    hold: bool
    actual_start: str | None
    actual_end: str | None
    duration_hours: float | None
    queue_position: int | None


class RestoredJob(Evidence):
    job_id: Positive
    previous_status: Name
    status: Name
    hold: bool
    metrics_only: bool
    restored_state: RestoredState | None


class OutsideBinding(Evidence):
    version: Literal[1]
    id: RecordID
    owner: Count
    print_job_id: Positive
    economic_uid: Name | None
    acceptance_uid: Name
    physical_uid: Name
    archive_id: Positive | None
    archive_ids: list[Positive]
    recipe_id: RecordID
    recipe_revision: Recipe
    payload: Payload
    filename: str | None
    job_name: str | None
    ended_at: Name
    odin_dispatched: Literal[False]
    owner_attested: Literal[True]
    attestation: Attestation
    attested_by: Literal['owner_in_app', 'owner_confirmed_to_operator']
    actor_user_id: Positive
    actor_token_id: Positive | None
    created_at: Name
    restored_job: RestoredJob | None


class Receipt(Evidence):
    id: RecordID = None
    recipe_id: RecordID = None
    version: Literal[1]
    job_id: Positive | None
    job_uid: Name
    physical_uid: Name
    occurred_at: Name
    recipe_name: Name
    outputs: list[AcceptedOutput] = Field(min_length=1, max_length=8)
    stock_status: Literal['prepared', 'in_baseline', 'no_sellable_output']
    # ODIN receipts use physical_uid. Outside receipts additionally carry this ID.
    print_job_id: Positive = None
    odin_dispatched: bool = True
    owner_attested: bool = False
    outside_binding: OutsideBinding | None = None

    @model_validator(mode='after')
    def source(self):
        if self.odin_dispatched:
            if self.job_id is None:
                raise ValueError('Missing job')
        elif self.print_job_id is None or self.outside_binding is None or not self.owner_attested:
            raise ValueError('Missing outside print')
        return self


class ModelSnapshot(Evidence):
    model_id: Positive
    name: Name
    created_at: Name
    print_file_id: Positive | None
    units_per_bed: Positive | None
    quantity_per_bed: Positive | None


class Component(Evidence):
    id: RecordID
    job_id: Positive
    job_uid: Name
    claim_token: RecordID
    source: ModelSnapshot
    payload: Payload
    physical_uid: Name
    accepted: Count
    rejected: Count
    unit_description: Name
    request_hash: Digest
    occurred_at: Name
    stock_status: Literal['components_only']


class Hardware(Evidence):
    consumable_id: Positive
    quantity_per_product: Annotated[float, Field(gt=0)]
    name: Name
    unit: Name
    managed: bool
    material_uid: Name | None


# BOM dictionaries are serialized with string model keys by the existing writer.
ModelKey = Annotated[str, Field(pattern=r'^[1-9][0-9]*$', max_length=19)]


class BOM(Evidence):
    product_id: Positive
    product_name: Name
    components: dict[ModelKey, Positive]
    sources: dict[ModelKey, ModelSnapshot]
    hardware: list[Hardware]


class Allocation(Evidence):
    receipt_id: RecordID
    quantity: Positive


class MaterialUse(Evidence):
    name: Name
    consumable_id: Positive
    material_uid: Name
    expected_units: Count
    used_units: Count
    reason: str


class MaterialUses(Evidence):
    managed: list[MaterialUse]
    unmanaged_ids: list[Positive]
    confirmed: bool
    reclaimed_units: Count


class Finish(Evidence):
    id: RecordID
    kind: Literal['assembly']
    occurred_at: Name
    request_hash: Digest
    bom: BOM
    bom_hash: Digest
    allocations: list[Allocation] = Field(min_length=1)
    outputs: list[AcceptedOutput] = Field(min_length=1)
    recipe_name: Name
    assembly_confirmed: Literal[True]
    hardware_confirmed: Literal[True]
    stock_status: Literal['prepared', 'no_sellable_output']
    material_use: MaterialUses | None = None


class Finishing(Evidence):
    version: Literal[1]
    owner: Count
    revision: RecordID | None
    components: dict[RecordID, Component]
    finishes: dict[RecordID, Finish]


class Work(Evidence):
    id: Positive
    model_id: Positive | None
    print_file_id: Positive | None
    printer_id: Positive | None
    payload_sha256: Digest | None
    plate_index: Count | None
    quantity: Positive | None
