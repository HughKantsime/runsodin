#!/usr/bin/env python3
"""
Vision Monitor Daemon — Local AI detection for 3D print failures.

Captures frames from printer cameras during active prints, runs ONNX inference
for three failure types (spaghetti, first layer, detachment), and integrates
with the existing alert/event pipeline.

Architecture:
  - One thread per actively-printing printer with camera
  - Frame capture: GET http://127.0.0.1:1984/api/frame.jpeg?src=printer_{id}
  - Preprocessing: cv2.imdecode → resize 640x640 → normalize → NCHW
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
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Any, Tuple

import cv2
import numpy as np

try:
    import onnxruntime as ort
except ImportError:
    ort = None

import printer_events
try:
    from ws_hub import push_event as ws_push
except ImportError:
    def ws_push(*a, **kw): pass

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('vision_monitor')

DB_PATH = os.environ.get('DATABASE_PATH', '/data/odin.db')
GO2RTC_BASE = 'http://127.0.0.1:1984'
VISION_FRAMES_DIR = '/data/vision_frames'
VISION_MODELS_DIR = '/data/vision_models'

SCAN_INTERVAL = 15  # seconds between main loop scans
MODEL_RELOAD_INTERVAL = 60  # seconds between model reload checks
ALERT_COOLDOWN = 60  # minimum seconds between same-type alerts per printer
FRAME_CLEANUP_INTERVAL = 3600  # hourly frame cleanup

DETECTION_TYPES = ['spaghetti', 'first_layer', 'detachment']


class VisionInferenceEngine:
    """Loads and caches ONNX models, runs inference."""

    def __init__(self):
        self._sessions: Dict[str, ort.InferenceSession] = {}
        self._model_info: Dict[str, dict] = {}
        self._last_reload = 0

    def reload_models(self):
        """Load active ONNX models from DB registry."""
        if ort is None:
            log.warning("onnxruntime not installed, inference disabled")
            return

        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, detection_type, filename, input_size "
                "FROM vision_models WHERE is_active = 1"
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log.error(f"Failed to load model registry: {e}")
            return

        loaded_types = set()
        for row in rows:
            dt = row['detection_type']
            model_path = os.path.join(VISION_MODELS_DIR, row['filename'])

            if not os.path.isfile(model_path):
                log.warning(f"Model file not found: {model_path}")
                continue

            # Only reload if not already loaded or file changed
            if dt in self._sessions:
                if self._model_info.get(dt, {}).get('id') == row['id']:
                    loaded_types.add(dt)
                    continue

            try:
                sess_opts = ort.SessionOptions()
                sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                sess_opts.intra_op_num_threads = 2
                session = ort.InferenceSession(model_path, sess_opts, providers=['CPUExecutionProvider'])
                self._sessions[dt] = session
                self._model_info[dt] = {
                    'id': row['id'],
                    'name': row['name'],
                    'input_size': row['input_size'] or 640,
                }
                loaded_types.add(dt)
                log.info(f"Loaded model for {dt}: {row['name']} ({row['filename']})")
            except Exception as e:
                log.error(f"Failed to load model {row['filename']}: {e}")

        # Remove sessions for types no longer active
        for dt in list(self._sessions.keys()):
            if dt not in loaded_types:
                del self._sessions[dt]
                del self._model_info[dt]

        self._last_reload = time.time()

    def has_model(self, detection_type: str) -> bool:
        return detection_type in self._sessions

    def get_input_size(self, detection_type: str) -> int:
        return self._model_info.get(detection_type, {}).get('input_size', 640)

    def infer(self, detection_type: str, frame: np.ndarray) -> List[dict]:
        """
        Run inference on a preprocessed frame.
        Returns list of detections: [{confidence, bbox: [x1,y1,x2,y2], class_id}]
        """
        session = self._sessions.get(detection_type)
        if session is None:
            return []

        input_size = self.get_input_size(detection_type)
        blob = self._preprocess(frame, input_size)

        input_name = session.get_inputs()[0].name
        try:
            outputs = session.run(None, {input_name: blob})
        except Exception as e:
            log.error(f"Inference failed for {detection_type}: {e}")
            return []

        # Detect output format and dispatch to appropriate postprocessor
        output_names = [o.name for o in session.get_outputs()]
        if len(outputs) == 2 and 'boxes' in output_names and 'confs' in output_names:
            # Obico/Darknet format: separate boxes (1,N,1,4) + confs (1,N,1)
            return self._postprocess_obico(outputs, output_names, frame.shape, input_size)
        else:
            # YOLOv8 format: single tensor (1, 5+nc, N)
            return self._postprocess_yolov8(outputs, frame.shape, input_size)

    def _preprocess(self, frame: np.ndarray, input_size: int) -> np.ndarray:
        """Resize, normalize, transpose to NCHW float32 batch."""
        img = cv2.resize(frame, (input_size, input_size))
        img = img.astype(np.float32) / 255.0
        img = np.transpose(img, (2, 0, 1))  # HWC → CHW
        return np.expand_dims(img, axis=0)   # add batch dim

    def _postprocess_obico(
        self, outputs: list, output_names: list,
        orig_shape: tuple, input_size: int,
    ) -> List[dict]:
        """
        Parse Obico/Darknet YOLO output.
        boxes: (1, N, 1, 4) — x1, y1, x2, y2 normalized [0..1]
        confs: (1, N, 1) — objectness confidence
        """
        boxes_idx = output_names.index('boxes')
        confs_idx = output_names.index('confs')
        boxes = outputs[boxes_idx].squeeze()  # (N, 4)
        confs = outputs[confs_idx].squeeze()   # (N,)

        h_orig, w_orig = orig_shape[:2]

        detections = []
        for i in range(len(confs)):
            conf = float(confs[i])
            if conf < 0.3:
                continue

            # Boxes are normalized [0..1], convert to pixel coords
            x1 = float(boxes[i, 0]) * w_orig
            y1 = float(boxes[i, 1]) * h_orig
            x2 = float(boxes[i, 2]) * w_orig
            y2 = float(boxes[i, 3]) * h_orig

            detections.append({
                'confidence': conf,
                'bbox': [x1, y1, x2, y2],
                'class_id': 0,  # single-class model
            })

        if detections:
            detections = self._nms(detections, iou_threshold=0.45)

        return detections

    def _postprocess_yolov8(
        self, outputs: list, orig_shape: tuple, input_size: int
    ) -> List[dict]:
        """Parse YOLOv8 output: (1, 5+nc, num_boxes) → list of detections."""
        output = outputs[0]  # shape: (1, 5+nc, N) or (1, N, 5+nc)

        # Handle both transposed and non-transposed output shapes
        if output.ndim == 3:
            if output.shape[1] < output.shape[2]:
                # (1, 5+nc, N) → transpose to (1, N, 5+nc)
                output = np.transpose(output, (0, 2, 1))
            preds = output[0]  # (N, 5+nc)
        else:
            return []

        h_orig, w_orig = orig_shape[:2]
        scale_x = w_orig / input_size
        scale_y = h_orig / input_size

        detections = []
        for pred in preds:
            cx, cy, w, h = pred[:4]
            class_scores = pred[4:]
            class_id = int(np.argmax(class_scores))
            confidence = float(class_scores[class_id])

            if confidence < 0.3:  # pre-filter low confidence
                continue

            x1 = (cx - w / 2) * scale_x
            y1 = (cy - h / 2) * scale_y
            x2 = (cx + w / 2) * scale_x
            y2 = (cy + h / 2) * scale_y

            detections.append({
                'confidence': confidence,
                'bbox': [float(x1), float(y1), float(x2), float(y2)],
                'class_id': class_id,
            })

        # NMS
        if detections:
            detections = self._nms(detections, iou_threshold=0.45)

        return detections

    def _nms(self, detections: List[dict], iou_threshold: float) -> List[dict]:
        """Non-maximum suppression."""
        if not detections:
            return []

        boxes = np.array([d['bbox'] for d in detections])
        scores = np.array([d['confidence'] for d in detections])
        order = scores.argsort()[::-1]

        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)

            if order.size == 1:
                break

            xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
            yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
            xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
            yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])

            w = np.maximum(0, xx2 - xx1)
            h = np.maximum(0, yy2 - yy1)
            inter = w * h

            area_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
            area_rest = (boxes[order[1:], 2] - boxes[order[1:], 0]) * (boxes[order[1:], 3] - boxes[order[1:], 1])
            iou = inter / (area_i + area_rest - inter + 1e-6)

            inds = np.where(iou <= iou_threshold)[0]
            order = order[inds + 1]

        return [detections[i] for i in keep]


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
    ):
        super().__init__(daemon=True)
        self.printer_id = printer_id
        self.printer_name = printer_name
        self.engine = engine
        self.settings = settings
        self.current_layer = current_layer
        self.print_job_id = print_job_id
        self._running = True

        # Confirmation buffers: track consecutive detections
        self._history: Dict[str, List[bool]] = {
            'spaghetti': [],
            'first_layer': [],
            'detachment': [],
        }
        # Alert cooldown tracking: detection_type → last alert timestamp
        self._last_alert: Dict[str, float] = {}

    def stop(self):
        self._running = False

    def update_layer(self, layer: Optional[int], job_id: Optional[int]):
        self.current_layer = layer
        self.print_job_id = job_id

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
            self._save_training_frame(frame)

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
        self._last_alert[detection_type] = time.time()
        confidence = detection['confidence']
        bbox = detection['bbox']

        log.warning(
            f"[{self.printer_name}] {detection_type} detected "
            f"(confidence={confidence:.2f})"
        )

        # Save frame
        frame_path = self._save_detection_frame(frame, detection_type)

        # Insert detection record
        detection_id = self._insert_detection(
            detection_type, confidence, frame_path, bbox
        )

        # Map detection type to alert type
        alert_type_map = {
            'spaghetti': 'spaghetti_detected',
            'first_layer': 'first_layer_issue',
            'detachment': 'detachment_detected',
        }
        alert_type = alert_type_map[detection_type]

        severity_map = {
            'spaghetti': 'critical',
            'first_layer': 'warning',
            'detachment': 'critical',
        }

        title_map = {
            'spaghetti': f"Spaghetti Detected: {self.printer_name}",
            'first_layer': f"First Layer Issue: {self.printer_name}",
            'detachment': f"Print Detachment: {self.printer_name}",
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

    def _save_detection_frame(self, frame: np.ndarray, detection_type: str) -> str:
        """Save frame JPEG and return relative path."""
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        rel_dir = f"{self.printer_id}"
        abs_dir = os.path.join(VISION_FRAMES_DIR, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        filename = f"{ts}_{detection_type}.jpg"
        filepath = os.path.join(abs_dir, filename)
        cv2.imwrite(filepath, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return f"{rel_dir}/{filename}"

    def _save_training_frame(self, frame: np.ndarray):
        """Save frame for training data collection."""
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')
        abs_dir = os.path.join(VISION_FRAMES_DIR, str(self.printer_id), 'training')
        os.makedirs(abs_dir, exist_ok=True)
        filepath = os.path.join(abs_dir, f"{ts}.jpg")
        cv2.imwrite(filepath, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])

    def _insert_detection(
        self, detection_type: str, confidence: float,
        frame_path: str, bbox: list
    ) -> Optional[int]:
        """Insert detection record into vision_detections table."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO vision_detections
                    (printer_id, print_job_id, detection_type, confidence,
                     status, frame_path, bbox_json, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, datetime('now'))""",
                (
                    self.printer_id,
                    self.print_job_id,
                    detection_type,
                    confidence,
                    frame_path,
                    json.dumps(bbox),
                )
            )
            detection_id = cur.lastrowid
            conn.commit()
            conn.close()
            return detection_id
        except Exception as e:
            log.error(f"Failed to insert detection: {e}")
            return None

    def _auto_pause(self):
        """Pause the printer using the appropriate adapter."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT api_type, api_host, api_key FROM printers WHERE id = ?",
                (self.printer_id,)
            )
            row = cur.fetchone()
            conn.close()
            if not row:
                return

            api_type = row['api_type']
            api_host = row['api_host']
            api_key = row['api_key']
            success = False

            if api_type == 'moonraker':
                from moonraker_adapter import MoonrakerPrinter
                adapter = MoonrakerPrinter(api_host)
                success = adapter.pause_print()

            elif api_type == 'bambu':
                from bambu_adapter import BambuPrinter
                from crypto import decrypt
                creds = decrypt(api_key)
                serial, access_code = creds.split('|', 1)
                adapter = BambuPrinter(api_host, serial, access_code)
                if adapter.connect():
                    success = adapter.pause_print()
                    adapter.disconnect()

            elif api_type == 'prusalink':
                from prusalink_adapter import PrusaLinkPrinter
                adapter = PrusaLinkPrinter(api_host, api_key=api_key or '')
                # PrusaLink pause requires job_id; try current running job
                conn2 = sqlite3.connect(DB_PATH)
                cur2 = conn2.cursor()
                cur2.execute(
                    "SELECT job_id FROM print_jobs WHERE printer_id = ? AND status = 'running' "
                    "ORDER BY id DESC LIMIT 1",
                    (self.printer_id,)
                )
                jrow = cur2.fetchone()
                conn2.close()
                if jrow and jrow[0]:
                    success = adapter.pause_print(int(jrow[0]))

            elif api_type == 'elegoo':
                from elegoo_adapter import ElegooPrinter
                adapter = ElegooPrinter(api_host)
                success = adapter.pause_print()

            if success:
                log.info(f"[{self.printer_name}] Auto-paused printer")
                # Update gcode_state in DB
                conn3 = sqlite3.connect(DB_PATH)
                conn3.execute(
                    "UPDATE printers SET gcode_state = 'PAUSED' WHERE id = ?",
                    (self.printer_id,)
                )
                # Update detection status
                conn3.execute(
                    """UPDATE vision_detections SET status = 'auto_paused'
                    WHERE printer_id = ? AND status = 'pending'
                    ORDER BY id DESC LIMIT 1""",
                    (self.printer_id,)
                )
                conn3.commit()
                conn3.close()
            else:
                log.error(f"[{self.printer_name}] Auto-pause failed")

        except Exception as e:
            log.error(f"[{self.printer_name}] Auto-pause error: {e}")


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
                    self._cleanup_old_frames()
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
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # Find printers: active, camera enabled, camera URL set, currently printing
            cur.execute("""
                SELECT p.id, p.name, p.nickname, p.gcode_state,
                       pj.id as print_job_id, pj.current_layer,
                       vs.enabled, vs.spaghetti_enabled, vs.spaghetti_threshold,
                       vs.first_layer_enabled, vs.first_layer_threshold,
                       vs.detachment_enabled, vs.detachment_threshold,
                       vs.auto_pause, vs.capture_interval_sec,
                       vs.collect_training_data
                FROM printers p
                LEFT JOIN vision_settings vs ON vs.printer_id = p.id
                LEFT JOIN print_jobs pj ON pj.printer_id = p.id
                    AND pj.status = 'running'
                WHERE p.is_active = 1
                  AND p.camera_enabled = 1
                  AND p.camera_url IS NOT NULL
                  AND p.gcode_state = 'RUNNING'
            """)
            rows = cur.fetchall()
            conn.close()
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
                'auto_pause': row['auto_pause'] or 0,
                'capture_interval_sec': row['capture_interval_sec'] or 10,
                'collect_training_data': row['collect_training_data'] or 0,
            }

            if pid in self._threads:
                # Update layer info on existing thread
                self._threads[pid].update_layer(
                    row['current_layer'], row['print_job_id']
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

    def _cleanup_old_frames(self):
        """Delete detection frames older than retention period."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # Get retention days from global settings (default 30)
            cur.execute(
                "SELECT value FROM system_config WHERE key = 'vision_retention_days'"
            )
            row = cur.fetchone()
            retention_days = 30
            if row:
                try:
                    retention_days = int(json.loads(row['value']))
                except (ValueError, TypeError):
                    pass

            # Find old detections with frame paths
            cur.execute(
                """SELECT id, frame_path FROM vision_detections
                WHERE created_at < datetime('now', ? || ' days')
                  AND frame_path IS NOT NULL""",
                (f"-{retention_days}",)
            )
            old_rows = cur.fetchall()

            deleted = 0
            for old in old_rows:
                fpath = os.path.join(VISION_FRAMES_DIR, old['frame_path'])
                if os.path.isfile(fpath):
                    os.remove(fpath)
                    deleted += 1

            # Clear frame_path on old records (keep detection metadata)
            if old_rows:
                ids = [str(r['id']) for r in old_rows]
                cur.execute(
                    f"UPDATE vision_detections SET frame_path = NULL "
                    f"WHERE id IN ({','.join(ids)})"
                )
                conn.commit()

            conn.close()

            if deleted:
                log.info(f"Cleaned up {deleted} old vision frames (>{retention_days} days)")

            # Also clean up training frames older than retention
            self._cleanup_training_frames(retention_days)

        except Exception as e:
            log.error(f"Frame cleanup error: {e}")

    def _cleanup_training_frames(self, retention_days: int):
        """Remove old training data frames."""
        cutoff = time.time() - (retention_days * 86400)
        for root, dirs, files in os.walk(VISION_FRAMES_DIR):
            if 'training' not in root:
                continue
            for f in files:
                fpath = os.path.join(root, f)
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                except OSError:
                    pass


if __name__ == '__main__':
    daemon = VisionMonitorDaemon()
    daemon.run()
