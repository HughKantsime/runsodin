"""Farm-wide pause reminder settings; no new alert preference or schema."""
import json
import math
from sqlalchemy import text
from core.system_config_values import write_config_text

DEFAULTS = {'enabled': True, 'first_hours': 2, 'repeat_hours': 12}
KEY = 'pause_reminder_settings'


def read_json(db, key):
    value = db.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': key}).scalar()
    try:
        while isinstance(value, str):
            value = json.loads(value)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def validate_update(body, current):
    for key, value in body.items():
        if key not in DEFAULTS:
            raise ValueError(f'Unknown setting: {key}.')
        if key == 'enabled':
            if type(value) is not bool:
                raise ValueError('enabled must be true or false.')
            continue
        number = type(value) is int or (type(value) is float and math.isfinite(value))
        if key == 'first_hours' and not (number and 0.25 <= value <= 168):
            raise ValueError('first_hours must be between 0.25 and 168.')
        if key == 'repeat_hours' and not (number and (value == 0 or 1 <= value <= 168)):
            raise ValueError('repeat_hours must be 0 (no repeat) or between 1 and 168.')
    return {**current, **body}


def read_settings(db):
    try:
        return validate_update(read_json(db, KEY), DEFAULTS)
    except ValueError:
        return dict(DEFAULTS)


def store_settings(db, settings):
    write_config_text(db, KEY, json.dumps(settings))
