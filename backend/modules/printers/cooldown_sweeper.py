"""Backstop for the smart-plug cooldown auto-off (B2c).

The cooldown timer (smart_plug.on_print_complete) lives in the monitor process
that saw the print end, so any restart of that process loses it; with the
license fleet lock enabled, a license change restarts whole monitor daemons
(core.license_monitor). This sweeper runs in the API process, which license
changes never restart. Every 60 s it looks at printers with a plug and auto-off
whose latest print completed at least cooldown + 2 minutes ago, and runs the
same off path the timer would:
- Home Assistant / MQTT: plug identity, _auto_off_safe and B2b's
  completion_allowed(..., 'cooldown_off'), then power_off(expected_config=identity);
- Tasmota: power_fence.run(..., 'cooldown_off', physical_id=print_job_id,
  expected=_snapshot(...)), which also refuses a second command for the same
  print (power_target_operations), so it never repeats the timer's command.
A plug whose stored state is off is skipped. Each (printer, print) gets one
attempt per process lifetime, and only within WINDOW_MINUTES of becoming due,
so a plug switched back on by hand later is left alone.

Runs only while the license fleet lock is enabled (rollout off: origin/main
behaviour, no sweeper queries). No schema change.
"""
import logging

from sqlalchemy import text

from core.db import concrete_engine

log = logging.getLogger("smart_plug")

SWEEP_SECONDS = 60
GRACE_MINUTES = 2
WINDOW_MINUTES = 30
COMPLETED = ('completed', 'finished', 'success')

_attempted = set()  # (printer_id, print_job_id) tried or skipped in this process


def _due(module):
    """(printer_id, print_job_id, minutes since it ended) for each plug with auto-off
    whose latest print completed."""
    from modules.printers.power_fence import _utc
    with module.engine.connect() as conn:
        now = _utc(conn.execute(text('SELECT clock_timestamp()' if conn.dialect.name == 'postgresql'
                                     else "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")).scalar_one())
        printers = conn.execute(text(
            "SELECT id FROM printers WHERE plug_type IS NOT NULL AND plug_auto_off IS TRUE")).scalars().all()
        due = []
        for printer_id in printers:
            row = conn.execute(text(
                "SELECT id, status, ended_at FROM print_jobs WHERE printer_id=:pid ORDER BY id DESC LIMIT 1"),
                {'pid': printer_id}).first()
            if row is None or row[1] not in COMPLETED or row[2] is None:
                continue
            due.append((printer_id, row[0], (now - _utc(row[2])).total_seconds() / 60))
    return due


def _off(module, printer_id, print_job_id, config):
    """The timer's off path, without the timer (see smart_plug.on_print_complete)."""
    from core import license_lock
    if config['type'] in ('homeassistant', 'mqtt'):
        identity = module._plug_identity(config)
        with module._printer_power_lock(printer_id):
            current = module.get_plug_config(printer_id)
            if (current and current['auto_off'] and module._plug_identity(current) == identity
                    and module._auto_off_safe(printer_id)
                    and license_lock.completion_allowed(concrete_engine(module.engine), printer_id,
                                                        print_job_id, 'cooldown_off')):
                return module.power_off(printer_id, expected_config=identity)
        return None
    from modules.printers.power_fence import run, _snapshot
    expected = _snapshot(module, printer_id, 'cooldown_off', print_job_id, admission=False)
    if expected is None:
        return None
    return run(module, printer_id, 'cooldown_off', physical_id=print_job_id, expected=expected)


def sweep(module=None):
    """One pass; returns how many off commands were attempted."""
    from core import license_lock
    if not license_lock.enabled():
        return 0
    if module is None:
        from modules.printers import smart_plug as module
    attempted = 0
    for printer_id, print_job_id, ended_minutes in _due(module):
        key = (printer_id, print_job_id)
        if key in _attempted or printer_id in module._cooldown_timers:
            continue  # tried already, or this process's own timer still owns it
        try:
            config = module.get_plug_config(printer_id)
            minutes = config['cooldown_minutes'] if config else None
            if not config or not config['auto_off'] or type(minutes) is not int or not 0 <= minutes <= 1440:
                continue
            due_at = minutes + GRACE_MINUTES
            if ended_minutes < due_at:
                continue
            _attempted.add(key)
            if ended_minutes > due_at + WINDOW_MINUTES:
                continue
            if config['power_state'] is not None and not config['power_state']:
                continue  # stored state off: the timer (or someone) already turned it off
            attempted += 1
            _off(module, printer_id, print_job_id, config)
        except Exception:
            log.warning("Automatic power-off backstop could not be completed")
    return attempted


async def run_sweeper(interval: float = SWEEP_SECONDS) -> None:
    """API lifespan task: one pass every interval seconds, the work in a thread."""
    import asyncio
    while True:
        try:
            await asyncio.to_thread(sweep)
        except Exception:
            log.warning("Automatic power-off backstop failed; it runs again at the next interval")
        await asyncio.sleep(interval)

