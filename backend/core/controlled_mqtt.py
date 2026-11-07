"""Telemetry cache: local invalidation plus at most one second of cross-process lag."""
import time

_states = {}


def invalidate():
    _states.clear()


def redact_payload(engine, payload, *, job_id=None):
    from core.controlled_parts import any_read_restrictions
    from core.controlled_events import external_payload
    now = time.monotonic()
    state = _states.get(engine)
    if state is None or now - state[0] >= 1:
        with engine.connect() as db:
            state = (now, any_read_restrictions(db))
        _states[engine] = state
    if not state[1]:
        return payload
    with engine.connect() as db:
        db.info.update(_controlled_request=True, _controlled_any=True)
        try:
            return external_payload(db, payload, job_id=job_id)
        finally:
            db.info.pop('_controlled_request', None)
            db.info.pop('_controlled_any', None)
