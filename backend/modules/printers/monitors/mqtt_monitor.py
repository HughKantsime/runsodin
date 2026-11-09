#!/usr/bin/env python3
"""
MQTT Print Monitor Daemon
Connects to all Bambu printers and tracks print jobs automatically.

Usage:
    python mqtt_monitor.py          # Run in foreground
    python mqtt_monitor.py --daemon # Run as background daemon
"""
import os
import sys
sys.path.insert(0, os.environ.get('BACKEND_PATH', '/app/backend'))

import time
import signal
import logging
from typing import Dict

from sqlalchemy import text

import core.crypto as crypto
# Resolved at use time: never a stale engine bound at import.
from core.db import live_engine as engine
from modules.printers.monitors.mqtt_printer import PrinterMonitor

# Moonraker support (Klipper printers like Kobra S1 w/ Rinkhals)
try:
    from moonraker_monitor import MoonrakerMonitor
    MOONRAKER_AVAILABLE = True
except ImportError:
    MOONRAKER_AVAILABLE = False

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('mqtt_monitor')


CAMERA_RESOLUTION_INTERVAL = 300  # seconds between go2rtc camera host re-resolution


class MQTTMonitorDaemon:
    """Main daemon that monitors all printers."""

    def __init__(self):
        from core.monitor_bootstrap import bootstrap_monitor_authority
        bootstrap_monitor_authority()
        self.monitors: Dict[int, PrinterMonitor] = {}
        self._running = False

    def load_printers(self):
        """Load Bambu printers from database."""
        # Get encryption key
        key = os.environ.get('ENCRYPTION_KEY')
        if not key:
            log.error("ENCRYPTION_KEY not set")
            return []

        with engine.connect() as conn:
            result = conn.execute(text('''
                SELECT id, name, model, api_host, api_key
                FROM printers
                WHERE api_host IS NOT NULL AND api_key IS NOT NULL AND api_key != ''
                  AND LOWER(COALESCE(api_type, 'bambu')) = 'bambu'
                  AND is_active IS NOT FALSE
            '''))

            printers = []
            for row in result.mappings():
                try:
                    decrypted = crypto.decrypt(row['api_key'])
                    parts = decrypted.split('|')
                    if len(parts) == 2:
                        printers.append({
                            'id': row['id'],
                            'name': row['name'],
                            'ip': row['api_host'],
                            'serial': parts[0],
                            'access_code': parts[1]
                        })
                except Exception as e:
                    log.warning(f"Could not load {row['name']}: {e}")

            # EDU replay is deliberately not configured through the printer
            # record: no fictional printer stores a host, serial, or access
            # credential. Both explicit sandbox gates are required before the
            # monitor uses the compose-internal broker and synthetic identity.
            if (
                os.environ.get("ODIN_EDU_SANDBOX_SEED") == "1"
                and os.environ.get("ODIN_EDU_BAMBU_REPLAY") == "1"
            ):
                result = conn.execute(text('''
                    SELECT id, name
                    FROM printers
                    WHERE api_type = 'bambu'
                      AND api_host IS NULL
                      AND api_key IS NULL
                      AND is_active IS TRUE
                '''))
                simulated = list(result.mappings())
                if len(simulated) != 1:
                    log.error("EDU replay requires exactly one credential-free active Bambu row")
                else:
                    row = simulated[0]
                    printers.append({
                        "id": row["id"],
                        "name": row["name"],
                        "ip": "mosquitto",
                        "serial": "EDU-BAMBU-SIM-001",
                        "access_code": "simulation-only",
                    })

        return printers

    def load_moonraker_printers(self):
        """Load Moonraker-based printers from database."""
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, name, api_host
                FROM printers
                WHERE api_type = 'moonraker'
                  AND api_host IS NOT NULL
                  AND api_host != ''
                  AND is_active IS TRUE
            """))

            printers = []
            for row in result.mappings():
                host_str = row['api_host']
                # Parse host:port
                if ':' in host_str:
                    host, port_str = host_str.rsplit(':', 1)
                    try:
                        port = int(port_str)
                    except ValueError:
                        host = host_str
                        port = 80
                else:
                    host = host_str
                    port = 80

                printers.append({
                    'id': row['id'],
                    'name': row['name'],
                    'host': host,
                    'port': port,
                })

        return printers

    def start(self):
        """Start monitoring all printers."""
        from modules.printers.monitor_commands import CommandServer
        try:
            self._command_server = CommandServer(self.monitors)
        except Exception as exc:
            # Commands are optional; telemetry must never depend on the channel.
            # Without it, light/resume requests report 'unavailable'.
            self._command_server = None
            log.warning("Monitor command channel unavailable (%s)", type(exc).__name__)
        self._running = True
        try:
            printers = self._licensed(self.load_printers)()
        except Exception:
            printers = []  # license policy unreadable at start: the next pass retries

        if not printers:
            log.info("No printers found yet, waiting for printers to be added...")

        log.info(f"Starting monitor for {len(printers)} printers")

        for p in printers:
            monitor = PrinterMonitor(
                printer_id=p['id'],
                name=p['name'],
                ip=p['ip'],
                serial=p['serial'],
                access_code=p['access_code']
            )
            if monitor.connect():
                self.monitors[p['id']] = monitor

        log.info(f"Connected to {len(self.monitors)}/{len(printers)} Bambu printers")

        # Start Moonraker monitors
        if MOONRAKER_AVAILABLE:
            mk_printers = self.load_moonraker_printers()
            if mk_printers:
                log.info(f"Starting {len(mk_printers)} Moonraker monitor(s)")
                for p in mk_printers:
                    monitor = MoonrakerMonitor(
                        printer_id=p['id'],
                        name=p['name'],
                        host=p['host'],
                        port=p['port'],
                    )
                    if monitor.connect():
                        self.monitors[p['id']] = monitor
                log.info(f"Moonraker monitors connected")
            else:
                log.info("No Moonraker printers configured")

        # Keep running with periodic reconnection checks
        self._all_printers = printers  # Save for reconnection
        if MOONRAKER_AVAILABLE:
            self._all_moonraker = mk_printers if mk_printers else []
        else:
            self._all_moonraker = []
        self._last_reconnect_check = time.time()
        self._last_printer_reload = time.time()

        try:
            while self._running:
                time.sleep(1)
                # Every 60s, bring the monitored set in line with the table; a
                # license change triggers the same pass within about 2 s (B2c).
                license_changed = False
                if time.time() - getattr(self, '_last_license_check', 0) >= 2:
                    self._last_license_check = time.time()
                    license_changed = hasattr(self, '_license') and self._license.changed()
                if license_changed or time.time() - self._last_printer_reload >= 60:
                    self._check_new_printers()
                    self._last_printer_reload = time.time()

                # Every 30s, check for dead connections and reconnect (after the license
                # pass, so a printer that just left the selection is not retried)
                if time.time() - self._last_reconnect_check >= 30:
                    self._check_reconnect()
                    self._last_reconnect_check = time.time()

                # Every 5 min, rebuild the go2rtc config: camera hosts are
                # written as validated IP literals, so a DNS change must reach
                # go2rtc. Unchanged configs do not restart go2rtc.
                if time.time() - getattr(self, '_last_camera_resolution', 0) >= CAMERA_RESOLUTION_INTERVAL:
                    self._last_camera_resolution = time.time()
                    try:
                        from modules.printers.route_utils import sync_go2rtc_config_raw
                        sync_go2rtc_config_raw()
                    except Exception:
                        log.warning("Camera source re-resolution failed; retry next interval")
        except KeyboardInterrupt:
            log.info("Shutting down...")

        self.stop()

    def _licensed(self, load):
        """Loader filtered to the printers this process may monitor (B2c).

        An unreadable policy raises, so _check_new_printers keeps the current set."""
        def licensed():
            printers = load()
            if not hasattr(self, '_license'):
                from core.license_monitor import MonitorLicense
                self._license = MonitorLicense(engine)
            allowed = self._license.wanted([p['id'] for p in printers])
            if allowed is None:
                raise RuntimeError('license policy unavailable')
            return [p for p in printers if p['id'] in allowed]
        return licensed

    def _families(self):
        families = [("Bambu", '_all_printers', self._licensed(self.load_printers), ('ip', 'serial', 'access_code'),
                     lambda p: PrinterMonitor(printer_id=p['id'], name=p['name'], ip=p['ip'],
                                              serial=p['serial'], access_code=p['access_code']))]
        if MOONRAKER_AVAILABLE:
            families.append(("Moonraker", '_all_moonraker', self._licensed(self.load_moonraker_printers), ('host', 'port'),
                             lambda p: MoonrakerMonitor(printer_id=p['id'], name=p['name'],
                                                        host=p['host'], port=p['port'])))
        return families

    def _check_new_printers(self):
        """Align monitors with the printers table: added, removed, deactivated, edited.

        All removals (both families) run before any connection, so a printer id
        that moved between families is never disconnected after it reconnects.
        A failed read keeps that family's monitors; a failed connect is retried
        on the next pass.
        """
        loaded = []
        for kind, attr, load, keys, factory in self._families():
            previous = getattr(self, attr, [])
            try:
                current = load()
            except Exception as e:
                log.warning(f"{kind} printer reload failed; keeping current monitors: {type(e).__name__}")
                continue
            loaded.append((kind, attr, previous, current, keys, factory))
        removals = []
        for kind, _attr, previous, current, keys, _factory in loaded:
            identity = lambda p: tuple(p.get(k) for k in keys)
            after = {p['id']: p for p in current}
            for old in previous:
                new = after.get(old['id'])
                if new is not None and identity(new) == identity(old):
                    continue
                monitor = self.monitors.pop(old['id'], None)
                if monitor is None:
                    continue
                reason = "connection settings changed" if new is not None else "removed, inactive or not licensed"
                log.info(f"[{old['name']}] {reason}; disconnecting {kind} monitor")
                removals.append((kind, monitor, old['id']))
        self._disconnect_all(removals)
        for kind, attr, _previous, current, _keys, factory in loaded:
            setattr(self, attr, current)
            for p in current:
                if p['id'] in self.monitors or self._tearing_down_now(p['id']):
                    continue
                log.info(f"[{p['name']}] Connecting {kind} monitor...")
                try:
                    monitor = factory(p)
                    if monitor.connect():
                        self.monitors[p['id']] = monitor
                        log.info(f"[{p['name']}] Connected")
                except Exception as e:
                    log.warning(f"[{p['name']}] {kind} connect failed; retry next pass: {type(e).__name__}")

    def _disconnect_all(self, removals):
        """Disconnect removed monitors.

        License fleet lock enabled (B2c): in parallel against one shared 5 s
        deadline (paho joins have no timeout); a monitor whose disconnect has not
        finished stays fenced in _tearing_down, and no replacement session for
        that printer starts until it has. Otherwise: one by one, as before."""
        if not removals:
            return
        from core import license_lock

        def disconnect(kind, monitor, _pid=None):
            try:
                monitor.disconnect()
            except Exception as e:
                log.debug(f"Error disconnecting {kind} monitor: {type(e).__name__}")
        if not license_lock.enabled():
            for removal in removals:
                disconnect(*removal)
            return
        from threading import Thread
        from core.license_monitor import drain, _joiner, TEARDOWN_SECONDS
        threads = [Thread(target=disconnect, args=removal, daemon=True) for removal in removals]
        late = drain([t.start for t in threads], [_joiner(t) for t in threads])
        if not hasattr(self, '_tearing_down'):
            self._tearing_down = {}
        for i in late:
            self._tearing_down[removals[i][2]] = threads[i]
        if late:
            log.warning(f"{len(late)} monitor(s) did not disconnect within {TEARDOWN_SECONDS}s; "
                        f"replacements wait until they do")

    def _persist_offline(self, printer_id):
        try:
            with engine.begin() as conn:
                conn.execute(text("UPDATE printers SET gcode_state='OFFLINE',print_stage='Offline' WHERE id=:pid"), {"pid": printer_id})
        except Exception:
            log.warning("Offline state persistence failed; retry on next connection check")

    def _check_reconnect(self):
        """Check for dead connections and attempt reconnection."""
        # Check Bambu printers
        for kind, printers, retry in (("Bambu", self._all_printers, self._reconnect_bambu),
                                      ("Moonraker", self._all_moonraker, self._reconnect_moonraker)):
            for p in list(printers):
                # One printer's failure must never stop the daemon or the others.
                try:
                    retry(p)
                except Exception as e:
                    log.warning(f"[{p['name']}] {kind} reconnect failed; retry next check: {type(e).__name__}")

    def _tearing_down_now(self, pid):
        thread = getattr(self, '_tearing_down', {}).get(pid)
        if thread is not None and not thread.is_alive():
            self._tearing_down.pop(pid, None)
            return False
        return thread is not None

    def _reconnect_bambu(self, p):
        pid = p['id']
        if self._tearing_down_now(pid):
            return  # the old session is still disconnecting: never two sessions for one printer
        monitor = self.monitors.get(pid)

        if monitor is None:
            # Never connected - try again
            log.info(f"[{p['name']}] Attempting initial connection...")
            self._persist_offline(pid)
            new_mon = PrinterMonitor(
                printer_id=p['id'],
                name=p['name'],
                ip=p['ip'],
                serial=p['serial'],
                access_code=p['access_code']
            )
            if new_mon.connect():
                self.monitors[pid] = new_mon
                log.info(f"[{p['name']}] Reconnected successfully")
            return

        # Check if connection is dead
        is_dead = getattr(monitor, "_transport_offline", False)
        if hasattr(monitor, '_bambu') and monitor._bambu:
            if not monitor._bambu._connected:
                is_dead = True

        # Also check staleness - no heartbeat in 60s
        if not is_dead and getattr(monitor, '_last_heartbeat', 0) > 0:
            if time.time() - monitor._last_heartbeat > 120:
                is_dead = True

        if is_dead:
            log.info(f"[{monitor.name}] Connection dead, reconnecting...")
            try:
                monitor.disconnect()
            except Exception as e:
                log.debug(f"Error disconnecting stale monitor: {e}")
            self._persist_offline(pid)
            time.sleep(1)  # let old TLS socket fully tear down

            new_mon = PrinterMonitor(
                printer_id=p['id'],
                name=p['name'],
                ip=p['ip'],
                serial=p['serial'],
                access_code=p['access_code']
            )
            if new_mon.connect():
                self.monitors[pid] = new_mon
                log.info(f"[{monitor.name}] Reconnected successfully")
            else:
                log.warning(f"[{monitor.name}] Reconnection failed, will retry in 30s")
                del self.monitors[pid]

    def _reconnect_moonraker(self, p):
        pid = p['id']
        monitor = self.monitors.get(pid)

        if monitor is None:
            log.info(f"[{p['name']}] Attempting Moonraker connection...")
            new_mon = MoonrakerMonitor(
                printer_id=p['id'],
                name=p['name'],
                host=p['host'],
                port=p['port'],
            )
            if new_mon.connect():
                self.monitors[pid] = new_mon
                log.info(f"[{p['name']}] Moonraker reconnected")
            return

        # Check staleness for Moonraker
        if hasattr(monitor, '_last_heartbeat') and monitor._last_heartbeat > 0:
            if time.time() - monitor._last_heartbeat > 120:
                log.info(f"[{monitor.name}] Moonraker stale, reconnecting...")
                try:
                    monitor.disconnect()
                except Exception as e:
                    log.debug(f"Error disconnecting stale Moonraker monitor: {e}")
                new_mon = MoonrakerMonitor(
                    printer_id=p['id'],
                    name=p['name'],
                    host=p['host'],
                    port=p['port'],
                )
                if new_mon.connect():
                    self.monitors[pid] = new_mon
                    log.info(f"[{monitor.name}] Moonraker reconnected")
                else:
                    log.warning(f"[{monitor.name}] Moonraker reconnection failed")
                    del self.monitors[pid]

    def stop(self):
        """Stop all monitors."""
        self._running = False
        server = getattr(self, "_command_server", None)
        if server is not None:
            server.close()
        for monitor in self.monitors.values():
            monitor.disconnect()
        log.info("All monitors stopped")


def main():
    from core.interfaces.filament_ownership import log_external_filament_accounting
    log_external_filament_accounting(log)
    from core.job_timeline import start
    start("mqtt_monitor")
    daemon = MQTTMonitorDaemon()

    # Handle signals
    def signal_handler(sig, frame):
        log.info("Signal received, stopping...")
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    daemon.start()


if __name__ == '__main__':
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    from core.schema.bootstrap import configure_orm
    configure_orm()
    main()
