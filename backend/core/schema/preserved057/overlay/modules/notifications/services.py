"""Service-layer entry points other modules may import (the sanctioned cross-module boundary).

Thin, lazy wrappers: the logic stays in the files named in each function.
"""


def read_temperature_alert_settings(db):
    from modules.notifications.temperature_alerts import read_settings
    return read_settings(db)


def store_temperature_alert_settings(db, *args, **kwargs):
    from modules.notifications.temperature_alerts import store_settings
    return store_settings(db, *args, **kwargs)


def validate_temperature_alert_update(*args, **kwargs):
    from modules.notifications.temperature_rules import validate_update
    return validate_update(*args, **kwargs)
