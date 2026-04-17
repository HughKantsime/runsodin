"""
PrinterVisionThread — per-printer detection loop.

Captures frames from go2rtc, runs ONNX inference via VisionInferenceEngine,
manages confirmation buffers, and dispatches alerts on confirmed detections.
Frame storage delegated to frame_storage module.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import cv2
import numpy as np
from sqlalchemy import text

from core.db import engine
from core.db_compat import sql
from modules.vision.inference_engine import VisionInferenceEngine
from modules.vision import frame_storage

try:
    from core.ws_hub import push_event as ws_push
except ImportError:
    def ws_push(*a, **kw): pass

log = logging.getLogger('vision_monitor')

GO2RTC_BASE = 'http://127.0.0.1:1984'
ALERT_COOLDOWN = 60  # minimum seconds between same-type alerts per printer
DETECTION_TYPES = ['spaghetti', 'first_layer', 'detachment', 'build_plate_empty']


class PrinterVisionThread(threading.Thread):
    """Vision monitoring thread for a single actively-printing printer."""

    def __init__(
        self,
        printer_id: int,
        printer_name: str,
        engine: VisionInferenceEngine,
        settings: dict,
        current_layer: Optional[int],
        print_job_id: Optional[int],
        gcode_state: Optional[str] = None,
    ):
        super().__init__(daemon=True)
        self.printer_id = printer_id
        self.printer_name = printer_name
        self.engine = engine
        self.settings = settings
        self.current_layer = current_layer
        self.print_job_id = print_job_id
        self.gcode_state = gcode_state or 'RUNNING'
        self._running = True

        # Confirmation buffers: track consecutive detections
        self._history: Dict[str, List[bool]] = {
            'spaghetti': [],
            'first_layer': [],
            'detachment': [],
            'build_plate_empty': [],
        }
        # Alert cooldown tracking: detection_type -> last alert timestamp
        self._last_alert: Dict[str, float] = {}

    def stop(self):
        self._running = False

    def update_layer(self, layer: Optional[int], job_id: Optional[int], gcode_state: Optional[str] = None):
        self.current_layer = layer
        self.print_job_id = job_id
        if gcode_state:
            self.gcode_state = gcode_state

    def run(self):
        log.info(f"[{self.printer_name}] Vision thread started")
        interval = self.settings.get('capture_interval_sec', 10)
        while self._running:
            try:
                self._capture_and_analyze()
            except Exception as e:
                log.error(f"[{self.printer_name}] Vision error: {e}")
            time.sleep(interval)
        log.info(f"[{self.printer_name}] Vision thread stopped")

    def _capture_and_analyze(self):
        """Capture frame from go2rtc and run detection."""
        frame = self._capture_frame()
        if frame is None:
            return

        layer = self.current_layer or 0
        collect = self.settings.get('collect_training_data', 0)

        # Save training data if enabled (every frame, not just detections)
        if collect:
            frame_storage.save_training_frame(self.printer_id, frame)

        # Determine which detections to run based on layer
        checks = []
        if self.settings.get('spaghetti_enabled', 1) and self.engine.has_model('spaghetti'):
            checks.append(('spaghetti', self.settings.get('spaghetti_threshold', 0.65)))

        if self.settings.get('first_layer_enabled', 1) and layer <= 3 and self.engine.has_model('first_layer'):
            checks.append(('first_layer', self.settings.get('first_layer_threshold', 0.60)))

        if self.settings.get('detachment_enabled', 1) and layer > 5 and self.engine.has_model('detachment'):
            checks.append(('detachment', self.settings.get('detachment_threshold', 0.70)))

        for detection_type, threshold in checks:
            detections = self.engine.infer(detection_type, frame)
            # Get best detection above threshold
            best = max(detections, key=lambda d: d['confidence'], default=None)
            above = best is not None and best['confidence'] >= threshold

            self._update_history(detection_type, above)

            if self._should_trigger(detection_type):
                self._on_detection(detection_type, best, frame)
                # Reset history after triggering
                self._history[detection_type].clear()

        # Build plate empty detection — runs when printer is IDLE, not during printing
        if (self.settings.get('build_plate_empty_enabled', 0)
                and self.gcode_state in ('IDLE', 'FINISH', 'FINISHED')):
            threshold = self.settings.get('build_plate_empty_threshold', 0.70)
            is_empty, confidence = self._detect_build_plate_empty(frame)
            above = is_empty and confidence >= threshold
            self._update_history('build_plate_empty', above)
            if self._should_trigger('build_plate_empty'):
                # Synthetic detection dict for _on_detection
                det = {'confidence': confidence, 'bbox': []}
                self._on_detection('build_plate_empty', det, frame)
                self._history['build_plate_empty'].clear()

    def _detect_build_plate_empty(self, frame: np.ndarray) -> tuple:
        """Heuristic baseline for empty plate detection (Option B).

        Crops to center 60% of frame and checks color uniformity.
        Returns (is_empty: bool, confidence: float).
        If an ONNX model is available for 'build_plate_empty', uses that instead.
        """
        # Option A: use ONNX model if available
        if self.engine.has_model('build_plate_empty'):
            detections = self.engine.infer('build_plate_empty', frame)
            best = max(detections, key=lambda d: d['confidence'], default=None)
            if best:
                return (True, best['confidence'])
            return (False, 0.0)

        # Option B: heuristic — check if center region is uniform
        h, w = frame.shape[:2]
        # Crop to center 60%
        y1, y2 = int(h * 0.2), int(h * 0.8)
        x1, x2 = int(w * 0.2), int(w * 0.8)
        crop = frame[y1:y2, x1:x2]

        # Convert to grayscale for uniformity analysis
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        std_dev = float(np.std(gray))
        mean_val = float(np.mean(gray))

        # Empty plate heuristic: low standard deviation = uniform surface
        # Typical empty plate: std < 25, not too dark (mean > 40)
        if std_dev < 25 and mean_val > 40:
            # Higher confidence when more uniform
            confidence = max(0.0, min(1.0, 1.0 - (std_dev / 50.0)))
            return (True, confidence)
        return (False, 0.0)

    def _capture_frame(self) -> Optional[np.ndarray]:
        """Fetch a JPEG frame from go2rtc snapshot API."""
        import httpx
        url = f"{GO2RTC_BASE}/api/frame.jpeg?src=printer_{self.printer_id}"
        try:
            resp = httpx.get(url, timeout=5)
            if resp.status_code != 200:
                return None
            img_array = np.frombuffer(resp.content, dtype=np.uint8)
            frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            return frame
        except Exception as e:
            log.debug(f"[{self.printer_name}] Frame capture failed: {e}")
            return None

    def _update_history(self, detection_type: str, detected: bool):
        """Maintain sliding window of recent detection results."""
        hist = self._history[detection_type]
        hist.append(detected)
        # Keep only last 3 frames
        if len(hist) > 3:
            hist.pop(0)

    def _should_trigger(self, detection_type: str) -> bool:
        """Check confirmation strategy and cooldown."""
        # Cooldown check
        now = time.time()
        last = self._last_alert.get(detection_type, 0)
        if now - last < ALERT_COOLDOWN:
            return False

        hist = self._history[detection_type]

        if detection_type == 'first_layer':
            # Single frame sufficient (time-critical)
            return len(hist) >= 1 and hist[-1]
        else:
            # 2 of 3 consecutive frames (spaghetti, detachment)
            if len(hist) < 3:
                return False
            return sum(hist[-3:]) >= 2

    def _on_detection(self, detection_type: str, detection: dict, frame: np.ndarray):
        """Handle confirmed detection: save frame, insert DB record, dispatch alert."""
        import modules.notifications.event_dispatcher as printer_events
        self._last_alert[detection_type] = time.time()
        confidence = detection['confidence']
        bbox = detection['bbox']

        log.warning(
            f"[{self.printer_name}] {detection_type} detected "
            f"(confidence={confidence:.2f})"
        )

        # Save frame via frame_storage module
        frame_path = frame_storage.save_detection_frame(self.printer_id, frame, detection_type)

        # Insert detection record
        detection_id = self._insert_detection(
            detection_type, confidence, frame_path, bbox
        )

        # Map detection type to alert type
        alert_type_map = {
            'spaghetti': 'spaghetti_detected',
            'first_layer': 'first_layer_issue',
            'detachment': 'detachment_detected',
            'build_plate_empty': 'build_plate_empty_detected',
        }
        alert_type = alert_type_map[detection_type]

        severity_map = {
            'spaghetti': 'critical',
            'first_layer': 'warning',
            'detachment': 'critical',
            'build_plate_empty': 'info',
        }

        title_map = {
            'spaghetti': f"Spaghetti Detected: {self.printer_name}",
            'first_layer': f"First Layer Issue: {self.printer_name}",
            'detachment': f"Print Detachment: {self.printer_name}",
            'build_plate_empty': f"Plate Ready: {self.printer_name}",
        }

        # Dispatch alert through existing pipeline
        printer_events.dispatch_alert(
            alert_type=alert_type,
            severity=severity_map[detection_type],
            title=title_map[detection_type],
            message=f"Confidence: {confidence:.0%}",
            printer_id=self.printer_id,
            metadata={
                'detection_id': detection_id,
                'confidence': confidence,
                'detection_type': detection_type,
            }
        )

        # Push WebSocket event
        ws_push('vision_detection', {
            'printer_id': self.printer_id,
            'detection_type': detection_type,
            'confidence': confidence,
            'detection_id': detection_id,
            'print_job_id': self.print_job_id,
        })

        # Auto-pause if enabled
        if self.settings.get('auto_pause', 0):
            self._auto_pause()

    def _insert_detection(
        self, detection_type: str, confidence: float,
        frame_path: str, bbox: list
    ) -> Optional[int]:
        """Insert detection record into vision_detections table."""
        try:
            with engine.begin() as conn:
                insert_sql = (
                    f"INSERT INTO vision_detections"
                    f" (printer_id, print_job_id, detection_type, confidence,"
                    f"  status, frame_path, bbox_json, created_at)"
                    f" VALUES (:pid, :jid, :dtype, :conf, 'pending', :fpath, :bbox, {sql.now()})")
                params = {
                    "pid": self.printer_id,
                    "jid": self.print_job_id,
                    "dtype": detection_type,
                    "conf": confidence,
                    "fpath": frame_path,
                    "bbox": json.dumps(bbox),
                }
                if sql.is_sqlite:
                    conn.execute(text(insert_sql), params)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                    detection_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                else:
                    detection_id = conn.execute(text(insert_sql + " RETURNING id"), params).scalar()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                return detection_id
        except Exception as e:
            log.error(f"Failed to insert detection: {e}")
            return None

    def _auto_pause(self):
        """Pause the printer using the appropriate adapter."""
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT api_type, api_host, api_key FROM printers WHERE id = :pid"),
                    {"pid": self.printer_id}
                ).mappings().fetchone()
            if not row:
                return

            api_type = row['api_type']
            api_host = row['api_host']
            api_key = row['api_key']
            success = False

            if api_type == 'moonraker':
                from modules.printers.adapters.moonraker import MoonrakerPrinter
                adapter = MoonrakerPrinter(api_host)
                success = adapter.pause_print()

            elif api_type == 'bambu':
                from core.crypto import decrypt
                from modules.printers.telemetry.feature_flag import is_v2_enabled
                creds = decrypt(api_key)
                serial, access_code = creds.split('|', 1)
                if is_v2_enabled():
                    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
                    from modules.printers.telemetry.bambu.session import run_command
                    config = BambuAdapterConfig(
                        printer_id=f"vision-pause-{self.printer_id}",
                        serial=serial,
                        host=api_host,
                        access_code=access_code,
                    )
                    success = run_command(config, "pause_print")
                else:
                    from modules.printers.adapters.bambu import BambuPrinter
                    adapter = BambuPrinter(api_host, serial, access_code)
                    if adapter.connect():
                        success = adapter.pause_print()
                        adapter.disconnect()

            elif api_type == 'prusalink':
                from modules.printers.adapters.prusalink import PrusaLinkPrinter
                adapter = PrusaLinkPrinter(api_host, api_key=api_key or '')
                # PrusaLink pause requires job_id; try current running job
                with engine.connect() as conn2:
                    jrow = conn2.execute(
                        text("SELECT job_id FROM print_jobs WHERE printer_id = :pid AND status = 'running' "
                        "ORDER BY id DESC LIMIT 1"),
                        {"pid": self.printer_id}
                    ).fetchone()
                if jrow and jrow[0]:
                    success = adapter.pause_print(int(jrow[0]))

            elif api_type == 'elegoo':
                from modules.printers.adapters.elegoo import ElegooPrinter
                adapter = ElegooPrinter(api_host)
                success = adapter.pause_print()

            if success:
                log.info(f"[{self.printer_name}] Auto-paused printer")
                # Update gcode_state in DB
                with engine.begin() as conn3:
                    conn3.execute(
                        text("UPDATE printers SET gcode_state = 'PAUSED' WHERE id = :pid"),
                        {"pid": self.printer_id}
                    )
                    # Update detection status
                    conn3.execute(
                        text("""UPDATE vision_detections SET status = 'auto_paused'
                        WHERE printer_id = :pid AND status = 'pending'
                        ORDER BY id DESC LIMIT 1"""),
                        {"pid": self.printer_id}
                    )
            else:
                log.error(f"[{self.printer_name}] Auto-pause failed")

        except Exception as e:
            log.error(f"[{self.printer_name}] Auto-pause error: {e}")
