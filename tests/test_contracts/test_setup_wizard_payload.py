"""The React payload assertion uses this same complete request fixture."""
import json
from pathlib import Path
from modules.system.routes_setup import SetupTestPrinterRequest, SetupPrinterRequest


def test_actual_prusalink_wizard_payload_validates_both_backend_models():
    payload = json.loads((Path(__file__).parents[1] / 'fixtures/setup_prusalink_payload.json').read_text())
    for model in (SetupTestPrinterRequest, SetupPrinterRequest):
        request = model.model_validate(payload)
        assert request.api_type == 'prusalink'
        assert request.api_host == payload['api_host']
        assert request.api_key == payload['api_key']
