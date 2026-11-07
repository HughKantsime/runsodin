"""Boot registration and RGB preprocessing, using owned databases and model files."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil

import numpy as np
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from core.schema.bootstrap import configure_orm
from core.models import SystemConfig
from modules.vision import default_model, inference_engine
from modules.vision.models import VisionModel
from modules.vision.output_contract import OutputContract
from tests.test_contracts.test_post027_migration_033_postgres import pg_engine

ROOT = Path(__file__).resolve().parents[2]
MODEL_HASH = '0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa'


@pytest.fixture(params=['sqlite', 'postgresql'])
def registry_engine(request, tmp_path):
    configure_orm()
    engine = (create_engine(f"sqlite:///{tmp_path / 'registry.db'}")
              if request.param == 'sqlite' else request.getfixturevalue('pg_engine'))
    VisionModel.__table__.create(engine)
    SystemConfig.__table__.create(engine)
    try:
        yield engine
    finally:
        VisionModel.__table__.drop(engine)
        SystemConfig.__table__.drop(engine)
        if request.param == 'sqlite':
            engine.dispose()


@pytest.fixture
def model_file(tmp_path, monkeypatch):
    monkeypatch.setattr(default_model, 'VISION_MODELS_DIR', tmp_path)
    path = tmp_path / default_model.DEFAULT_FILENAME
    path.write_bytes(b'operator-owned model bytes')
    return path


def rows(engine):
    with Session(engine) as db:
        return [
            {column.name: getattr(model, column.name) for column in VisionModel.__table__.columns}
            for model in db.scalars(select(VisionModel).order_by(VisionModel.id))
        ]


def seed_marker(engine):
    with Session(engine) as db:
        return db.get(SystemConfig, 'vision_default_model_seeded')


def test_fresh_registration_and_second_boot_noop(registry_engine, model_file):
    default_model.register_default_model(registry_engine)
    first = rows(registry_engine)
    assert len(first) == 1
    assert seed_marker(registry_engine).value is True
    row = first[0]
    assert (row['name'], row['detection_type'], row['filename'], row['version'],
            row['input_size'], row['is_active']) == (
        'Obico Spaghetti Detector', 'spaghetti', 'obico_spaghetti.onnx', '1.0', 416, 1)
    assert row['uploaded_at'] is not None
    assert OutputContract.from_metadata(row['metadata_json']) == OutputContract(
        'obico_boxes_confs', None, 1, (0,))
    default_model.register_default_model(registry_engine)
    assert rows(registry_engine) == first
    assert model_file.read_bytes() == b'operator-owned model bytes'


@pytest.mark.parametrize('identity', ['original', 'renamed', 'replacement_filename'])
@pytest.mark.parametrize('active', [0, 1])
def test_operator_row_and_file_untouched(registry_engine, model_file, identity, active):
    with Session(registry_engine) as db, db.begin():
        db.add(VisionModel(
            name='Operator name' if identity == 'renamed' else default_model.DEFAULT_NAME,
            filename='replacement.onnx' if identity == 'replacement_filename' else model_file.name,
            detection_type='spaghetti', version='operator-version', input_size=640,
            is_active=active, metadata_json='{"operator": "metadata"}',
        ))
    before = rows(registry_engine)
    assert seed_marker(registry_engine) is None
    default_model.register_default_model(registry_engine)
    assert seed_marker(registry_engine).value is True
    assert rows(registry_engine) == before
    assert model_file.read_bytes() == b'operator-owned model bytes'


@pytest.mark.parametrize('missing_kind', ['absent', 'directory'])
def test_missing_model_does_not_register(registry_engine, model_file, missing_kind):
    model_file.unlink()
    if missing_kind == 'directory':
        model_file.mkdir()
    default_model.register_default_model(registry_engine)
    assert rows(registry_engine) == []
    assert seed_marker(registry_engine) is None


def test_deactivate_delete_then_reboot_does_not_register(registry_engine, model_file):
    from modules.vision.routes.vision_models import delete_vision_model

    default_model.register_default_model(registry_engine)
    with Session(registry_engine) as db:
        model = db.scalars(select(VisionModel)).one()
        model.is_active = 0
        db.commit()
        assert asyncio.run(delete_vision_model(model.id, current_user={}, db=db))['deleted']
    assert model_file.is_file()
    assert rows(registry_engine) == []
    default_model.register_default_model(registry_engine)
    default_model.register_default_model(registry_engine)
    assert rows(registry_engine) == []
    assert seed_marker(registry_engine).value is True


@pytest.mark.parametrize('value', [True, False])
def test_existing_marker_without_row_prevents_insert(registry_engine, model_file, value):
    with Session(registry_engine) as db, db.begin():
        db.add(SystemConfig(key='vision_default_model_seeded', value=value))
    default_model.register_default_model(registry_engine)
    assert rows(registry_engine) == []
    assert seed_marker(registry_engine).value is value
    assert model_file.read_bytes() == b'operator-owned model bytes'


@pytest.mark.parametrize('failed_model', [SystemConfig, VisionModel])
def test_seed_and_marker_roll_back_together(registry_engine, model_file, failed_model):
    def refuse_insert(*_):
        raise RuntimeError('injected seed failure')

    event.listen(failed_model, 'after_insert', refuse_insert)
    try:
        with pytest.raises(RuntimeError, match='injected seed failure'):
            default_model.register_default_model(registry_engine)
    finally:
        event.remove(failed_model, 'after_insert', refuse_insert)
    assert rows(registry_engine) == []
    assert seed_marker(registry_engine) is None
    default_model.register_default_model(registry_engine)
    assert len(rows(registry_engine)) == 1
    assert seed_marker(registry_engine).value is True


def test_boot_command_registers_after_schema_and_real_inference_loads(tmp_path, monkeypatch, capsys):
    from scripts import bootstrap_database as command

    source = Path(os.environ.get('ODIN_REAL_VISION_MODEL',
                                ROOT / 'backend/vision_models_default/obico_spaghetti.onnx'))
    if not source.is_file() or source.stat().st_size < 1000:
        pytest.skip('hydrated Obico ONNX model not available')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == MODEL_HASH
    models_dir = tmp_path / 'models'
    models_dir.mkdir()
    shutil.copyfile(source, models_dir / default_model.DEFAULT_FILENAME)
    monkeypatch.setattr(default_model, 'VISION_MODELS_DIR', models_dir)
    monkeypatch.setattr(inference_engine, 'VISION_MODELS_DIR', str(models_dir))
    database_url = f"sqlite:///{tmp_path / 'boot.db'}"
    monkeypatch.setenv('DATABASE_URL', database_url)
    monkeypatch.delenv('DATABASE_PASSWORD_FILE', raising=False)
    assert command.main() == 0
    first_boot = json.loads(capsys.readouterr().out)
    engine = create_engine(database_url)
    try:
        first = rows(engine)
        assert len(first) == 1
        monkeypatch.setattr(inference_engine, 'engine', engine)
        detector = inference_engine.VisionInferenceEngine()
        detector.reload_models()
        assert detector.has_model('spaghetti')
        assert detector.get_input_size('spaghetti') == 416
        observation = detector.infer_observation(
            'spaghetti', np.full((480, 640, 3), 128, dtype=np.uint8))
        assert observation.available
        assert command.main() == 0
        second_boot = json.loads(capsys.readouterr().out)
        assert second_boot['applied'] == []
        assert second_boot['schema_fingerprint'] == first_boot['schema_fingerprint']
        assert rows(engine) == first
    finally:
        engine.dispose()


def test_preprocess_converts_bgr_to_rgb_before_normalizing_without_mutating_frame():
    frame = np.full((2, 4, 3), [17, 83, 241], dtype=np.uint8)
    original = frame.copy()
    blob = inference_engine.VisionInferenceEngine()._preprocess(frame, 16)
    assert blob.shape == (1, 3, 16, 16)
    assert blob.dtype == np.float32
    np.testing.assert_allclose(blob[0, :, 0, 0], np.array([241, 83, 17]) / 255)
    np.testing.assert_array_equal(blob, np.broadcast_to(blob[:, :, :1, :1], blob.shape))
    np.testing.assert_array_equal(frame, original)
