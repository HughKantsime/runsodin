"""Cross-process live report evidence; persisted printer rows are not authority."""
import json
import math
import time

from sqlalchemy import text

MAX_REPORT_AGE = 60
REFRESH_INTERVAL = 15


def fresh_report(retained=False, report_age_seconds=None):
    return retained is not True and (report_age_seconds is None or (
        type(report_age_seconds) in (int, float)
        and math.isfinite(report_age_seconds) and 0 <= report_age_seconds <= MAX_REPORT_AGE))


def cache_key(printer_id):
    return f'printer.live_state.{printer_id}'


def persist_dbapi(conn, printer_id, state, *, retained=False, report_age_seconds=None):
    if not isinstance(state, str) or not state or not fresh_report(retained, report_age_seconds):
        return
    value = json.dumps({'state': state.upper(), 'at': time.time() - (report_age_seconds or 0)})
    conn.execute('INSERT INTO system_config(key,value) VALUES(?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (cache_key(printer_id), value))


def persist(conn, printer_id, state, *, retained=False, report_age_seconds=None):
    """Persist validated live evidence, including a connection-scoped MQTT delta."""
    if not isinstance(state, str) or not state or not fresh_report(retained, report_age_seconds):
        return
    value = json.dumps({'state': state.upper(), 'at': time.time() - (report_age_seconds or 0)})
    conn.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
                 {'key': cache_key(printer_id), 'value': value})


class ObservationPublisher:
    """Per-monitor write throttle; only successful commits consume the interval."""

    def __init__(self):
        self._written_state = None
        self._written_at = None

    def publish(self, connect, printer_id, state, *, dbapi=False,
                retained=False, report_age_seconds=None):
        if not isinstance(state, str) or not state or not fresh_report(retained, report_age_seconds):
            return
        state = state.upper()
        now = time.time()
        if (state == self._written_state and self._written_at is not None
                and 0 <= now - self._written_at < REFRESH_INTERVAL):
            return
        with connect() as conn:
            writer = persist_dbapi if dbapi else persist
            writer(conn, printer_id, state, report_age_seconds=report_age_seconds)
            if dbapi:
                conn.commit()
        self._written_state, self._written_at = state, now


def live_state(conn, printer_id, since):
    """Read the latest report again at the command boundary; fail closed on bad data."""
    value = conn.execute(text('SELECT value FROM system_config WHERE key=:key'),
                         {'key': cache_key(printer_id)}).scalar_one_or_none()
    try:
        observation = json.loads(value)
        at = observation['at']
        if at >= since and 0 <= time.time() - at <= MAX_REPORT_AGE:
            return observation['state']
    except (ValueError, TypeError, KeyError):
        pass
    return None
