"""Caller-owned light reconciliation with a command timestamp snapshot guard."""


def persist_light_observation(conn, printer_id, observed_state, toggle_snapshot):
    """Do not overwrite a command committed after the caller's cooldown read.

    The caller commits this update together with its heartbeat. No physical
    command runs while this helper holds a database write lock.
    """
    if observed_state is None:
        return
    stamp = 'CAST(? AS TIMESTAMP WITH TIME ZONE)' if getattr(conn, '_postgres', False) else '?'
    conn.execute(
        f'UPDATE printers SET lights_on=? WHERE id=? AND '
        f'(lights_toggled_at={stamp} OR (lights_toggled_at IS NULL AND CAST(? AS TEXT) IS NULL))',
        (observed_state, printer_id, toggle_snapshot, toggle_snapshot),
    )
