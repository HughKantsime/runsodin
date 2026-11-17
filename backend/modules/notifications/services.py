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


def stage_restart_alerts(db, printer_id, printer_name, question):
    from modules.notifications.restart_alerts import stage
    return stage(db, printer_id, printer_name, question)


def resolve_restart_alerts(db, identifiers):
    from modules.notifications.restart_alerts import resolve
    return resolve(db, identifiers)


def read_pause_reminder_settings(db):
    from modules.notifications.pause_reminder_settings import read_settings
    return read_settings(db)


def store_pause_reminder_settings(db, settings):
    from modules.notifications.pause_reminder_settings import store_settings
    return store_settings(db, settings)


def validate_pause_reminder_update(body, current):
    from modules.notifications.pause_reminder_settings import validate_update
    return validate_update(body, current)
