"""Native transport metadata uses stored facts, never device probes."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_camera_scope_behavior import camera_client, token_client, review_db


@pytest.mark.parametrize('url,transport', [
    ('http://192.0.2.4/stream', 'mjpeg'), ('https://192.0.2.4/stream', 'mjpeg'),
    ('bambu-chamber://', 'mjpeg'), ('rtsps://192.0.2.8/live', 'webrtc'),
    ('rtsp://192.0.2.8/live', 'webrtc'),
])
def test_camera_choice(camera_client, monkeypatch, url, transport):
    from modules.printers import camera_routes
    from modules.printers.bambu_chamber_camera import SOURCE
    if url == 'bambu-chamber://': url = SOURCE
    client, issue, calls = camera_client
    monkeypatch.setattr(camera_routes, 'get_camera_url', lambda printer: url)
    response = client.get('/cameras/9/stream', headers=issue(['read:printers']))
    assert response.status_code == 200
    assert response.json()['transport'] == transport
    assert url not in response.text
    assert calls == []


def test_disabled_camera_does_not_offer_a_transport(camera_client, review_db):
    client, issue, _ = camera_client
    review_db.execute(text('UPDATE printers SET camera_enabled=0 WHERE id=9')); review_db.commit()
    assert client.get('/cameras/9/stream', headers=issue(['read:printers'])).status_code == 404
