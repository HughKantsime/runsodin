"""Shared helpers for the Vigil evaluation tools (developer tooling, not product code).

Local files only: nothing here opens a connection, uploads, or copies frames. Heavy
packages (numpy, cv2, onnxruntime) are imported inside the functions that need them,
so the pure math can be used and tested without them.

Product code that is MIRRORED here (the product modules cannot be imported without
the backend environment: inference_engine.py imports core.db at module level):
  preprocess            <- VisionInferenceEngine._preprocess        inference_engine.py:249-255
  postprocess_obico     <- VisionInferenceEngine._postprocess_obico inference_engine.py:257-291
  postprocess_yolov8    <- VisionInferenceEngine._postprocess_yolov8 inference_engine.py:293-333
  nms                   <- VisionInferenceEngine._nms               inference_engine.py:335-368
  load_model            <- VisionInferenceEngine.reload_models      inference_engine.py:105-146
  detect                <- VisionInferenceEngine.infer_observation  inference_engine.py:213-221
  read_frame            <- PrinterVisionThread._capture_frame       detection_thread.py:311-312
Product code that is IMPORTED (loaded by file path, it only needs json and numpy):
  OutputContract        backend/modules/vision/output_contract.py
Tests pin the mirrored constants against the product source text.
"""
import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

# training_service.py:20 CLASSES (export class ids 0..3).
CLASS_NAMES = ['spaghetti', 'first_layer', 'detachment', 'build_plate_empty']
CONFIDENCE_FLOOR = 0.3      # inference_engine.py:273 and :315
NMS_IOU = 0.45              # inference_engine.py:289 and :331
MAX_MODEL_BYTES = 500 * 1024 * 1024   # inference_engine.py:111
MAX_FRAME_BYTES = 64 * 1024 * 1024
MANIFEST_VERSION = 1
IMAGE_SUFFIXES = ('.jpg', '.jpeg', '.png')
REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_FILE = REPO_ROOT / 'backend' / 'modules' / 'vision' / 'output_contract.py'


class ToolError(Exception):
    """A failure the tool reports on stderr (exit code 1)."""


