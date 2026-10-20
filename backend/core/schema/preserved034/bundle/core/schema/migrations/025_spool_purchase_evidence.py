"""Registered prospective purchase evidence; entire currency dependency is frozen.

Changing finance_currency.py requires an explicit migration compatibility decision,
including changes unrelated to its currency vocabulary; never silently repin025.
"""
import hashlib
from pathlib import Path
from core import spool_purchase_schema as schema
from core.schema.migrator import MigrationError
MIGRATION_ID='python:025-spool-purchase-evidence'
SOURCE_PINS={'spool_purchase_schema.py': '4813cc361887a02ec47176447b5c680e534dc2ba065d5aca2b07421078329eba', 'spool_purchase_validation.py': 'fa178aa3f7677def1da7915f5b42da964a4045ce26fee5439039c54459d0cec5', 'finance_currency.py': 'ccc24d34b1a223115caebcbe054143b545cdf1efb7359f3ba18adff3a7400df7'}

def _source():
    for name,pin in SOURCE_PINS.items():
        if hashlib.sha256((Path(__file__).resolve().parents[2]/name).read_bytes()).hexdigest()!=pin:
            raise MigrationError('Spool purchase source differs')

def apply(connection):
    _source()
    schema.apply(connection)

def validate(connection):
    _source()
    schema.validate(connection)
