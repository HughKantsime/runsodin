"""Register the bundled Vigil model after schema bootstrap, before services start."""

import json
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from core.models import SystemConfig
from modules.vision.models import VisionModel

VISION_MODELS_DIR = Path('/data/vision_models')
DEFAULT_FILENAME = 'obico_spaghetti.onnx'
DEFAULT_NAME = 'Obico Spaghetti Detector'
SEED_MARKER_KEY = 'vision_default_model_seeded'


def register_default_model(engine):
    """Seed once; an operator's row or replacement file is never rewritten.

    Called only by the boot schema owner, not request handlers or monitor workers.
    Persist the seed marker with the insert so deleting a model cannot undo an
    operator's choice on reboot. Adopt existing rows by either identity without
    rewriting them, including on upgrades that predate the marker.
    """
    if not (VISION_MODELS_DIR / DEFAULT_FILENAME).is_file():
        return
    with Session(engine) as db, db.begin():
        if db.get(SystemConfig, SEED_MARKER_KEY) is not None:
            return
        existing = db.scalar(select(VisionModel.id).where(or_(
            VisionModel.filename == DEFAULT_FILENAME,
            VisionModel.name == DEFAULT_NAME,
        )).limit(1))
        db.add(SystemConfig(key=SEED_MARKER_KEY, value=True))
        if existing is not None:
            return
        db.add(VisionModel(
            name=DEFAULT_NAME,
            detection_type='spaghetti',
            filename=DEFAULT_FILENAME,
            version='1.0',
            input_size=416,
            is_active=1,
            metadata_json=json.dumps({'inference_contract': {
                'version': 1,
                'format': 'obico_boxes_confs',
                'class_count': 1,
                'positive_class_ids': [0],
            }}),
        ))