def guard(function):
    """Run function() and turn failures into exit code 1."""
    try:
        return function()
    except ToolError as exc:
        print(f'error: {exc}', file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - last-resort report for a developer tool
        print(f'error: {type(exc).__name__}: {exc}', file=sys.stderr)
    return 1


# ---------------------------------------------------------------- geometry / metrics

def iou(a, b):
    """IoU of two [x1, y1, x2, y2] boxes. Invariant to per-axis scaling, so it can be
    computed in normalized coordinates."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def match_frame(dets, gts, iou_min):
    """Greedy matching for one frame. dets: [(class, conf, box)], gts: [(class, box)].

    A detection matches an unmatched label of the same class with the highest IoU at or
    above iou_min; detections are taken in descending confidence. Returns
    {class: [tp, fp, fn]}.
    """
    out = {}
    for cls in sorted({d[0] for d in dets} | {g[0] for g in gts}):
        ds = sorted((d for d in dets if d[0] == cls), key=lambda d: -d[1])
        boxes = [g[1] for g in gts if g[0] == cls]
        used = [False] * len(boxes)
        tp = fp = 0
        for det in ds:
            best, best_iou = -1, -1.0
            for k, box in enumerate(boxes):
                if used[k]:
                    continue
                value = iou(det[2], box)
                if value >= iou_min and value > best_iou:
                    best, best_iou = k, value
            if best >= 0:
                used[best] = True
                tp += 1
            else:
                fp += 1
        out[cls] = [tp, fp, used.count(False)]
    return out


def precision_recall(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return precision, recall


# ---------------------------------------------------------------- files

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def load_manifest(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ToolError(f'Cannot read manifest: {exc}') from None
    if (not isinstance(data, dict) or data.get('version') != MANIFEST_VERSION
            or not isinstance(data.get('frames'), list)):
        raise ToolError('Not a version 1 evaluation manifest')
    for frame in data['frames']:
        if (not isinstance(frame, dict) or not isinstance(frame.get('path'), str)
                or not isinstance(frame.get('labels'), list)):
            raise ToolError('Manifest frame entry is malformed')
        for label in frame['labels']:
            box = label.get('box') if isinstance(label, dict) else None
            if (not isinstance(label.get('class'), str) or not isinstance(box, list) or len(box) != 4
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                           for v in box)):
                raise ToolError('Manifest label is malformed')
    return data


def resolve_frame(root, relative):
    """A manifest path inside the frames root; anything that escapes it is refused."""
    base = Path(root).resolve()
    target = (base / relative).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ToolError(f'Frame path escapes the frames root: {relative}') from None
    return target


def read_frame(path):
    """Decode like PrinterVisionThread._capture_frame (detection_thread.py:311-312): BGR."""
    import cv2
    import numpy as np
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_FRAME_BYTES:
        raise ToolError(f'Frame missing or too large: {path.name}')
    frame = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ToolError(f'Frame cannot be decoded: {path.name}')
    return frame


# ---------------------------------------------------------------- model (mirrors the product)

def contract_module():
    if not CONTRACT_FILE.is_file():
        raise ToolError(f'Product output contract not found at {CONTRACT_FILE}; run from the repository')
    spec = importlib.util.spec_from_file_location('vigil_output_contract', CONTRACT_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules['vigil_output_contract'] = module
    spec.loader.exec_module(module)
    return module


class Model:
    def __init__(self, session, input_size, contract):
        self.session = session
        self.input_size = input_size
        self.contract = contract


def load_model(path, contract_path=None):
    """Mirror of VisionInferenceEngine.reload_models (inference_engine.py:105-146).

    The registry row is replaced by the file itself: input size comes from the model's
    input shape, and the optional contract file holds the same JSON as
    vision_models.metadata_json. Without it the legacy Obico boxes/confs layout is
    required, exactly as in the product.
    """
    import onnxruntime as ort
    oc = contract_module()
    with open(path, 'rb') as source:
        model_bytes = source.read(MAX_MODEL_BYTES + 1)
    if len(model_bytes) > MAX_MODEL_BYTES:
        raise ToolError('Model exceeds size limit')
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.intra_op_num_threads = 2
    session = ort.InferenceSession(model_bytes, options, providers=['CPUExecutionProvider'])
    inputs = session.get_inputs()
    if len(inputs) != 1:
        raise ToolError('One image input required')
    image = inputs[0]
    shape = image.shape
    if (image.type != 'tensor(float)' or len(shape) != 4 or shape[0] != 1 or shape[1] != 3
            or type(shape[2]) is not int or shape[2] != shape[3] or not 16 <= shape[2] <= 4096):
        raise ToolError('Unsupported image input contract')
    size = shape[2]
    try:
        if contract_path:
            metadata = Path(contract_path).read_text(encoding='utf-8')
            contract = oc.OutputContract.from_metadata(metadata)
        else:
            outputs = {o.name: o.shape for o in session.get_outputs()}
            boxes, confs = outputs.get('boxes'), outputs.get('confs')
            if (len(outputs) != 2 or boxes is None or confs is None
                    or len(boxes) != 4 or boxes[0] != 1 or boxes[2:] != [1, 4]
                    or len(confs) != 3 or confs[0] != 1 or confs[2] != 1
                    or boxes[1] != confs[1]):
                raise ToolError('Explicit output contract required: pass --contract with the '
                                'vision_models.metadata_json content')
            contract = oc.OutputContract('obico_boxes_confs', None, 1, (0,))
    except oc.InvalidModelOutput as exc:
        raise ToolError(str(exc)) from None
    return Model(session, size, contract)


def preprocess(frame, input_size):
    """Mirror of VisionInferenceEngine._preprocess (inference_engine.py:249-255)."""
    import cv2
    import numpy as np
    img = cv2.resize(frame, (input_size, input_size))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = img.astype(np.float32) / 255.0
    img = np.transpose(img, (2, 0, 1))
    return np.expand_dims(img, axis=0)


def detect(model, frame):
    """Mirror of VisionInferenceEngine.infer_observation (inference_engine.py:213-221).

    Returns the product's detections: dicts with confidence, bbox in frame pixels, class_id.
    """
    blob = preprocess(frame, model.input_size)
    session = model.session
    outputs = session.run(None, {session.get_inputs()[0].name: blob})
    names = [o.name for o in session.get_outputs()]
    if model.contract.format == 'obico_boxes_confs':
        model.contract.obico_arrays(outputs, names)
        return postprocess_obico(outputs, names, frame.shape)
    return postprocess_yolov8(outputs, frame.shape, model.input_size, model.contract)


def postprocess_obico(outputs, names, orig_shape):
    """Mirror of _postprocess_obico (inference_engine.py:257-291)."""
    oc = contract_module()
    boxes, confs = oc.OutputContract('obico_boxes_confs', None, 1, (0,)).obico_arrays(outputs, names)
    h_orig, w_orig = orig_shape[:2]
    detections = []
    for i in range(len(confs)):
        conf = float(confs[i])
        if conf < CONFIDENCE_FLOOR:
            continue
        detections.append({
            'confidence': conf,
            'bbox': [float(boxes[i, 0]) * w_orig, float(boxes[i, 1]) * h_orig,
                     float(boxes[i, 2]) * w_orig, float(boxes[i, 3]) * h_orig],
            'class_id': 0,
        })
    return nms(detections, NMS_IOU) if detections else detections


def postprocess_yolov8(outputs, orig_shape, input_size, contract):
    """Mirror of _postprocess_yolov8 (inference_engine.py:293-333)."""
    import numpy as np
    preds = contract.yolo_rows(outputs)
    h_orig, w_orig = orig_shape[:2]
    scale_x = w_orig / input_size
    scale_y = h_orig / input_size
    detections = []
    for pred in preds:
        cx, cy, bw, bh = pred[:4]
        class_scores = pred[4:]
        class_id = int(np.argmax(class_scores))
        confidence = float(class_scores[class_id])
        if class_id not in contract.positive_class_ids:
            continue
        if confidence < CONFIDENCE_FLOOR:
            continue
        detections.append({
            'confidence': confidence,
            'bbox': [float((cx - bw / 2) * scale_x), float((cy - bh / 2) * scale_y),
                     float((cx + bw / 2) * scale_x), float((cy + bh / 2) * scale_y)],
            'class_id': class_id,
        })
    return nms(detections, NMS_IOU) if detections else detections


def nms(detections, iou_threshold):
    """Mirror of VisionInferenceEngine._nms (inference_engine.py:335-368)."""
    import numpy as np
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
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        area_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        area_rest = (boxes[order[1:], 2] - boxes[order[1:], 0]) * (boxes[order[1:], 3] - boxes[order[1:], 1])
        overlap = inter / (area_i + area_rest - inter + 1e-6)
        order = order[np.where(overlap <= iou_threshold)[0] + 1]
    return [detections[i] for i in keep]


def normalize(det, frame_shape, names):
    """(class name, confidence, normalized box) for one product detection."""
    h, w = frame_shape[:2]
    x1, y1, x2, y2 = det['bbox']
    class_id = det['class_id']
    name = names[class_id] if 0 <= class_id < len(names) else f'class_{class_id}'
    return name, det['confidence'], [x1 / w, y1 / h, x2 / w, y2 / h]


def class_names(args_class_names, single_class_name, model):
    if args_class_names:
        return [n.strip() for n in args_class_names.split(',') if n.strip()]
    return [single_class_name] if model.contract.format == 'obico_boxes_confs' else list(CLASS_NAMES)
