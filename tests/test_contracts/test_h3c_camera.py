"""H3C built-in camera contracts using synthetic credentials and offline printers."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from core import crypto
from modules.printers import camera_relay, route_utils
from modules.printers.printer_models import bambu_model_name
from modules.printers.schemas import PrinterResponse
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


@pytest.fixture
def printer(monkeypatch):
    monkeypatch.setenv('ENCRYPTION_KEY', crypto.generate_key())
    return SimpleNamespace(id=7, name='Test printer', api_type='bambu', model='H3C',
                           api_host='192.0.2.7', api_key=crypto.encrypt('SYNTHETIC|test-code'),
                           camera_enabled=True, camera_url=None, created_at=None, updated_at=datetime.now())


@pytest.mark.parametrize('reported', ['H3C', 'N10', 'Bambu Lab H3C'])
def test_h3c_reported_names_generate_a_masked_auto_camera(printer, reported):
    assert bambu_model_name(reported) == 'H3C'
    printer.model = reported
    assert route_utils.get_camera_url(printer) == 'rtsps://bblp:test-code@192.0.2.7:322/streaming/live/1'
    response = PrinterResponse.model_validate(printer)
    assert response.camera_source == 'auto'
    assert response.camera_url == 'rtsps://***@192.0.2.7:322/streaming/live/1'


def test_h3c_stream_uses_only_local_sources_in_itar_mode(printer, monkeypatch):
    monkeypatch.setenv('ODIN_ITAR_MODE', '1')
    monkeypatch.setattr(camera_relay, 'relay_secret', lambda: 'synthetic-relay-key')
    assert 'printer_7' not in route_utils.go2rtc_config_for([printer], '192.0.2.10')['streams']
    printer.api_host = '10.0.0.7'
    streams = route_utils.go2rtc_config_for([printer], '192.0.2.10')['streams']
    assert streams['printer_7'] == 'rtsps://bblp:test-code@10.0.0.7:322/streaming/live/1'


@pytest.mark.parametrize('switch_off', [False, True])
def test_model_identification_preserves_camera_choice(printer, review_db, switch_off):
    from core.printer_identity_audit import observe_printer_identity
    from modules.printers.camera_routes import toggle_camera
    from modules.printers.services import create_printer_record

    row = create_printer_record(review_db, name=printer.name, api_type='bambu',
                                api_host=printer.api_host, api_key=printer.api_key)
    assert row.camera_enabled and route_utils.get_camera_url(row) is None
    if switch_off:
        assert toggle_camera(row.id, {'role': 'admin'}, review_db)['camera_enabled'] is False
    assert observe_printer_identity(review_db, row.id, 'model', bambu_model_name('N10'), only_unset=True)
    review_db.refresh(row)
    assert row.camera_enabled is (not switch_off)
    assert PrinterResponse.model_validate(row).camera_source == 'auto'
