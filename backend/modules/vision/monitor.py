#!/usr/bin/env python3
"""
Vision Monitor Daemon — Local AI detection for 3D print failures.

Captures frames from printer cameras during active prints, runs ONNX inference
for three failure types (spaghetti, first layer, detachment), and integrates
with the existing alert/event pipeline.

Architecture:
  - One thread per actively-printing printer with camera
  - Frame capture: GET http://127.0.0.1:1984/api/frame.jpeg?src=printer_{id}
  - Preprocessing: cv2.imdecode -> resize 640x640 -> normalize -> NCHW
  - Inference: onnxruntime.InferenceSession.run()
  - Post-processing: NMS, confidence filter, alert dispatch

Managed by supervisord (priority 35, after go2rtc).
"""

import os
import sys
sys.path.insert(0, os.environ.get('BACKEND_PATH', '/app/backend'))

import json
import time
import signal
import logging
from typing import Dict, Optional

from sqlalchemy import text

try:
    import cv2
    import numpy as np
except ImportError:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    logging.getLogger('vision_monitor').warning(
        'Vision monitor disabled — cv2/numpy not available. '
        'This is normal if opencv is not installed in this environment.'
    )
    sys.exit(0)  # Exit cleanly so supervisor doesn't restart

from core.db import engine
from modules.vision.inference_engine import VisionInferenceEngine
from modules.vision.detection_thread import PrinterVisionThread, capture_interval, max_vision_threads
from modules.vision import frame_storage

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('vision_monitor')

SCAN_INTERVAL = 15          # seconds between main loop scans
MODEL_RELOAD_INTERVAL = 60  # seconds between model reload checks
FRAME_CLEANUP_INTERVAL = 3600  # hourly frame cleanup
THREAD_JOIN_TIMEOUT = 5     # seconds to wait for a stopped thread to exit


