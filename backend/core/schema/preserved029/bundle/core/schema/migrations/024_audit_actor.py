"""Held, explicit-only prospective audit identity and durable erasure footprint."""
import hashlib
from pathlib import Path
from core import audit_actor_schema as schema
from core.schema.migrator import MigrationError
MIGRATION_ID = 'python:024-audit-actor'
SOURCE_PINS = {'audit_actor_schema.py': '7e7748c3ebeee11af21cbe7685a15a702f57a78a37994bab44b1e8a1e2946154', 'audit_actor.py': '48d9eaa7c9d9904f2d6619ee5f1b943fe7c0fc985e6fdd68e8849eea3d08402c'}

def _source():
    for name,pin in SOURCE_PINS.items():
        if hashlib.sha256((Path(__file__).resolve().parents[2]/name).read_bytes()).hexdigest()!=pin:
            raise MigrationError('Audit actor source differs')

def apply(connection):
    _source()
    schema.apply(connection)

def validate(connection):
    _source()
    schema.validate(connection)
