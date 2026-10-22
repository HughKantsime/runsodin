"""Internal DB configuration binding. This does not authorize an initiator."""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field

from cryptography.fernet import Fernet
from sqlalchemy import text
from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig


class AMSConfigurationUnavailable(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class BoundAMSConfiguration:
    printer_id: int
    owner_org_id: int | None
    configuration_key: str
    transport: BambuAdapterConfig = field(repr=False, compare=False)

    def __repr__(self):
        return 'BoundAMSConfiguration(<private transport>)'


def _flag(value):
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if value is None:
        return False
    raise ValueError()


def _host(value):
    if type(value) is not str or not 1 <= len(value) <= 253 or '%' in value:
        raise ValueError()
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
               for label in value.split('.')):
            raise ValueError() from None
    return value


def load_ams_configuration(db, printer_id):
    """Load current facts. Caller must hold authority/transaction locks separately.

    No crypto.decrypt legacy plaintext fallback. No writes or network actions. Fixed TLS/8883 real-printer transport;
    demo broker overrides are not resolved here.
    Exact host/serial/credential values remain transport-significant.
    """
    try:
        if type(printer_id) is not int or printer_id <= 0:
            raise ValueError()
        row = db.execute(text('SELECT id,org_id,api_type,api_host,api_key,is_active,shared '
                              'FROM printers WHERE id=:id'), {'id': printer_id}).mappings().one_or_none()
        if row is None or row['id'] != printer_id or row['api_type'] != 'bambu':
            raise ValueError()
        owner = row['org_id']
        if owner is not None and (type(owner) is not int or owner <= 0):
            raise ValueError()
        if not _flag(row['is_active']) or _flag(row['shared']):
            raise ValueError()
        host = _host(row['api_host'])
        ciphertext = row['api_key']
        key = os.environ.get('ENCRYPTION_KEY')
        if (type(ciphertext) is not str or not 1 <= len(ciphertext) <= 4096
                or type(key) is not str or len(key) != 44):
            raise ValueError()
        fernet = Fernet(key.encode('ascii'))
        decoded = fernet.decrypt(ciphertext.encode('ascii')).decode('utf-8')
        parts = decoded.split('|')
        if (len(parts) != 2 or not re.fullmatch(r'[A-Za-z0-9]{1,64}', parts[0])
                or not re.fullmatch(r'[^\s|\x00-\x1f\x7f]{1,128}', parts[1])):
            raise ValueError()
        serial, access_code = parts
        digest_key = hmac.new(base64.urlsafe_b64decode(key),
                             b'odin/ams-configuration/key/v1', hashlib.sha256).digest()
        canonical = json.dumps([printer_id, 'bambu', host, 8883, 'bblp', True,
                                serial, access_code], separators=(',', ':'),
                               ensure_ascii=True).encode('ascii')
        digest = hmac.new(digest_key, b'odin/ams-configuration/value/v1\x00' + canonical,
                          hashlib.sha256).hexdigest()
        return BoundAMSConfiguration(printer_id, owner, digest,
            BambuAdapterConfig(str(printer_id), serial, host, access_code))
    except Exception:
        raise AMSConfigurationUnavailable('Fresh printer configuration unavailable') from None
