"""Unregistered029 import journals. SQLite requires separately qualified maintenance."""
import hashlib
from pathlib import Path
from core import project_import_schema as schema
from core import project_import_validation as validation
from core.schema.migrator import MigrationError
MIGRATION_ID='python:029-project-imports'
SOURCE_PINS={'project_import_schema.py': '5d73af689269df6228590a5505a4f9bfa4804dd05a0d07372cd4d45ecc74ceea', 'project_import_validation.py': 'e20fd72120d8cbe2d7fac4305494b867fb9336aee6c8feb6038ef93630544d7a', 'project_import_transaction.py': 'ad9531c9723072bf8ad1d8094b055f3d6d78f58a8d6e41ec8782475a66ffb53c', 'db_utils.py': '459a390e21d7ac537d331637839864a124c9ce8f2f7fc8f6d65af57f9fefa6ba'}

def _source():
    base=Path(__file__).resolve().parents[2]
    if not SOURCE_PINS or any(hashlib.sha256((base/name).read_bytes()).hexdigest()!=digest for name,digest in SOURCE_PINS.items()):
        raise MigrationError('Project import029 source qualification differs')

def apply(connection):
    _source()
    schema.apply(connection)

def validate(connection):
    _source()
    validation.validate(connection)
