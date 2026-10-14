"""Unregistered026 prospective global provenance; no source adoption."""
import hashlib
from pathlib import Path
from core import global_order_origin_schema as schema
from core.schema.migrator import MigrationError
MIGRATION_ID='python:026-global-order-origins'
SOURCE_SHA256='fb30ce92c43c15a5db81a86bb6dbe4d926dfe16ac577583bfab2b73d59dc94f4'
def _source():
    if hashlib.sha256((Path(__file__).resolve().parents[2]/'global_order_origin_schema.py').read_bytes()).hexdigest()!=SOURCE_SHA256:
        raise MigrationError('Global order origin source differs')
def apply(connection):
    _source()
    schema.apply(connection)
def validate(connection):
    _source()
    schema.validate(connection)
