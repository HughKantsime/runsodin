"""
Vision Inference Engine.

Loads and caches ONNX models, runs inference for print failure detection.
Extracted from VisionMonitorDaemon to isolate model lifecycle management.
"""

import os
import logging
import sqlite3
import time
from typing import Dict, List, Optional

import numpy as np

try:
    import onnxruntime as ort
except ImportError:
    ort = None

try:
    import cv2
except ImportError:
    cv2 = None

from core.db_utils import get_db

log = logging.getLogger('vision_monitor')

VISION_MODELS_DIR = '/data/vision_models'


class VisionInferenceEngine:
    """Loads and caches ONNX models, runs inference."""

    def __init__(self):
        self._sessions: Dict[str, 'ort.InferenceSession'] = {}
        self._model_info: Dict[str, dict] = {}
        self._last_reload = 0

    def reload_models(self):
        """Load active ONNX models from DB registry."""
        if ort is None:
            log.warning("onnxruntime not installed, inference disabled")
            return

        try:
            with get_db(row_factory=sqlite3.Row) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, name, detection_type, filename, input_size "
                    "FROM vision_models WHERE is_active = 1"
                )
                rows = cur.fetchall()
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
        img = np.transpose(img, (2, 0, 1))  # HWC -> CHW
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
        """Parse YOLOv8 output: (1, 5+nc, num_boxes) -> list of detections."""
        output = outputs[0]  # shape: (1, 5+nc, N) or (1, N, 5+nc)

        # Handle both transposed and non-transposed output shapes
        if output.ndim == 3:
            if output.shape[1] < output.shape[2]:
                # (1, 5+nc, N) -> transpose to (1, N, 5+nc)
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
