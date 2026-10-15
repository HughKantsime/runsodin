"""Reviewed whole025 restore contract source pin; explicit compatibility freeze."""
import hashlib
from pathlib import Path
from core.schema.migrator import MigrationError
SOURCE_SHA256='c0e8f20c2572c5adad8d94f74e989feca0784fc679c3d2a20125e7768ffb3f5c'
def check_manifest():
    if hashlib.sha256((Path(__file__).parent/'pre026_restore_contract.py').read_bytes()).hexdigest()!=SOURCE_SHA256:
        raise MigrationError('Packaged025 restore contract source differs')
