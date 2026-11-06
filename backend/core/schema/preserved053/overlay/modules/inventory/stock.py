"""Tenant-specific reorder policy and explicit receiving seals in the config store.

Keys are bounded hashes; values retain the human-readable material/color pair.
Existing rolls are open unless explicitly received sealed. Loading breaks a seal.
"""
import hashlib
from core.models import SystemConfig


def stock_identity(material, color):
    return (str(material or 'UNKNOWN').strip().upper(),
            str(color or 'UNKNOWN').strip().lstrip('#').upper())


def policy_prefix(org):
    return f'inventory.reorder.{org if org is not None else "global"}.'


def policy_key(org, material, color):
    digest = hashlib.sha256(f'{material}\0{color}'.encode()).hexdigest()[:32]
    return policy_prefix(org) + digest


def seal_key(spool_id):
    return f'inventory.sealed.{spool_id}'


def break_seal(db, spool_id):
    db.query(SystemConfig).filter_by(key=seal_key(spool_id)).delete(synchronize_session=False)
