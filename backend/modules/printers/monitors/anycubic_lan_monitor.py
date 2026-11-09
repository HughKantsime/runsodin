"""Stock Anycubic monitor. Reuses Elegoo's common telemetry/lifecycle sink."""
from core.base import slot_filament_value
from core.printer_identity_audit import observe_printer_identity
import logging
import signal
import threading
import time

from sqlalchemy import column, func, table, text
from core.db import live_engine as engine
from core.license_monitor import MonitorLicense, drain, LICENSE_PASS_SECONDS, TEARDOWN_SECONDS
from core.base import FilamentType
from modules.printers.adapters.anycubic_lan import AnycubicLANPrinter
from modules.printers.parsing.anycubic_lan import MODELS, filament_slots
from modules.printers.monitors.elegoo_monitor import ElegooMonitorThread

log = logging.getLogger(__name__)
POLL_INTERVAL = 30
STALE_INTERVAL = 90


class AnycubicLANMonitorThread(ElegooMonitorThread):
    def __init__(self, printer_id, name, host):
        super().__init__(printer_id, name, host)
        self.client = AnycubicLANPrinter(host)
        self.client.on_status(self._on_status_update)
        self._wake = threading.Event()

    def stop(self):
        self._wake.set()
        super().stop()

    def _offline(self):
        with self._status_lock:
            if self._retired:
                return
            try:
                with engine.begin() as conn:
                    conn.execute(text("UPDATE printers SET gcode_state='OFFLINE', print_stage='Offline' WHERE id=:pid"), {'pid': self.printer_id})
                self._marked_offline = True
            except Exception:
                log.warning('Anycubic offline persistence failed')

    def run(self):
        while self._running:
            if not self.client._connected:
                if not self.client.connect():
                    self._offline()
                    self._wake.wait(POLL_INTERVAL)
                    continue
                if not self._running:
                    self.client.disconnect()
                    return
                self._discover_and_save_camera()
                try:
                    model = MODELS.get(self.client.model_id, self.client.model_id)
                    with engine.begin() as conn:
                        observe_printer_identity(conn, self.printer_id, 'model', model)
                except Exception:
                    log.warning('Anycubic model persistence failed')
                connected_at = time.monotonic()
            if not self.client.poll():
                self._offline()
                self.client.disconnect()
            elif time.monotonic() - max(self.client.last_info, connected_at) > STALE_INTERVAL:
                # Temperature pushes must not hide stale lifecycle information.
                self._offline()
                self.client.disconnect()
            self._wake.wait(POLL_INTERVAL)
        self.client.disconnect()

    def _process_status_locked(self, status):
        if status.internal_state == 'OFFLINE':
            return  # No live lifecycle report yet; do not feed OFFLINE into the Elegoo sink.
        super()._process_status_locked(status)
        sync_slots(self.printer_id, status.boxes)
        now = time.time()
        if status.boxes and now - getattr(self, '_last_ace_env', 0) >= 300:
            with engine.begin() as conn:
                for box in status.boxes:
                    humidity = box.get('humidity')
                    if box.get('model_id') == 40001 and humidity == 0 and not (box.get('drying_status') or {}).get('status'):
                        humidity = None
                    if box.get('temp') is not None or box.get('humidity') is not None:
                        conn.execute(text('INSERT INTO ams_telemetry (printer_id,ams_unit,humidity,temperature) VALUES (:pid,:unit,:hum,:temp)'),
                                     {'pid': self.printer_id, 'unit': box['id'], 'hum': humidity, 'temp': box.get('temp')})
            self._last_ace_env = now


def sync_slots(printer_id, boxes):
    """Preserve identity on color reports; explicit empty unloads through inventory."""
    with engine.begin() as conn:
        slots = filament_slots(boxes)
        from core.interfaces.inventory_moves import inventory_moves
        inventory_moves().observe_empty_slots(conn, printer_id,
            [s['slot_number'] for s in slots if s['material'] == 'EMPTY'])
        for slot in slots:
            params = {'pid': printer_id, 'sn': slot['slot_number'], 'ft': slot_filament_value(slot['material']),
                      'color': slot['material'] if slot['material'] != 'EMPTY' else None,
                      'hex': slot['color_hex']}
            existing = conn.execute(text('SELECT id FROM filament_slots WHERE printer_id=:pid AND slot_number=:sn'), params).first()
            if existing:
                conn.execute(text("UPDATE filament_slots SET filament_type=:ft, color=:color, color_hex=CASE WHEN :ft='empty' THEN NULL ELSE COALESCE(:hex,color_hex) END WHERE printer_id=:pid AND slot_number=:sn"), params)
            else:
                slot_table = table('filament_slots', *(column(name) for name in
                                   ('printer_id', 'slot_number', 'filament_type', 'color', 'color_hex', 'loaded_at')))
                conn.execute(slot_table.insert().values(
                    printer_id=params['pid'], slot_number=params['sn'], filament_type=params['ft'],
                    color=params['color'], color_hex=params['hex'], loaded_at=func.now()))
        if slots:
            conn.execute(text('UPDATE printers SET slot_count=:count WHERE id=:pid'), {'count': max(s['slot_number'] for s in slots), 'pid': printer_id})


INVENTORY_SECONDS = 10


def _joiner(thread):
    def join(timeout):
        thread.join(timeout=timeout)
        return not thread.is_alive()
    return join


def reconcile_threads(threads, wanted, factory=None):
    """Stop removed, changed or dead threads (one shared deadline), then start additions."""
    removals = [(pid, thread) for pid, (identity, thread) in threads.items()
                if wanted.get(pid) != identity or not thread.is_alive()]
    late = drain([thread.stop for _, thread in removals], [_joiner(thread) for _, thread in removals])
    if late:
        log.warning('Anycubic monitor: %d thread(s) did not stop within %ss; abandoned (retired)',
                    len(late), TEARDOWN_SECONDS)
    for pid, _ in removals:
        del threads[pid]
    for pid, identity in wanted.items():
        if pid not in threads:
            thread = (factory or AnycubicLANMonitorThread)(pid, *identity)
            threads[pid] = (identity, thread)
            thread.start()


def main():
    from core.monitor_bootstrap import bootstrap_monitor_authority
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    from core.job_timeline import start
    start("anycubic_lan_monitor")
    logging.basicConfig(level=logging.INFO)
    from core.interfaces.filament_ownership import log_external_filament_accounting
    log_external_filament_accounting(log)
    bootstrap_monitor_authority()
    shutdown = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: shutdown.set())
    threads = {}
    license_view = MonitorLicense(engine)
    inventory, permitted, next_inventory = {}, set(), 0.0
    try:
        while not shutdown.is_set():
            refresh = time.monotonic() >= next_inventory
            if refresh:
                with engine.connect() as conn:
                    rows = conn.execute(text("SELECT id,name,api_host FROM printers WHERE api_type='anycubic_lan' AND api_host IS NOT NULL AND is_active IS TRUE")).mappings().all()
                inventory = {r['id']: (r['name'], r['api_host']) for r in rows}
                next_inventory = time.monotonic() + INVENTORY_SECONDS
            if refresh or license_view.changed():
                allowed = license_view.wanted(inventory)
                if allowed is not None:  # unreadable policy: keep the current set
                    permitted = allowed
                reconcile_threads(threads, {pid: ident for pid, ident in inventory.items() if pid in permitted})
            shutdown.wait(LICENSE_PASS_SECONDS)
    finally:
        reconcile_threads(threads, {})


if __name__ == '__main__':
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    from core.schema.bootstrap import configure_orm
    configure_orm()
    main()
