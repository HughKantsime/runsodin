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
import sqlite3
from typing import Dict, Optional

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

from core.db_utils import get_db
from modules.vision.inference_engine import VisionInferenceEngine
from modules.vision.detection_thread import PrinterVisionThread
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


class VisionMonitorDaemon:
    """Main daemon: manages per-printer vision threads."""

    def __init__(self):
        self._running = True
        self._engine = VisionInferenceEngine()
        self._threads: Dict[int, PrinterVisionThread] = {}
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

    def _scan_printers(self):
        """Query DB for printers that should have vision monitoring."""
        try:
            with get_db(row_factory=sqlite3.Row) as conn:
                cur = conn.cursor()

                # Find printers: active, camera enabled, camera URL set,
                # either printing (for failure detection) or idle with build_plate_empty enabled
                cur.execute("""
                    SELECT p.id, p.name, p.nickname, p.gcode_state,
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
                    WHERE p.is_active = 1
                      AND p.camera_enabled = 1
                      AND p.camera_url IS NOT NULL
                      AND (p.gcode_state = 'RUNNING'
                           OR (p.gcode_state IN ('IDLE', 'FINISH', 'FINISHED')
                               AND COALESCE(vs.build_plate_empty_enabled, 0) = 1))
                """)
                rows = cur.fetchall()
        except Exception as e:
            log.error(f"Failed to scan printers: {e}")
            return

        active_ids = set()
        for row in rows:
            pid = row['id']
            active_ids.add(pid)

            # Check if vision is enabled (default yes if no settings row)
            if row['enabled'] is not None and not row['enabled']:
                continue

            printer_name = row['nickname'] or row['name']
            settings = {
                'spaghetti_enabled': row['spaghetti_enabled'] if row['spaghetti_enabled'] is not None else 1,
                'spaghetti_threshold': row['spaghetti_threshold'] or 0.65,
                'first_layer_enabled': row['first_layer_enabled'] if row['first_layer_enabled'] is not None else 1,
                'first_layer_threshold': row['first_layer_threshold'] or 0.60,
                'detachment_enabled': row['detachment_enabled'] if row['detachment_enabled'] is not None else 1,
                'detachment_threshold': row['detachment_threshold'] or 0.70,
                'build_plate_empty_enabled': row['build_plate_empty_enabled'] if row['build_plate_empty_enabled'] is not None else 0,
                'build_plate_empty_threshold': row['build_plate_empty_threshold'] or 0.70,
                'auto_pause': row['auto_pause'] or 0,
                'capture_interval_sec': row['capture_interval_sec'] or 10,
                'collect_training_data': row['collect_training_data'] or 0,
            }

            if pid in self._threads:
                # Update layer info and gcode_state on existing thread
                self._threads[pid].update_layer(
                    row['current_layer'], row['print_job_id'], row['gcode_state']
                )
            else:
                # Start new thread
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

        # Stop threads for printers no longer active
        for pid in list(self._threads.keys()):
            if pid not in active_ids:
                self._threads[pid].stop()
                del self._threads[pid]
                log.info(f"Stopped vision thread for printer {pid}")


if __name__ == '__main__':
    daemon = VisionMonitorDaemon()
    daemon.run()
