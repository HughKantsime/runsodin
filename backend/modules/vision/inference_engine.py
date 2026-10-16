"""
Vision Inference Engine.

Loads and caches ONNX models, runs inference for print failure detection.
Extracted from VisionMonitorDaemon to isolate model lifecycle management.
"""

import os
import logging
import time
import threading
import hashlib
import uuid
from modules.vision.runtime_status import write_status, registry_fingerprint
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from modules.vision.output_contract import OutputContract, InvalidModelOutput
from typing import Dict, List, Optional

import numpy as np
from sqlalchemy import text

try:
    import onnxruntime as ort
except ImportError:
    ort = None

try:
    import cv2
except ImportError:
    cv2 = None

# Resolved at use time: never a stale engine bound at import.
from core.db import live_engine as engine

log = logging.getLogger('vision_monitor')

VISION_MODELS_DIR = '/data/vision_models'


@dataclass(frozen=True)
class ModelSnapshot:
    generation: int
    state: str
    session: object = None
    input_size: int = 640
    contract: object = None
    fingerprint: str = ''
    model_id: int = 0
    observed_ok: bool = False


@dataclass(frozen=True)
class InferenceObservation:
    generation: int
    available: bool
    detections: tuple = ()



class VisionInferenceEngine:
    """Loads and caches ONNX models, runs inference."""

    def __init__(self):
        self._lock = threading.RLock()
        self._reload_lock = threading.Lock()
        self._snapshots = MappingProxyType({})
        self._generation = 0
        self._registry_available = False
        self._last_reload = 0
        self._incarnation = uuid.uuid4().hex

    def reload_models(self):
        """Publish one immutable registry generation; failures remain configured."""
        with self._reload_lock:
            try:
                with engine.connect() as conn:
                    rows = conn.execute(text(
                        "SELECT id,name,detection_type,filename,input_size,metadata_json "
                        "FROM vision_models WHERE is_active IS TRUE"
                    )).mappings().fetchall()
            except Exception:
                with self._lock:
                    self._generation += 1
                    self._registry_available = False
                    self._snapshots = MappingProxyType({})
                log.error("Vision model registry unavailable")
                return
            grouped = {}
            for row in rows:
                grouped.setdefault(row['detection_type'], []).append(row)
            with self._lock:
                generation = self._generation + 1
                previous = self._snapshots
            snapshots = {}
            for kind, candidates in grouped.items():
                unavailable = ModelSnapshot(generation, 'unavailable')
                snapshots[kind] = unavailable
                if len(candidates) != 1 or ort is None:
                    continue
                row = candidates[0]
                try:
                    path = os.path.join(VISION_MODELS_DIR, row['filename'])
                    size = row['input_size'] or 640
                    if type(size) is not int or not 16 <= size <= 4096:
                        raise InvalidModelOutput('Invalid input size')
                    with open(path, 'rb') as source:
                        model_bytes = source.read(500 * 1024 * 1024 + 1)
                    if len(model_bytes) > 500 * 1024 * 1024:
                        raise InvalidModelOutput('Model exceeds size limit')
                    digest = hashlib.sha256(model_bytes)
                    fingerprint = registry_fingerprint(row['id'],row['filename'],size,row['metadata_json'],digest.hexdigest())
                    snapshots[kind] = ModelSnapshot(generation,'unavailable',fingerprint=fingerprint,model_id=row['id'])
                    old = previous.get(kind)
                    if old and old.state == 'ready' and old.fingerprint == fingerprint:
                        snapshots[kind] = ModelSnapshot(generation,'ready',old.session,
                            old.input_size,old.contract,fingerprint,row['id'])
                        continue
                    options = ort.SessionOptions()
                    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                    options.intra_op_num_threads = 2
                    session = ort.InferenceSession(model_bytes, options, providers=['CPUExecutionProvider'])
                    inputs = session.get_inputs()
                    if len(inputs) != 1:
                        raise InvalidModelOutput('One image input required')
                    image = inputs[0]
                    if (image.type != 'tensor(float)' or len(image.shape) != 4
                        or image.shape[0] != 1 or image.shape[1] != 3
                        or image.shape[2:] != [size,size]):
                        raise InvalidModelOutput('Unsupported image input contract')
                    metadata = row['metadata_json']
                    if metadata:
                        contract = OutputContract.from_metadata(metadata)
                    else:
                        outputs = {o.name: o.shape for o in session.get_outputs()}
                        boxes, confs = outputs.get('boxes'), outputs.get('confs')
                        if (len(outputs) != 2 or boxes is None or confs is None
                            or len(boxes) != 4 or boxes[0] != 1 or boxes[2:] != [1,4]
                            or len(confs) != 3 or confs[0] != 1 or confs[2] != 1
                            or boxes[1] != confs[1]):
                            raise InvalidModelOutput('Explicit output contract required')
                        contract = OutputContract('obico_boxes_confs',None,1,(0,))
                    snapshots[kind] = ModelSnapshot(generation,'ready',session,size,contract,fingerprint,row['id'])
                except Exception:
                    log.warning("Configured vision model unavailable for %s",kind)
            with self._lock:
                self._generation = generation
                self._registry_available = True
                self._snapshots = MappingProxyType(snapshots)
                self._last_reload = time.time()

    def publish_runtime_status(self):
        with self._lock:
            models = {kind:{'id':snapshot.model_id,'state':('loaded' if snapshot.state == 'ready' and not snapshot.observed_ok else snapshot.state),
                           'fingerprint':snapshot.fingerprint}
                      for kind,snapshot in self._snapshots.items()}
            generation = self._generation
            try:
                write_status(os.path.join(VISION_MODELS_DIR,'.runtime'),self._incarnation,generation,models)
            except (OSError,ValueError):
                log.warning('Vision runtime status unavailable')

    def model_state(self, detection_type):
        with self._lock:
            if not self._registry_available:
                return 'unavailable'
            snapshot = self._snapshots.get(detection_type)
            return snapshot.state if snapshot else 'not_configured'

    def configuration_state(self, detection_type):
        with self._lock:
            return self._generation, self.model_state(detection_type)

    def heuristic_current(self, detection_type, generation):
        with self._lock:
            return self._generation == generation and self.model_state(detection_type) == 'not_configured'

    def has_model(self, detection_type):
        return self.model_state(detection_type) == 'ready'

    def get_input_size(self, detection_type):
        with self._lock:
            snapshot = self._snapshots.get(detection_type)
            return snapshot.input_size if snapshot else 640

    def observation_current(self, detection_type, observation):
        with self._lock:
            snapshot = self._snapshots.get(detection_type)
            return bool(observation.available and self._registry_available and snapshot
                and snapshot.state == 'ready' and snapshot.generation == observation.generation)

    @contextmanager
    def decision_guard(self, detection_type, observation=None, heuristic_generation=None):
        # Reload publication and decision admission share the same lock.
        # A checked observation cannot lose its authority before admission.
        with self._lock:
            valid = (self.observation_current(detection_type, observation)
                     if observation is not None else
                     self.heuristic_current(detection_type, heuristic_generation))
            yield valid

    def infer_observation(self, detection_type, frame):
        with self._lock:
            snapshot = self._snapshots.get(detection_type)
            generation = self._generation
            available = self._registry_available
        if not available or snapshot is None or snapshot.state != 'ready':
            return InferenceObservation(generation,False)
        try:
            session = snapshot.session
            blob = self._preprocess(frame,snapshot.input_size)
            outputs = session.run(None,{session.get_inputs()[0].name:blob})
            names = [o.name for o in session.get_outputs()]
            if snapshot.contract.format == 'obico_boxes_confs':
                snapshot.contract.obico_arrays(outputs,names)
                detections = self._postprocess_obico(outputs,names,frame.shape,snapshot.input_size)
            else:
                detections = self._postprocess_yolov8(outputs,frame.shape,snapshot.input_size,snapshot.contract)
            observation = InferenceObservation(snapshot.generation,True,tuple(detections))
            with self._lock:
                if not self.observation_current(detection_type,observation):
                    return InferenceObservation(snapshot.generation,False)
                current = self._snapshots[detection_type]
                updated = dict(self._snapshots)
                updated[detection_type] = ModelSnapshot(current.generation,'ready',current.session,
                    current.input_size,current.contract,current.fingerprint,current.model_id,True)
                self._snapshots = MappingProxyType(updated)
            return observation
        except Exception:
            with self._lock:
                current = self._snapshots.get(detection_type)
                if current and current.generation == snapshot.generation and current.session is snapshot.session:
                    updated = dict(self._snapshots)
                    updated[detection_type] = ModelSnapshot(snapshot.generation,'unavailable',
                        snapshot.session,snapshot.input_size,snapshot.contract,snapshot.fingerprint,snapshot.model_id)
                    self._snapshots = MappingProxyType(updated)
            log.warning("Vision inference observation unavailable for %s",detection_type)
            return InferenceObservation(snapshot.generation,False)

    def infer(self, detection_type, frame):
        observation = self.infer_observation(detection_type,frame)
        if not observation.available:
            raise InvalidModelOutput('Inference unavailable')
        return list(observation.detections)

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
        boxes: (1, N, 1, 4), relative x1/y1/x2/y2; may extend outside image
        confs: (1, N, 1) — objectness confidence
        """
        boxes, confs = OutputContract('obico_boxes_confs',None,1,(0,)).obico_arrays(outputs,output_names)

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
        self, outputs: list, orig_shape: tuple, input_size: int,
        contract: OutputContract = None,
    ) -> List[dict]:
        """Parse only a declared YOLO layout with exactly 4+class_count features."""
        if contract is None:
            raise InvalidModelOutput('Explicit output contract required')
        preds = contract.yolo_rows(outputs)

        h_orig, w_orig = orig_shape[:2]
        scale_x = w_orig / input_size
        scale_y = h_orig / input_size

        detections = []
        for pred in preds:
            cx, cy, w, h = pred[:4]
            class_scores = pred[4:]
            class_id = int(np.argmax(class_scores))
            confidence = float(class_scores[class_id])
            if class_id not in contract.positive_class_ids:
                continue

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
