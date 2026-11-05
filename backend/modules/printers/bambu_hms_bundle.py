"""Validated, offline-only Bambu release data. Never contacts a printer or vendor."""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

BUNDLE_DIR = Path(__file__).resolve().parents[2] / 'data' / 'bambu_hms'
MAX_BYTES = 8 * 1024 * 1024


def prefix_from_serial(serial):
    value = serial.strip().upper()[:3] if isinstance(serial, str) else ''
    return value if re.fullmatch(r'[A-Z0-9]{3}', value) else ''


def credential_prefix(credential):
    """printers.api_key holds encrypted serial|access_code. Never return either."""
    from core.crypto import decrypt
    try:
        parts = (decrypt(credential) or '').split('|')
        return prefix_from_serial(parts[0]) if len(parts) == 2 else ''
    except Exception:
        return ''


def canonical(code):
    if type(code) is int and 0 <= code <= 0xFFFFFFFF:
        return f'{code:08X}'
    value = str(code).strip().upper().replace('-', '').replace('_', '')
    return value


def payload_bytes(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def validate_response(raw):
    """Bambu query.php envelope; empty intro is a deliberate hidden entry."""
    if not isinstance(raw, dict) or raw.get('result') != 0:
        raise ValueError('Invalid Bambu result')
    version = raw.get('ver')
    if type(version) not in (int, str) or not str(version).isdigit():
        raise ValueError('Missing Bambu version')
    data = raw.get('data')
    if not isinstance(data, dict):
        raise ValueError('Missing Bambu data')
    parsed = {}
    for section, width in (('device_hms', 16), ('device_error', 8)):
        table = data.get(section)
        if not isinstance(table, dict) or not isinstance(table.get('en'), list) or not table['en']:
            raise ValueError('Missing English Bambu table')
        entries = {}
        for entry in table['en']:
            if not isinstance(entry, dict) or not isinstance(entry.get('ecode'), str) or not isinstance(entry.get('intro'), str):
                raise ValueError('Invalid Bambu entry')
            code = canonical(entry['ecode'])
            if not re.fullmatch(r'[0-9A-F]{' + str(width) + '}', code) or code in entries:
                raise ValueError('Invalid or duplicate Bambu code')
            entries[code] = entry['intro'].strip()
        parsed[section] = entries
    return parsed


@lru_cache(maxsize=64)
def load_bundle(prefix):
    """Missing/corrupt bundles fail to plain codes; a release restart reloads data."""
    if not isinstance(prefix, str) or not re.fullmatch(r'[A-Z0-9]{3}', prefix):
        return {}
    try:
        path = BUNDLE_DIR / f'{prefix}.json'
        with path.open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return {}
        bundle = json.loads(data)
        payload = bundle['response']
        if bundle['prefix'] != prefix or bundle['sha256'] != hashlib.sha256(payload_bytes(payload)).hexdigest():
            return {}
        return validate_response(payload)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return {}
