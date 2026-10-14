"""Explicit immutable model output contracts; invalid observations are unavailable."""
from dataclasses import dataclass
import json
import numpy as np


class InvalidModelOutput(ValueError):
    """The model did not produce a trustworthy observation."""


def _validated_confidences(values):
    # ORT's fused float32 sigmoid can round one ULP beyond a probability
    # boundary. Other dtypes do not inherit this float32 allowance.
    tolerance = np.finfo(np.float32).eps if values.dtype == np.dtype('float32') else 0
    if not np.isfinite(values).all() or not (values >= -tolerance).all() or not (values <= 1 + tolerance).all():
        raise InvalidModelOutput('Invalid confidence values')
    return np.clip(values, 0, 1)


@dataclass(frozen=True)
class OutputContract:
    format: str
    layout: str | None
    class_count: int
    positive_class_ids: tuple[int, ...]

    @classmethod
    def from_metadata(cls, metadata):
        try:
            if isinstance(metadata, str) and len(metadata) > 16384:
                raise ValueError
            root = json.loads(metadata) if isinstance(metadata, str) else metadata
            value = root['inference_contract']
            if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
                raise ValueError
            count = value['class_count']
            positives = value['positive_class_ids']
            kind, layout = value['format'], value.get('layout')
            if type(count) is not int or not 1 <= count <= 1000:
                raise ValueError
            if not isinstance(positives, list) or not positives or any(
                type(i) is not int or not 0 <= i < count for i in positives
            ) or len(set(positives)) != len(positives):
                raise ValueError
            if kind == 'obico_boxes_confs':
                if count != 1 or positives != [0] or layout is not None:
                    raise ValueError
            elif kind == 'yolov8_class_scores':
                if layout not in ('channels_first', 'rows_first'):
                    raise ValueError
            else:
                raise ValueError
            return cls(kind, layout, count, tuple(positives))
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise InvalidModelOutput('Invalid inference contract') from None

    def yolo_rows(self, outputs):
        if self.format != 'yolov8_class_scores' or len(outputs) != 1:
            raise InvalidModelOutput('Unexpected output format')
        value = np.asarray(outputs[0])
        features = 4 + self.class_count
        axis = 1 if self.layout == 'channels_first' else 2
        if value.dtype.kind not in 'fiu':
            raise InvalidModelOutput('Unsupported output dtype')
        if value.ndim != 3 or value.shape[0] != 1 or value.shape[axis] != features:
            raise InvalidModelOutput('Unexpected output shape')
        rows = value[0].T if axis == 1 else value[0]
        try:
            valid = np.isfinite(rows).all() and (rows[:, 2:4] >= 0).all()
            scores = rows[:, 4:]
            scores = _validated_confidences(scores)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise InvalidModelOutput('Invalid output values')
        rows = rows.copy()
        rows[:, 4:] = scores
        return rows

    def obico_arrays(self, outputs, names):
        if self.format != 'obico_boxes_confs' or len(outputs) != 2 or sorted(names) != ['boxes', 'confs']:
            raise InvalidModelOutput('Unexpected output format')
        boxes = np.asarray(outputs[names.index('boxes')])
        confs = np.asarray(outputs[names.index('confs')])
        if boxes.dtype.kind not in 'fiu' or confs.dtype.kind not in 'fiu':
            raise InvalidModelOutput('Unsupported output dtype')
        if boxes.ndim != 4 or boxes.shape[0] != 1 or boxes.shape[2:] != (1, 4):
            raise InvalidModelOutput('Unexpected boxes shape')
        if confs.shape != (1, boxes.shape[1], 1):
            raise InvalidModelOutput('Unexpected confidences shape')
        boxes, confs = boxes.reshape(-1, 4), confs.reshape(-1)
        try:
            valid = (np.isfinite(boxes).all() and np.isfinite(confs).all()
                     and (boxes[:, 2:] >= boxes[:, :2]).all()
                     )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise InvalidModelOutput('Invalid output values')
        return boxes, _validated_confidences(confs)
