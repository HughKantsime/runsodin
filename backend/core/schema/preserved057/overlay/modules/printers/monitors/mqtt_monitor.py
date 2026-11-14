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

    def load_printers(self, bind=None):
        """Load Bambu printers from database (bind: an engine; default the shared one)."""
        # Get encryption key
        key = os.environ.get('ENCRYPTION_KEY')
        if not key:
            log.error("ENCRYPTION_KEY not set")
            return []

        with (bind or engine).connect() as conn:
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

    def load_moonraker_printers(self, bind=None):
        """Load Moonraker-based printers from database (bind: an engine; default the shared one)."""
        with (bind or engine).connect() as conn:
            result = conn.execute(text("""
                SELECT id, name, api_host, model, machine_type
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
                    'model': row['model'], 'machine_type': row['machine_type'],
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
        from core import license_lock
        licensed = license_lock.enabled()
        if licensed:
            # Restart retirement (B2c): the license pass runs on its own thread,
            # independent of the synchronous connects in this loop; the first
            # inventory pass (_licensed_pass) starts the allowed printers.
            from core.license_monitor import Retirement, INVENTORY_SECONDS
            # The removal/reconfiguration check also runs on its own thread (inventory=),
            # so a connect hung in this loop cannot delay it.
            self._retirement = Retirement(engine, 'Bambu', log,
                                          inventory=self._session_fingerprints).start()
        printers = [] if licensed else self.load_printers()

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
            mk_printers = [] if licensed else self.load_moonraker_printers()
            if mk_printers:
                log.info(f"Starting {len(mk_printers)} Moonraker monitor(s)")
                for p in mk_printers:
                    monitor = MoonrakerMonitor(
                        printer_id=p['id'],
                        name=p['name'],
                        host=p['host'],
                        port=p['port'], model=p.get('model'), machine_type=p.get('machine_type'),
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
        self._last_printer_reload = 0.0 if licensed else time.time()
        reload_every = INVENTORY_SECONDS if licensed else 60
        refresh = self._licensed_pass if licensed else self._check_new_printers

        try:
            while self._running:
                time.sleep(1)
                # Every 30s, check for dead connections and reconnect
                if time.time() - self._last_reconnect_check >= 30:
                    self._check_reconnect()
                    self._last_reconnect_check = time.time()

                # Every 60s, bring the monitored set in line with the table
                # (license fleet lock enabled: every 10 s, see _licensed_pass)
                if time.time() - self._last_printer_reload >= reload_every:
                    refresh()
                    self._last_printer_reload = time.time()

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

    def _families(self):
        families = [("Bambu", '_all_printers', self.load_printers, ('ip', 'serial', 'access_code'),
                     lambda p: PrinterMonitor(printer_id=p['id'], name=p['name'], ip=p['ip'],
                                              serial=p['serial'], access_code=p['access_code']))]
        if MOONRAKER_AVAILABLE:
            families.append(("Moonraker", '_all_moonraker', self.load_moonraker_printers, ('host', 'port', 'model', 'machine_type'),
                             lambda p: MoonrakerMonitor(printer_id=p['id'], name=p['name'],
                                                        host=p['host'], port=p['port'], model=p.get('model'), machine_type=p.get('machine_type'))))
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
                reason = "connection settings changed" if new is not None else "removed or inactive"
                log.info(f"[{old['name']}] {reason}; disconnecting {kind} monitor")
                try:
                    monitor.disconnect()
                except Exception as e:
                    log.debug(f"Error disconnecting {kind} monitor: {type(e).__name__}")
        for kind, attr, _previous, current, _keys, factory in loaded:
            setattr(self, attr, current)
            for p in current:
                if p['id'] in self.monitors:
                    continue
                log.info(f"[{p['name']}] Connecting {kind} monitor...")
                try:
                    monitor = factory(p)
                    if monitor.connect():
                        self.monitors[p['id']] = monitor
                        log.info(f"[{p['name']}] Connected")
                except Exception as e:
                    log.warning(f"[{p['name']}] {kind} connect failed; retry next pass: {type(e).__name__}")

    def _licensed_inventory(self, bind=None):
        """{printer_id: (fingerprint, kind, attr, row, factory)}; raises if any family fails to load."""
        from core.license_monitor import connection_fingerprint
        inventory = {}
        for kind, attr, load, keys, factory in self._families():
            for p in load(bind=bind):
                fingerprint = connection_fingerprint(kind, *(p.get(k) for k in keys))
                inventory[p['id']] = (fingerprint, kind, attr, p, factory)
        return inventory

    def _session_fingerprints(self, bind):
        """Retirement's inventory source: read on its bounded engine (own connection, short timeouts)."""
        return {pid: entry[0] for pid, entry in self._licensed_inventory(bind).items()}

    def _licensed_pass(self):
        """License fleet lock enabled (B2c): the inventory pass, every 10 s.

        Sessions are never stopped in-process. A printer with a session that was
        removed, deactivated or reconfigured ends the process (exit 75; see
        core.license_monitor), and so does losing the license, on the retirement
        thread. A new or newly allowed printer becomes a session here: registered
        before its first connect, then retried by _check_reconnect like any other.
        """
        from core import license_lock
        try:
            inventory = self._licensed_inventory()
        except Exception as e:
            log.warning(f"printer reload failed; retry next pass: {type(e).__name__}")
            return
        self._retirement.check_inventory({pid: entry[0] for pid, entry in inventory.items()})
        pending = [pid for pid in inventory if not self._retirement.has(pid)]
        policy = self._retirement.current() if pending else None
        for pid in pending if policy is not None else ():
            if not license_lock.printer_allowed(policy, pid, 'M'):
                continue
            fingerprint, kind, attr, p, factory = inventory[pid]
            self._retirement.add(pid, fingerprint)
            getattr(self, attr).append(p)  # _check_reconnect retries a failed connect
            log.info(f"[{p['name']}] Connecting {kind} monitor...")
            try:
                monitor = factory(p)
                if monitor.connect():
                    self.monitors[pid] = monitor
                    log.info(f"[{p['name']}] Connected")
            except Exception as e:
                log.warning(f"[{p['name']}] {kind} connect failed; retry next check: {type(e).__name__}")

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

    def _reconnect_bambu(self, p):
        pid = p['id']
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
                    port=p['port'], model=p.get('model'), machine_type=p.get('machine_type'),
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
