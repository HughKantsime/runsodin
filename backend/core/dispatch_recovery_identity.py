"""Claim-time equality evidence for manual Release. Never grants dispatch authority."""
import hmac
import json

from sqlalchemy import JSON, bindparam, select, text

from core.dispatch_identity import build_identity, stable_configuration_key
from core.models import SystemConfig


def _keys(creds, key, token, configuration_key):
    # Bind both comparisons to one attempt, even if the printer ID is reused.
    attempt_key = hmac.digest(key, b'ODIN dispatch recovery v1\0' + token.encode() + configuration_key.encode(), 'sha256')
    connection = {name: creds[name] for name in (
        'api_type', 'ip', 'port', 'scheme', 'serial', 'access_code', 'api_key',
        'username', 'password', 'org_id', 'is_active', 'shared') if name in creds}
    identity = build_identity(connection, attempt_key)
    if identity is None:
        raise ValueError('Printer connection could not be verified')
    loadout = [sorted(set(creds.get('active_materials', []))),
               sorted(creds.get('filament_slots', []), key=lambda slot: slot[0])]
    slots_key = hmac.digest(attempt_key, b'slots\0' + json.dumps(loadout, separators=(',', ':')).encode(), 'sha256').hex()
    return {'connection': identity.configuration_key, 'slots': slots_key}


def _record_key(printer_id):
    return f'dispatch_recovery_keys:{printer_id}'


def store(conn, printer_id, token, configuration_key, creds, key):
    # One bounded record per printer, replaced only alongside a new claim.
    record = _keys(creds, key, token, configuration_key)
    conn.execute(text('INSERT INTO system_config (key,value) VALUES (:key,:value) '
                      'ON CONFLICT (key) DO UPDATE SET value=excluded.value').bindparams(bindparam('value', type_=JSON)),
                 {'key': _record_key(printer_id), 'value': record})


def compare(conn, claim, receipts, creds, key):
    """Return (same configured target, filament changed or unknown)."""
    current = build_identity(creds, key)
    if current is None or current.target_key != claim['physical_key'] or creds.get('org_id') != claim['owner_org_id']:
        return False, None
    if current.configuration_key == claim['configuration_key']:
        return True, False
    stored = conn.execute(select(SystemConfig.value).where(SystemConfig.key == _record_key(claim['printer_id']))).scalar_one_or_none()
    if isinstance(stored, dict):
        current_keys = _keys(creds, key, claim['token'], claim['configuration_key'])
        if current_keys['connection'] == stored.get('connection'):
            return True, current_keys['slots'] != stored['slots'] if stored.get('slots') else None

    # Only an original admission receipt proves historical equality. A later
    # recipe backfill and a different attempt on this printer cannot do so.
    receipt = next((r for r in receipts if r['claim_token'] == claim['token']
                    and r['printer_id'] == claim['printer_id'] and r['job_id'] == claim['job_id']
                    and r['configuration_key'] == claim['configuration_key']), None)
    if (not receipt or receipt.get('stable_key_source') != 'dispatch'
            or receipt.get('stable_configuration_key') != stable_configuration_key(creds, key)):
        return False, None
    # The stable key excludes both filament and power. Attribute a full-key
    # difference to filament only when the original power evidence also matches.
    from core.dispatch_receipts import power_fingerprint
    plug = creds['plug_configuration']
    broker = None
    if plug.get('plug_type') == 'mqtt' and plug.get('plug_auto_on'):
        from modules.notifications.services import recovery_power_connection
        broker = recovery_power_connection(conn)
        if broker is None:
            return True, None
    power_matches = receipt['prepared_power_key'] == power_fingerprint(key, claim['token'], plug, broker)
    return True, True if power_matches else None
