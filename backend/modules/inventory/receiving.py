"""Validated receiving rows and transaction-local product-line selection."""
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, text
from core.errors import ErrorCode, OdinError
from core.material_identity import canonical_reported_material
from core.models import SystemConfig
from core.dependencies import log_audit
from modules.inventory.models import FilamentLibrary
from modules.inventory.stock import seal_key


class ReceiveRolls(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    filament_id: int | None = Field(default=None, gt=0)
    brand: str | None = Field(default=None, min_length=1, max_length=100)
    product_line: str | None = Field(default=None, min_length=1, max_length=100)
    material: str | None = Field(default=None, min_length=1, max_length=32)
    color_name: str | None = Field(default=None, min_length=1, max_length=100)
    color_hex: str | None = None
    quantity: int = Field(ge=1, le=100, strict=True)
    initial_weight_g: float = Field(default=1000, gt=0, le=100000, allow_inf_nan=False)
    spool_weight_g: float = Field(default=250, ge=0, le=10000, allow_inf_nan=False)
    storage_location: str = Field(min_length=1, max_length=100)
    sealed: bool = True
    vendor: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator('color_hex')
    @classmethod
    def hex_color(cls, value):
        if value is None:
            return None
        value = value.lstrip('#').upper()
        if len(value) != 6 or any(c not in '0123456789ABCDEF' for c in value):
            raise ValueError('Color hex must contain six hexadecimal digits')
        return value

    @field_validator('material')
    @classmethod
    def known_material(cls, value):
        if value is None:
            return None
        normalized = canonical_reported_material(value)
        if normalized is None:
            raise ValueError('Unknown material; use a supported material such as PLA or PETG')
        return normalized

    @model_validator(mode='after')
    def required_facts(self):
        if self.sealed and not self.color_name:
            raise ValueError('Color name is required for sealed shelf stock')
        if self.filament_id is None and not all((self.brand, self.product_line, self.material)):
            raise ValueError('Select a filament or supply brand, product_line and material')
        if self.filament_id is not None and any((self.brand, self.product_line, self.material)):
            raise ValueError('Select a filament or supply a product line, not both')
        return self


class BulkReceive(BaseModel):
    model_config = ConfigDict(extra='forbid')
    import_id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    rows: list[ReceiveRolls] = Field(min_length=1, max_length=200)

    @model_validator(mode='after')
    def bounded_rolls(self):
        if sum(row.quantity for row in self.rows) > 1000:
            raise ValueError('Receive at most 1000 rolls per import')
        return self


def lock_receiving(db):
    conn = db.connection()
    if conn.dialect.name == 'postgresql':
        conn.execute(text('SELECT pg_advisory_xact_lock(1330071888)'))
    elif conn.dialect.name == 'sqlite' and not conn.connection.driver_connection.in_transaction:
        conn.exec_driver_sql('BEGIN IMMEDIATE')


def validate_references(db, rows):
    # Validate every reference before creating any library or spool rows.
    for row in rows:
        if row.filament_id is not None and db.get(FilamentLibrary, row.filament_id) is None:
            raise OdinError(ErrorCode.not_found, 'Filament not found', status=404)


def receive_row(db, user, body, create_spool):
    from modules.inventory.routes._helpers import SpoolCreate
    filament_id = body.filament_id
    if filament_id is None:
        filament = db.query(FilamentLibrary).filter(
            func.lower(FilamentLibrary.brand) == body.brand.lower(),
            func.lower(FilamentLibrary.name) == body.product_line.lower(),
            func.upper(FilamentLibrary.material) == body.material).order_by(FilamentLibrary.id).first()
        if filament is None:
            filament = FilamentLibrary(brand=body.brand, name=body.product_line, material=body.material, is_custom=True)
            db.add(filament)
            db.flush()
        filament_id = filament.id
    values = body.model_dump(exclude={'filament_id', 'brand', 'product_line', 'material', 'quantity', 'sealed', 'note'})
    results = []
    for _ in range(body.quantity):
        row = create_spool(SpoolCreate(filament_id=filament_id, notes=body.note, **values), user, db, commit=False)
        if body.sealed:
            db.add(SystemConfig(key=seal_key(row['id']), value=True))
        results.append({**row, 'color_name': body.color_name, 'color_hex': body.color_hex})
    log_audit(db, 'inventory.receive', 'spools', details={
        'ids': [r['id'] for r in results], 'filament_id': filament_id,
        'color_name': body.color_name, 'storage_location': body.storage_location, 'sealed': body.sealed})
    return {'spools': results}