class VisionMonitorDaemon:
    """Main daemon: manages per-printer vision threads."""

    def __init__(self):
        self._running = True
        self._engine = VisionInferenceEngine()
        self._threads: Dict[int, PrinterVisionThread] = {}
        # Stopped threads that outlived THREAD_JOIN_TIMEOUT; their printer gets no new thread
        # until they exit, so re-enabling never runs two threads for one camera.
        self._stopping: Dict[int, PrinterVisionThread] = {}
        self._last_model_reload = 0
        self._last_cleanup = 0

    def run(self):
        log.info("Vision Monitor starting...")

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Initial model load
        self._engine.reload_models()

        while self._running:
            try:
                self._scan_printers()

                # Periodic model reload
                now = time.time()
                if now - self._last_model_reload > MODEL_RELOAD_INTERVAL:
                    self._engine.reload_models()
                    self._last_model_reload = now

                self._engine.publish_runtime_status()

                # Periodic frame cleanup
                if now - self._last_cleanup > FRAME_CLEANUP_INTERVAL:
                    frame_storage.cleanup_old_frames()
                    self._last_cleanup = now

            except Exception as e:
                log.error(f"Main loop error: {e}")

            time.sleep(SCAN_INTERVAL)

        # Shutdown
        log.info("Vision Monitor shutting down...")
        for pid, thread in self._threads.items():
            thread.stop()
        for thread in self._threads.values():
            thread.join(timeout=5)
        log.info("Vision Monitor stopped")

    def _signal_handler(self, signum, frame):
        log.info(f"Received signal {signum}, shutting down...")
        self._running = False

    @staticmethod
    def _has_effective_camera(row) -> bool:
        if row['camera_url']:
            return True
        from types import SimpleNamespace
        from modules.printers.route_utils import get_camera_url
        try:
            return get_camera_url(SimpleNamespace(
                camera_url=None, api_type=row['api_type'], api_key=row['api_key'],
                api_host=row['api_host'], model=row['model'])) is not None
        except Exception as e:
            log.error(f"Camera URL resolution failed for printer {row['id']}: {e}")
            return False

    def _global_settings(self) -> dict:
        """Global Vigil AI settings from system_config 'vision_settings' ({} when unset/unreadable)."""
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT value FROM system_config WHERE key = 'vision_settings'")
                ).fetchone()
        except Exception as e:
            log.error(f"Failed to read global vision settings: {e}")
            return {}
        value = row[0] if row else None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                log.error("Global vision settings are not valid JSON; using defaults")
                return {}
        return value if isinstance(value, dict) else {}

    def _globally_enabled(self) -> bool:
        """The global Vigil AI switch (default on)."""
        return self._global_settings().get('enabled', True) is not False

    def _max_threads(self) -> int:
        """Configured cap on concurrent vision threads (vision_settings.max_threads, default 16)."""
        return max_vision_threads(self._global_settings().get('max_threads'))

    def _publish_monitor_status(self, max_threads: int, waiting: list) -> None:
        """Record the thread cap state for GET /api/vision/monitor-status (written on change only)."""
        status = {'max_threads': max_threads, 'active_threads': len(self._threads), 'capped_printer_ids': waiting}
        if status == getattr(self, '_last_status', None):
            return
        try:
            payload = json.dumps(status)
            with engine.begin() as conn:
                updated = conn.execute(text("UPDATE system_config SET value = :v WHERE key = 'vision_monitor_status'"),
                                       {'v': payload}).rowcount
                if not updated:
                    conn.execute(text("INSERT INTO system_config (key, value) VALUES ('vision_monitor_status', :v)"),
                                 {'v': payload})
            self._last_status = status
        except Exception as e:
            log.error(f"Failed to publish vision monitor status: {e}")

    def _scan_printers(self):
        """Query DB for printers that should have vision monitoring."""
        try:
            with engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT p.id, p.name, p.nickname, p.gcode_state,
                           p.camera_url, p.api_type, p.api_key, p.api_host, p.model,
                           pj.id as print_job_id, pj.current_layer,
                           vs.enabled, vs.spaghetti_enabled, vs.spaghetti_threshold,
                           vs.first_layer_enabled, vs.first_layer_threshold,
                           vs.detachment_enabled, vs.detachment_threshold,
                           vs.build_plate_empty_enabled, vs.build_plate_empty_threshold,
                           vs.auto_pause, vs.capture_interval_sec,
                           vs.collect_training_data
                    FROM printers p
                    LEFT JOIN vision_settings vs ON vs.printer_id = p.id
                    LEFT JOIN print_jobs pj ON pj.printer_id = p.id
                        AND pj.status = 'running'
                    WHERE p.is_active IS TRUE
                      AND p.camera_enabled IS TRUE
                      AND (p.gcode_state = 'RUNNING'
                           OR (p.gcode_state IN ('IDLE', 'FINISH', 'FINISHED')
                               AND COALESCE(vs.build_plate_empty_enabled, 0) = 1))
                """)).mappings().fetchall()
        except Exception as e:
            log.error(f"Failed to scan printers: {e}")
            return

        # Global kill switch (PATCH /api/vision/settings {enabled: false}) stops all monitoring.
        if not self._globally_enabled():
            rows = []

        active_ids = set()
        wanted = []
        for row in sorted(rows, key=lambda item: item['id']):
            pid = row['id']

            # Effective camera: the stored URL, else the one derived from credentials
            # (auto-configured Bambu X1C/H2D RTSP); same resolution go2rtc sync uses.
            if not self._has_effective_camera(row):
                continue

            # Check if vision is enabled (default yes if no settings row); a disabled
            # printer is not active, so its running thread is stopped below.
            if row['enabled'] is not None and not row['enabled']:
                continue
            active_ids.add(pid)

            def flag(name, default, row=row):
                return row[name] if row[name] is not None else default

            # Explicit None checks: a stored 0 / 0.0 is a real setting, not "unset".
            settings = {
                'spaghetti_enabled': flag('spaghetti_enabled', 1),
                'spaghetti_threshold': flag('spaghetti_threshold', 0.65),
                'first_layer_enabled': flag('first_layer_enabled', 1),
                'first_layer_threshold': flag('first_layer_threshold', 0.60),
                'detachment_enabled': flag('detachment_enabled', 1),
                'detachment_threshold': flag('detachment_threshold', 0.70),
                'build_plate_empty_enabled': flag('build_plate_empty_enabled', 0),
                'build_plate_empty_threshold': flag('build_plate_empty_threshold', 0.70),
                'auto_pause': flag('auto_pause', 0),
                'capture_interval_sec': capture_interval(row['capture_interval_sec']),
                'collect_training_data': flag('collect_training_data', 0),
            }
            wanted.append((pid, row, settings))

        # Stop threads for printers no longer active first (frees cap slots): signal every
        # thread, then join them all against one shared deadline so the loop never stalls
        # for THREAD_JOIN_TIMEOUT per printer.
        stopping = [(pid, self._threads.pop(pid)) for pid in list(self._threads) if pid not in active_ids]
        for _, thread in stopping:
            thread.stop()
        deadline = time.monotonic() + THREAD_JOIN_TIMEOUT
        for pid, thread in stopping:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self._stopping[pid] = thread
                log.warning(f"Vision thread for printer {pid} did not exit within {THREAD_JOIN_TIMEOUT}s")
            log.info(f"Stopped vision thread for printer {pid}")

        max_threads = self._max_threads()
        waiting = []
        for pid, row, settings in wanted:
            if pid in self._threads and not self._threads[pid].is_alive():
                log.warning(f"Vision thread for printer {pid} died; restarting it")
                del self._threads[pid]
            previous = self._stopping.get(pid)
            if previous is not None:
                if previous.is_alive():
                    log.warning(f"Previous vision thread for printer {pid} still exiting; not starting another")
                    continue
                del self._stopping[pid]

            if pid in self._threads:
                # Update layer info, gcode_state and the persisted settings on the existing
                # thread so sensitivity/auto-pause changes apply to a running print.
                self._threads[pid].update_layer(
                    row['current_layer'], row['print_job_id'], row['gcode_state']
                )
                self._threads[pid].update_settings(settings)
                continue
            if len(self._threads) >= max_threads:
                waiting.append(pid)
                continue
            printer_name = row['nickname'] or row['name']
            thread = PrinterVisionThread(
                printer_id=pid,
                printer_name=printer_name,
                engine=self._engine,
                settings=settings,
                current_layer=row['current_layer'],
                print_job_id=row['print_job_id'],
                gcode_state=row['gcode_state'],
            )
            thread.start()
            self._threads[pid] = thread
            log.info(f"Started vision thread for {printer_name} (id={pid})")

        if waiting and waiting != getattr(self, '_last_waiting', []):
            log.warning(f"Vision thread cap {max_threads} reached; not monitoring printers {waiting} "
                        "(raise vision_settings.max_threads)")
        self._last_waiting = waiting
        self._publish_monitor_status(max_threads, waiting)

if __name__ == '__main__':
    daemon = VisionMonitorDaemon()
    daemon.run()
