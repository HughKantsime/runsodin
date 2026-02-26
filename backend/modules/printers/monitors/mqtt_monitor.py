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

import sqlite3
import time
import signal
import logging
from typing import Dict

import core.crypto as crypto
from core.db_utils import get_db
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


class MQTTMonitorDaemon:
    """Main daemon that monitors all printers."""

    def __init__(self):
        self.monitors: Dict[int, PrinterMonitor] = {}
        self._running = False

    def load_printers(self):
        """Load Bambu printers from database."""
        # Get encryption key
        key = os.environ.get('ENCRYPTION_KEY')
        if not key:
            log.error("ENCRYPTION_KEY not set")
            return []

        with get_db(row_factory=sqlite3.Row) as conn:
            cur = conn.cursor()
            cur.execute('''
                SELECT id, name, model, api_host, api_key
                FROM printers
                WHERE api_host IS NOT NULL AND api_key IS NOT NULL AND api_key != ''
            ''')

            printers = []
            for row in cur.fetchall():
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

        return printers

    def load_moonraker_printers(self):
        """Load Moonraker-based printers from database."""
        with get_db(row_factory=sqlite3.Row) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, name, api_host
                FROM printers
                WHERE api_type = 'moonraker'
                  AND api_host IS NOT NULL
                  AND api_host != ''
                  AND is_active = 1
            """)

            printers = []
            for row in cur.fetchall():
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
        self._running = True
        printers = self.load_printers()

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
                # Every 30s, check for dead connections and reconnect
                if time.time() - self._last_reconnect_check >= 30:
                    self._check_reconnect()
                    self._last_reconnect_check = time.time()

                # Every 60s, check for newly added printers
                if time.time() - self._last_printer_reload >= 60:
                    self._check_new_printers()
                    self._last_printer_reload = time.time()
        except KeyboardInterrupt:
            log.info("Shutting down...")

        self.stop()

    def _check_new_printers(self):
        """Check for newly added printers and connect to them."""
        try:
            current_printers = self.load_printers()
            current_ids = {p['id'] for p in current_printers}
            monitored_ids = set(self.monitors.keys())
            new_ids = current_ids - monitored_ids
            if not new_ids:
                return
            for p in current_printers:
                if p['id'] in new_ids:
                    log.info(f"New printer detected: {p['name']}, connecting...")
                    monitor = PrinterMonitor(
                        printer_id=p['id'],
                        name=p['name'],
                        ip=p['ip'],
                        serial=p['serial'],
                        access_code=p['access_code']
                    )
                    if monitor.connect():
                        self.monitors[p['id']] = monitor
                        log.info(f"[{p['name']}] Connected")
            if MOONRAKER_AVAILABLE:
                mk_printers = self.load_moonraker_printers()
                for p in mk_printers:
                    if p['id'] not in self.monitors:
                        log.info(f"New Moonraker printer: {p['name']}")
                        monitor = MoonrakerMonitor(
                            printer_id=p['id'],
                            name=p['name'],
                            host=p['host'],
                            port=p['port'],
                        )
                        if monitor.connect():
                            self.monitors[p['id']] = monitor
        except Exception as e:
            log.warning(f"Error checking for new printers: {e}")

    def _check_reconnect(self):
        """Check for dead connections and attempt reconnection."""
        # Check Bambu printers
        for p in self._all_printers:
            pid = p['id']
            monitor = self.monitors.get(pid)

            if monitor is None:
                # Never connected - try again
                log.info(f"[{p['name']}] Attempting initial connection...")
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
                continue

            # Check if connection is dead
            is_dead = False
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
                except Exception:
                    pass
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

        # Check Moonraker printers
        for p in self._all_moonraker:
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
                continue

            # Check staleness for Moonraker
            if hasattr(monitor, '_last_heartbeat') and monitor._last_heartbeat > 0:
                if time.time() - monitor._last_heartbeat > 120:
                    log.info(f"[{monitor.name}] Moonraker stale, reconnecting...")
                    try:
                        monitor.disconnect()
                    except Exception:
                        pass
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
        for monitor in self.monitors.values():
            monitor.disconnect()
        log.info("All monitors stopped")


def main():
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
    main()
