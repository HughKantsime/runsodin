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
from core.printer_utils import is_idle_state, is_printing_state
from core.db_compat import execute_insert_returning_id, sql
from modules.vision.inference_engine import VisionInferenceEngine
from modules.vision import frame_storage, training_capture

try:
    from core.ws_hub import push_event as ws_push
except ImportError:
    def ws_push(*a, **kw): pass

log = logging.getLogger('vision_monitor')

from modules.printers.go2rtc_access import go2rtc_api_url, go2rtc_client_kwargs

GO2RTC_BASE = go2rtc_api_url()  # loopback in the single container; ODIN_GO2RTC_API_URL otherwise
ALERT_COOLDOWN = 60  # minimum seconds between same-type alerts per printer
DETECTION_TYPES = ['spaghetti', 'first_layer', 'detachment', 'build_plate_empty']
# Collected training frames are stored as status='training' rows, never as detections.
TRAINING_SAMPLE = 'training_sample'
TRAINING_STATUS = 'training'
DEFAULT_CAPTURE_INTERVAL = 10
MAX_CAPTURE_INTERVAL = 86400  # same bound as PATCH /api/printers/{id}/vision
PROCESS_STARTED_AT = time.time()
AUTO_PAUSE_GRACE_SECONDS = 60
AUTO_PAUSE_MIN_FRAMES = 3


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
        self._analyzed_frames = 0
        self._first_analysis_at = None
        # Training capture: sampling clock, the previous tick's frame, and the post-detection burst.
        self._sampler = training_capture.TrainingSampler()
        self._previous_frame = None        # (job id, frame) of the last tick, only while collecting
        self._burst_previous = None        # that frame, if it belongs to the current job
        self._burst_after = 0              # frames still to save after a detection
        self._burst_after_job = None
        self._saved_this_tick = False

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
        if job_id != self.print_job_id:
            self._analyzed_frames = 0
            self._first_analysis_at = None
            self._last_alert.clear()
            self._reset_training_capture()
            for history in self._history.values():
                history.clear()
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

        layer = self.current_layer
        analyzed_job = self.print_job_id
        counted_frame = False
        collect = self.settings.get('collect_training_data', 0)

        # Training frames are sampled (every N seconds) plus a burst around each detection,
        # not saved on every detection tick. Detection and review frames stay full size.
        # Whether the tick before was itself saved (by the sampler or a burst, which runs later in
        # the tick): read it before the reset so that frame is never saved twice.
        self._burst_previous_saved = self._saved_this_tick
        self._saved_this_tick = False
        previous, self._previous_frame = self._previous_frame, ((analyzed_job, frame) if collect else None)
        self._burst_previous = previous if previous is not None and previous[0] == analyzed_job else None
        if collect:
            self._collect_training(frame)

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
        if is_printing_state(self.gcode_state) and self.settings.get('spaghetti_enabled', 1) and self.engine.has_model('spaghetti'):
            checks.append(('spaghetti', self.settings.get('spaghetti_threshold', 0.65)))

        if is_printing_state(self.gcode_state) and self.settings.get('first_layer_enabled', 1) and layer is not None and 0 <= layer <= 3 and self.engine.has_model('first_layer'):
            checks.append(('first_layer', self.settings.get('first_layer_threshold', 0.60)))

        if is_printing_state(self.gcode_state) and self.settings.get('detachment_enabled', 1) and layer is not None and layer > 5 and self.engine.has_model('detachment'):
            checks.append(('detachment', self.settings.get('detachment_threshold', 0.70)))

        for detection_type, threshold in checks:
            if not is_printing_state(self.gcode_state):
                break
            observation = self.engine.infer_observation(detection_type, frame)
            if not self.engine.observation_current(detection_type, observation):
                self._history[detection_type].clear()
                continue
            detections = observation.detections
            if not is_printing_state(self.gcode_state):
                break
            with self.engine.decision_guard(detection_type, observation) as admitted:
                if not admitted:
                    self._history[detection_type].clear()
                    continue
                if self.print_job_id != analyzed_job:
                    break
                if not counted_frame:
                    if self._first_analysis_at is None:
                        self._first_analysis_at = time.monotonic()
                    self._analyzed_frames += 1
                    counted_frame = True
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
                and is_idle_state(self.gcode_state)):
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
            resp = httpx.get(url, timeout=5, **go2rtc_client_kwargs())
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

        if detection_type != 'build_plate_empty' and self.settings.get('collect_training_data', 0):
            self._detection_burst(frame)

        from core.base import AlertType, AlertSeverity

        # Map detection type to alert type
        alert_type_map = {
            'spaghetti': AlertType.SPAGHETTI_DETECTED,
            'first_layer': AlertType.FIRST_LAYER_ISSUE,
            'detachment': AlertType.DETACHMENT_DETECTED,
            'build_plate_empty': AlertType.BED_COOLED,
        }
        alert_type = alert_type_map[detection_type]

        severity_map = {
            'spaghetti': AlertSeverity.CRITICAL,
            'first_layer': AlertSeverity.WARNING,
            'detachment': AlertSeverity.CRITICAL,
            'build_plate_empty': AlertSeverity.INFO,
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
                and is_printing_state(self.gcode_state)):
            self._auto_pause()

    def _training_capture_allowed(self) -> bool:
        """No training frame is written for controlled work.

        Viewing these frames is already restricted, but a saved picture of a controlled part
        would still sit on disk for the retention period. Rechecked before every write;
        if the check or the running print's job link cannot be verified, nothing is saved.
        """
        try:
            from sqlalchemy.orm import Session
            from core.controlled_parts import any_controlled_data, policies
            with engine.connect() as conn, Session(bind=conn) as db:
                if not any_controlled_data(db):
                    allowed = True
                elif self.print_job_id is None:
                    # A print ODIN cannot tie to a job cannot be shown to be uncontrolled.
                    allowed = False
                else:
                    # An empty policy list also means an absent job link, not proof that
                    # the print is uncontrolled. Verify its job and printer first.
                    job_id = db.execute(text(
                        'SELECT j.id FROM print_jobs pj JOIN jobs j ON j.id=pj.scheduled_job_id '
                        'WHERE pj.id=:pid AND pj.printer_id=:printer'),
                        {'pid': self.print_job_id, 'printer': self.printer_id}).scalar()
                    allowed = job_id is not None and not policies(db, 'job', job_id)
        except Exception:
            log.warning(f"[{self.printer_name}] Controlled-work check failed; training frame not saved")
            allowed = False
        return allowed

    def _reset_training_capture(self) -> None:
        """A new print starts its own sampling clock; nothing carries over from the last job."""
        self._sampler.reset()
        self._previous_frame = None
        self._burst_previous = None
        self._burst_after = 0

    def _training_settings(self) -> dict:
        return self.settings.get('training_capture') or training_capture.resolve_config({}, None)

    def _collect_training(self, frame: np.ndarray) -> None:
        """Save the scheduled training frame (every N seconds) or the frame after a detection."""
        config = self._training_settings()
        after = self._burst_after if self._burst_after_job == self.print_job_id else 0
        if after > 0:
            self._burst_after = after - 1
        elif not self._sampler.due(training_capture.training_interval(config.get('interval_sec'))):
            return
        self._save_training_frame(frame)

    def _detection_burst(self, frame: np.ndarray) -> None:
        """Up to BURST_FRAMES training frames around a detection: the tick before, this one, the next."""
        try:
            # Read before saving the earlier frame: that save also marks this tick as saved.
            current_saved = self._saved_this_tick
            if self._burst_previous is not None and not self._burst_previous_saved:
                self._save_training_frame(self._burst_previous[1])
            self._burst_previous = None
            if not current_saved:
                self._save_training_frame(frame)
            self._burst_after = max(0, training_capture.BURST_FRAMES - 2)
            self._burst_after_job = self.print_job_id
        except Exception:
            log.warning(f"[{self.printer_name}] Detection burst frames not saved", exc_info=True)

    def _save_training_frame(self, frame: np.ndarray) -> bool:
        """Save one training frame if every rule allows it; True when a frame was written.

        Order matters: controlled work and excluded models are refused before the storage
        guard runs, so they never raise a storage alert. The crop applies here only.
        """
        if not self._training_capture_allowed() or self._training_job_excluded():
            return False
        config = self._training_settings()
        if config.get('crop_invalid'):
            log.warning(f"[{self.printer_name}] Training crop is unusable; training frame not saved")
            return False
        minimum = config.get('min_free_bytes')
        if type(minimum) is not int:
            minimum = training_capture.min_free_bytes(None)
        if not training_capture.STORAGE_GUARD.has_room(frame_storage.VISION_FRAMES_DIR, minimum, self._storage_alert):
            return False
        try:
            path = frame_storage.save_training_frame(
                self.printer_id, training_capture.crop_frame(frame, config.get('crop')))
        except Exception as exc:
            log.error(f"[{self.printer_name}] Training frame not saved: {exc}")
            return False
        self._insert_training_sample(path)
        self._sampler.mark()
        self._saved_this_tick = True
        return True

    def _training_job_excluded(self) -> bool:
        """True when the running job's model has the category 'no-training'.

        Fails closed when the check itself fails. A print with no ODIN job or model (started at
        the printer, or not yet linked in the first moments of a print) has no category to read
        and is collected; controlled work is refused separately and earlier, and that check does
        fail closed for an unidentified print once controlled work exists.

        models.category is used because jobs.required_tags is a scheduling constraint (a job
        needing a tag only runs on printers that carry it), so tagging a job would block it.
        Checked once a minute per print.
        """
        if self.print_job_id is None:
            return False
        now = time.monotonic()
        cached = getattr(self, '_training_excluded', None)
        if cached and cached[0] == self.print_job_id and now - cached[2] < 60:
            return cached[1]
        try:
            with engine.connect() as conn:
                category = conn.execute(text(
                    "SELECT m.category FROM print_jobs pj JOIN jobs j ON j.id = pj.scheduled_job_id "
                    "LEFT JOIN models m ON m.id = j.model_id WHERE pj.id = :pid"),
                    {"pid": self.print_job_id}).scalar()
            excluded = training_capture.excluded_category(category)
        except Exception:
            log.warning(f"[{self.printer_name}] Training exclusion check failed; training frame not saved")
            excluded = True
        self._training_excluded = (self.print_job_id, excluded, now)
        return excluded

    def _storage_alert(self, free: int, minimum: int) -> None:
        """One in-app alert for platform admins when training capture stops for lack of disk space."""
        from sqlalchemy.orm import Session
        from core.base import AlertType, AlertSeverity
        from modules.notifications.models import Alert
        gib = 1024 ** 3
        message = (f"Vigil stopped saving training frames: the frames volume has {free / gib:.1f} GB free, "
                   f"below the {minimum / gib:.1f} GB minimum (Vigil AI settings: training_min_free_gb). "
                   "Saving resumes by itself once space is freed.")
        log.warning(message)
        with engine.begin() as conn, Session(bind=conn) as db:
            admin_ids = db.execute(text(
                "SELECT id FROM users WHERE role = 'admin' AND group_id IS NULL AND is_active IS TRUE")).scalars().all()
            for user_id in admin_ids:
                db.add(Alert(user_id=user_id, alert_type=AlertType.PRINTER_ERROR, severity=AlertSeverity.WARNING,
                             title='Vigil training capture paused: low disk space', message=message,
                             metadata_json={'kind': 'vision_training_storage',
                                            'free_bytes': free, 'minimum_bytes': minimum}))
            db.flush()

    def _insert_training_sample(self, frame_path: str) -> None:
        """Index a collected frame for annotation; not a detection (status 'training')."""
        try:
            with engine.begin() as conn:
                conn.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragments: the TRAINING_SAMPLE module constant and sql.now() dialect helper; values are bound
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
        minimum = self.settings.get('auto_pause_min_frames', AUTO_PAUSE_MIN_FRAMES)
        if type(minimum) is not int or minimum < AUTO_PAUSE_MIN_FRAMES:
            minimum = AUTO_PAUSE_MIN_FRAMES
        if (not is_printing_state(self.gcode_state) or self.print_job_id is None
                or self._analyzed_frames < minimum or self._first_analysis_at is None
                or time.monotonic() - self._first_analysis_at < AUTO_PAUSE_GRACE_SECONDS):
            return
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT api_type, api_host, api_key FROM printers WHERE id = :pid"),
                    {"pid": self.printer_id}
                ).mappings().fetchone()
            if not row or not self._live_printing():
                return

            api_type = row['api_type']
            api_host = row['api_host']
            api_key = row['api_key']
            success = False

            if api_type == 'moonraker':
                from modules.printers.adapters.moonraker import MoonrakerPrinter
                adapter = MoonrakerPrinter(api_host)
                if self._live_printing():
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
                    if self._live_printing():
                        success = run_command(config, "pause_print")
                else:
                    from modules.printers.adapters.bambu import BambuPrinter
                    adapter = BambuPrinter(api_host, serial, access_code)
                    if adapter.connect():
                        try:
                            if self._live_printing():
                                success = adapter.pause_print()
                        finally:
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
                if jrow and jrow[0] and self._live_printing():
                    success = adapter.pause_print(int(jrow[0]))

            elif api_type == 'elegoo':
                from modules.printers.adapters.elegoo import ElegooPrinter
                adapter = ElegooPrinter(api_host)
                if self._live_printing():
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

    def _live_printing(self):
        from core.monitor_observations import live_state
        with engine.connect() as conn:
            state = live_state(conn, self.printer_id, PROCESS_STARTED_AT)
            row = conn.execute(text(
                "SELECT p.gcode_state, pj.id FROM printers p JOIN print_jobs pj "
                "ON pj.printer_id=p.id AND pj.status='running' "
                "WHERE p.id=:pid ORDER BY pj.id DESC LIMIT 1"),
                {'pid': self.printer_id}).first()
        return bool(row and row[1] == self.print_job_id
                    and is_printing_state(row[0]) and is_printing_state(state))
