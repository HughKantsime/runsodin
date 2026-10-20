"""027 additive power storage only; shared registration is a separate batch."""
import hashlib
from pathlib import Path
from core import power_target_operation_schema as schema
from core.schema.migrator import MigrationError, acquire_schema_writer_lock
MIGRATION_ID = 'python:027-power-target-operations'
SOURCE_SHA256 = '35c17c77d2a3c4b223c597a3698dc00d23a316920b126d2f4483a404f02c5b94'
def _source():
    source = Path(__file__).resolve().parents[2] / 'power_target_operation_schema.py'
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise MigrationError('Power storage source differs')
def apply(connection):
    _source(); acquire_schema_writer_lock(connection); schema.apply(connection)
def validate(connection):
    _source(); acquire_schema_writer_lock(connection); schema.validate(connection)
