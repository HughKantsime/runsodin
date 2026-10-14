"""Claim F21-21.1-04: Bambu serial + access code stored Fernet-encrypted at rest.

Drives the real printer CRUD routes against an owned SQLite store, reads the raw
column, then loads it back through the real MQTT monitor daemon loader.
"""
from contextlib import contextmanager
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db

SERIAL = '01P00A000000001'
ACCESS = '87654321'


@pytest.fixture
def crud(token_client, monkeypatch):
    from modules.printers import routes_crud
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('ENCRYPTION_KEY', key)
    client, issue = token_client
    client.app.include_router(routes_crud.router)
    return client, issue(['write:printers', 'read:printers']), key


def _raw(db, pid):
    return db.execute(text('SELECT api_key FROM printers WHERE id=:id'), {'id': pid}).scalar_one()


def test_create_stores_fernet_ciphertext_and_monitor_reloads_credentials(crud, review_db, monkeypatch):
    client, headers, key = crud
    response = client.post('/printers', headers=headers, json={
        'name': 'Claim X1C', 'model': 'X1C', 'slot_count': 4, 'api_type': 'bambu',
        'api_host': '192.168.50.10', 'api_key': f'{SERIAL}|{ACCESS}'})
    assert response.status_code == 201, response.text
    body = response.text
    assert SERIAL not in body and ACCESS not in body
    pid = response.json()['id']
    stored = _raw(review_db, pid)
    assert stored.startswith('gAAAA') and SERIAL not in stored and ACCESS not in stored
    assert Fernet(key.encode()).decrypt(stored.encode()).decode() == f'{SERIAL}|{ACCESS}'
    assert client.get(f'/printers/{pid}', headers=headers).json().get('has_api_key') is True
    # Reload path: the real Bambu MQTT daemon loader decrypts serial/access code.
    from modules.printers.monitors import mqtt_monitor
    bind = review_db.get_bind()
    monkeypatch.setattr(mqtt_monitor, 'engine', bind)
    daemon = mqtt_monitor.MQTTMonitorDaemon.__new__(mqtt_monitor.MQTTMonitorDaemon)
    loaded = [p for p in daemon.load_printers() if p['id'] == pid]
    assert loaded == [{'id': pid, 'name': 'Claim X1C', 'ip': '192.168.50.10', 'serial': SERIAL, 'access_code': ACCESS}]


def test_update_reencrypts_and_wrong_key_cannot_read(crud, review_db, monkeypatch):
    client, headers, key = crud
    pid = client.post('/printers', headers=headers, json={
        'name': 'Claim P1S', 'model': 'P1S', 'slot_count': 4, 'api_type': 'bambu',
        'api_host': '192.168.50.11', 'api_key': f'{SERIAL}|{ACCESS}'}).json()['id']
    before = _raw(review_db, pid)
    response = client.patch(f'/printers/{pid}', headers=headers, json={'api_key': f'{SERIAL}|11112222'})
    assert response.status_code == 200, response.text
    after = _raw(review_db, pid)
    assert after != before and '11112222' not in after
    assert Fernet(key.encode()).decrypt(after.encode()).decode() == f'{SERIAL}|11112222'
    with pytest.raises(Exception):
        Fernet(Fernet.generate_key()).decrypt(after.encode())


def test_missing_encryption_key_refuses_plaintext_storage(crud, review_db, monkeypatch):
    client, headers, _ = crud
    monkeypatch.delenv('ENCRYPTION_KEY')
    count = review_db.execute(text('SELECT COUNT(*) FROM printers')).scalar_one()
    try:
        response = client.post('/printers', headers=headers, json={
            'name': 'Claim A1', 'model': 'A1', 'slot_count': 4, 'api_type': 'bambu',
            'api_host': '192.168.50.12', 'api_key': f'{SERIAL}|{ACCESS}'})
        assert response.status_code >= 400
    except RuntimeError:
        pass
    review_db.rollback()
    assert review_db.execute(text('SELECT COUNT(*) FROM printers')).scalar_one() == count
    assert review_db.execute(text('SELECT COUNT(*) FROM printers WHERE api_key LIKE :s'), {'s': f'%{SERIAL}%'}).scalar_one() == 0
