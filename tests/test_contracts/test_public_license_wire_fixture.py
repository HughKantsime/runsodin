"""Owned signed public-license HTTP shape, with identity omission intact."""
import json
import re
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.test_contracts.test_license_user_limits import signed_license


def test_public_license_wire_capture(signed_license, monkeypatch):
    from modules.system import routes_health
    _, info = signed_license(max_users=500, max_printers=20, expires_at='2099-01-01')
    monkeypatch.delenv('ODIN_LICENSE_FILE', raising=False)
    monkeypatch.setattr(routes_health, 'get_license', lambda:info)
    app = FastAPI()
    app.add_api_route('/license', routes_health.get_license_info)
    response = TestClient(app).get('/license')
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {'valid','tier','tier_name','issued_at','expires_at','expired','trial','trial_days_left','max_printers','max_users','features','managed_externally','device_bound','binding_present','binding_matches_current','license_sha256'}
    assert body['expires_at'] == '2099-01-01'
    assert body['max_users'] == 500 and body['max_printers'] == 20
    assert 'licensee' not in body and 'installation_id' not in body
    assert re.fullmatch('[0-9a-f]{64}', body['license_sha256'])
    path = Path(__file__).resolve().parents[2] / 'frontend/src/test-fixtures/public-license-wire-owned.json'
    fixture = {'scope':'Actual owned signed public-license HTTP; identity fields absent; first captured synthetic digest retained', 'volatile_fields':['license_sha256'], 'body':body}
    if path.exists():
        saved = json.loads(path.read_text())
        assert {k:v for k,v in saved['body'].items() if k != 'license_sha256'} == {k:v for k,v in body.items() if k != 'license_sha256'}
        assert re.fullmatch('[0-9a-f]{64}', saved['body']['license_sha256'])
    else:
        path.write_text(json.dumps(fixture,indent=2)+'\n')
