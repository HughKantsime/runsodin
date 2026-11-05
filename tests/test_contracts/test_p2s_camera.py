"""Verified P2S camera model uses the existing RTSPS path; no hardware calls."""
from types import SimpleNamespace
import pytest
from modules.printers.camera_urls import get_camera_url, BAMBU_RTSPS_CAMERA_MODELS
from modules.printers.printer_models import bambu_model_name


@pytest.mark.parametrize('reported', ['P2S', 'N7', 'Bambu Lab P2S'])
def test_p2s_reported_models_resolve_to_rtsps(monkeypatch, reported):
    from modules.printers import camera_urls
    monkeypatch.setattr(camera_urls.crypto, 'decrypt', lambda value: 'SYNTHETIC|test-code')
    model = bambu_model_name(reported)
    assert model == 'P2S'
    printer = SimpleNamespace(model=model, machine_type=model, api_type='bambu',
                              api_host='192.0.2.8', api_key='synthetic', camera_url=None)
    assert get_camera_url(printer) == 'rtsps://bblp:test-code@192.0.2.8:322/streaming/live/1'


@pytest.mark.parametrize('model', ['H2S', 'H2C', 'H2D Pro'])
def test_unverified_models_remain_out(model):
    assert model not in BAMBU_RTSPS_CAMERA_MODELS
    assert get_camera_url(SimpleNamespace(model=model, api_type='bambu',
        api_host='192.0.2.8', api_key='synthetic', camera_url=None)) is None
