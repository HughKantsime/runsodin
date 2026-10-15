"""Reviewed whole025 restore contract source pin; explicit compatibility freeze."""
import hashlib
from pathlib import Path
from core.schema.migrator import MigrationError
SOURCE_SHA256='a69f865465cb478ad0c490ba7096a83f171d0714377b88d566994309d1ccbd9d'
def check_manifest():
    if hashlib.sha256((Path(__file__).parent/'pre026_restore_contract.py').read_bytes()).hexdigest()!=SOURCE_SHA256:
        raise MigrationError('Packaged025 restore contract source differs')
