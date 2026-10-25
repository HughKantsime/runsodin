"""Stock Anycubic monitor. Reuses Elegoo's common telemetry/lifecycle sink."""
from core.base import slot_filament_value
import logging
import signal
import threading
import time

from sqlalchemy import column, func, table, text
from core.db import live_engine as engine
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
                        conn.execute(text('UPDATE printers SET model=:model WHERE id=:pid'), {'model': model, 'pid': self.printer_id})
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


def main():
    from core.monitor_bootstrap import bootstrap_monitor_authority
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    logging.basicConfig(level=logging.INFO)
    bootstrap_monitor_authority()
    shutdown = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: shutdown.set())
    threads = {}
    try:
        while not shutdown.is_set():
            with engine.connect() as conn:
                rows = conn.execute(text("SELECT id,name,api_host FROM printers WHERE api_type='anycubic_lan' AND api_host IS NOT NULL AND is_active IS TRUE")).mappings().all()
            wanted = {r['id']: (r['name'], r['api_host']) for r in rows}
            for pid, (identity, thread) in list(threads.items()):
                if wanted.get(pid) != identity or not thread.is_alive():
                    thread.stop()
                    thread.join(timeout=25)
                    del threads[pid]
            for pid, identity in wanted.items():
                if pid not in threads:
                    thread = AnycubicLANMonitorThread(pid, *identity)
                    threads[pid] = (identity, thread)
                    thread.start()
            shutdown.wait(10)
    finally:
        for _, thread in threads.values():
            thread.stop()
        for _, thread in threads.values():
            thread.join(timeout=25)


if __name__ == '__main__':
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    main()
