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

# Resolved at use time: never a stale engine bound at import.
from core.db import live_engine as engine
from core.db_compat import execute_insert_returning_id, sql
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
# Collected training frames are stored as status='training' rows, never as detections.
TRAINING_SAMPLE = 'training_sample'
TRAINING_STATUS = 'training'
DEFAULT_CAPTURE_INTERVAL = 10
MAX_CAPTURE_INTERVAL = 86400  # same bound as PATCH /api/printers/{id}/vision


DEFAULT_MAX_VISION_THREADS = 16
MAX_VISION_THREADS_LIMIT = 256  # same bound as PATCH /api/vision/settings max_threads


def max_vision_threads(value) -> int:
    """A valid concurrent-thread cap (1..256), else the default 16."""
    if type(value) is int and 1 <= value <= MAX_VISION_THREADS_LIMIT:
        return value
    return DEFAULT_MAX_VISION_THREADS


def capture_interval(value) -> int:
    """A valid capture interval in seconds (1..86400), else the default."""
    if type(value) is int and 1 <= value <= MAX_CAPTURE_INTERVAL:
        return value
    return DEFAULT_CAPTURE_INTERVAL


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
        self.gcode_state = gcode_state
        self._running = True
        # Interruptible sleep: stop() and interval changes wake the thread immediately.
        self._wake = threading.Event()

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
        self._wake.set()

    def update_settings(self, settings: dict):
        """Apply persisted settings to the running thread; a new interval takes effect now."""
        previous = capture_interval(self.settings.get('capture_interval_sec'))
        self.settings = settings
        if capture_interval(settings.get('capture_interval_sec')) != previous:
            self._wake.set()

    def update_layer(self, layer: Optional[int], job_id: Optional[int], gcode_state: Optional[str] = None):
        self.current_layer = layer
        self.print_job_id = job_id
        if gcode_state != self.gcode_state:
            for history in self._history.values():
                history.clear()
        self.gcode_state = gcode_state

    def run(self):
        log.info(f"[{self.printer_name}] Vision thread started")
        try:
            while self._running:
                try:
                    self._capture_and_analyze()
                except Exception as e:
                    log.error(f"[{self.printer_name}] Vision error: {e}")
                # Clear the wake flag BEFORE re-checking _running and reading the interval, so a
                # stop() or interval change landing in between is never erased.
                self._wake.clear()
                if not self._running:
                    break
                # Re-read the interval every loop so a changed setting applies to this print.
                interval = capture_interval(self.settings.get('capture_interval_sec'))
                try:
                    self._wake.wait(interval)
                except Exception as e:
                    log.error(f"[{self.printer_name}] Vision sleep error: {e}")
                    time.sleep(1)
        except BaseException:
            log.exception(f"[{self.printer_name}] Vision thread died")
            raise
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
            path = frame_storage.save_training_frame(self.printer_id, frame)
            self._insert_training_sample(path)

        if not hasattr(self, '_history_generations'):
            self._history_generations = {}
        for kind in DETECTION_TYPES:
            generation, state = self.engine.configuration_state(kind)
            if (self._history_generations.get(kind) != generation
                    or state == 'unavailable'):
                self._history[kind].clear()
            self._history_generations[kind] = generation

        # Determine which detections to run based on layer
        checks = []
        if self.gcode_state == 'RUNNING' and self.settings.get('spaghetti_enabled', 1) and self.engine.has_model('spaghetti'):
            checks.append(('spaghetti', self.settings.get('spaghetti_threshold', 0.65)))

        if self.gcode_state == 'RUNNING' and self.settings.get('first_layer_enabled', 1) and layer <= 3 and self.engine.has_model('first_layer'):
            checks.append(('first_layer', self.settings.get('first_layer_threshold', 0.60)))

        if self.gcode_state == 'RUNNING' and self.settings.get('detachment_enabled', 1) and layer > 5 and self.engine.has_model('detachment'):
            checks.append(('detachment', self.settings.get('detachment_threshold', 0.70)))

        for detection_type, threshold in checks:
            if self.gcode_state != 'RUNNING':
                break
            observation = self.engine.infer_observation(detection_type, frame)
            if not self.engine.observation_current(detection_type, observation):
                self._history[detection_type].clear()
                continue
            detections = observation.detections
            if self.gcode_state != 'RUNNING':
                break
            with self.engine.decision_guard(detection_type, observation) as admitted:
                if not admitted:
                    self._history[detection_type].clear()
                    continue
                if self._history_generations.get(detection_type) != observation.generation:
                    self._history[detection_type].clear()
                self._history_generations[detection_type] = observation.generation
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
            plate = self._detect_build_plate_empty(frame)
            if plate is None:
                self._history['build_plate_empty'].clear()
                return
            is_empty, confidence, observation, generation = plate
            with self.engine.decision_guard('build_plate_empty', observation, generation) as admitted:
                if not admitted:
                    self._history['build_plate_empty'].clear()
                    return
                admitted_generation = observation.generation if observation is not None else generation
                if self._history_generations.get('build_plate_empty') != admitted_generation:
                    self._history['build_plate_empty'].clear()
                self._history_generations['build_plate_empty'] = admitted_generation
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
        generation, state = self.engine.configuration_state('build_plate_empty')
        if state == 'unavailable':
            return None
        if state == 'ready':
            observation = self.engine.infer_observation('build_plate_empty', frame)
            if not self.engine.observation_current('build_plate_empty', observation):
                return None
            best = max(observation.detections, key=lambda d: d['confidence'], default=None)
            return (True, best['confidence'], observation, generation) if best else (False, 0.0, observation, generation)

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
        if not self.engine.heuristic_current('build_plate_empty', generation):
            return None
        if std_dev < 25 and mean_val > 40:
            # Higher confidence when more uniform
            confidence = max(0.0, min(1.0, 1.0 - (std_dev / 50.0)))
            return (True, confidence, None, generation)
        return (False, 0.0, None, generation)

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
        if (self.settings.get('auto_pause', 0)
                and detection_type in ('spaghetti', 'first_layer', 'detachment')
                and self.gcode_state == 'RUNNING'):
            self._auto_pause()

    def _insert_training_sample(self, frame_path: str) -> None:
        """Index a collected frame for annotation; not a detection (status 'training')."""
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO vision_detections (printer_id, print_job_id, detection_type, confidence, "
                    f"status, frame_path, created_at) VALUES (:pid, :jid, '{TRAINING_SAMPLE}', 0, 'training', :fpath, {sql.now()})"),
                    {"pid": self.printer_id, "jid": self.print_job_id, "fpath": frame_path})
        except Exception:
            log.warning(f"[{self.printer_name}] Training sample could not be indexed")

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
                detection_id = execute_insert_returning_id(conn, insert_sql, params)
                return detection_id
        except Exception as e:
            log.error(f"Failed to insert detection: {e}")
            return None

    def _auto_pause(self):
        """Pause only with positive running telemetry, using the appropriate adapter."""
        if self.gcode_state != 'RUNNING':
            return
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
                    from modules.printers.telemetry.bambu.broker_policy import resolve_bambu_broker_config
                    from modules.printers.telemetry.bambu.session import run_command
                    endpoint = resolve_bambu_broker_config(
                        api_host, printer_id=f"vision-pause-{self.printer_id}",
                    )
                    config = BambuAdapterConfig(
                        printer_id=f"vision-pause-{self.printer_id}",
                        serial=serial,
                        host=endpoint.host,
                        port=endpoint.port,
                        use_tls=endpoint.use_tls,
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
                self.update_layer(self.current_layer, self.print_job_id, 'PAUSED')
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
                        WHERE id = (SELECT id FROM vision_detections
                            WHERE printer_id = :pid AND status = 'pending'
                            ORDER BY id DESC LIMIT 1)"""),
                        {"pid": self.printer_id}
                    )
            else:
                log.error(f"[{self.printer_name}] Auto-pause failed")

        except Exception as e:
            log.error(f"[{self.printer_name}] Auto-pause error: {e}")
