"""Authenticated WebSocket message and audience-isolation checks."""

from __future__ import annotations

import pytest
from anyio import WouldBlock
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ops.edu_readiness.api_load import _prepare_database


@pytest.fixture(scope="module")
def websocket_stack(tmp_path_factory: pytest.TempPathFactory):
    app, tokens = _prepare_database(tmp_path_factory.mktemp("websocket-privacy") / "odin.db")
    with TestClient(app) as client:
        yield client, tokens


def test_anonymous_websocket_fails_closed(websocket_stack):
    client, _ = websocket_stack
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/api/v1/ws"):
            pass
    assert error.value.code == 4001


def test_regular_access_token_cannot_be_reused_in_websocket_url(websocket_stack):
    client, tokens = websocket_stack
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(f"/api/v1/ws?token={tokens['viewer'][0]}"):
            pass
    assert error.value.code == 4001


def test_global_api_key_cannot_be_used_as_websocket_url_token(websocket_stack, monkeypatch):
    client, _ = websocket_stack
    from core.config import settings

    monkeypatch.setattr(settings, "api_key", "synthetic-global-api-key")
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/api/v1/ws?token=synthetic-global-api-key"):
            pass
    assert error.value.code == 4001


def test_websocket_subscription_ping_and_user_audience_isolation(websocket_stack):
    client, tokens = websocket_stack
    from core.app import ws_manager

    with client.websocket_connect(f"/api/v1/ws?token={tokens['ws_target'][0]}") as target:
        with client.websocket_connect(f"/api/v1/ws?token={tokens['ws_foreign'][0]}") as foreign:
            for session in (target, foreign):
                session.send_json({"type": "subscribe", "channels": ["events"]})
                assert session.receive_json() == {"type": "subscribed", "channels": ["events"]}
                session.send_text("ping")
                assert session.receive_text() == "pong"

            delivered = client.portal.call(
                ws_manager.broadcast,
                {
                    "type": "privacy_probe",
                    "data": {"marker": "synthetic"},
                    "_audience": {"user_ids": [1]},
                },
            )
            assert delivered == 1
            assert target.receive_json() == {
                "type": "privacy_probe",
                "data": {"marker": "synthetic"},
            }
            with pytest.raises(WouldBlock):
                foreign._send_rx.receive_nowait()


def test_printer_events_are_scoped_to_the_printer_tenant(websocket_stack):
    client, tokens = websocket_stack
    from core.app import ws_manager

    with client.websocket_connect(f"/api/v1/ws?token={tokens['ws_target'][0]}") as target:
        with client.websocket_connect(f"/api/v1/ws?token={tokens['ws_foreign'][0]}") as foreign:
            delivered = client.portal.call(
                ws_manager.broadcast,
                {"type": "printer_telemetry", "data": {"printer_id": 9001, "state": "idle"}},
            )
            assert delivered == 1
            assert target.receive_json()["data"]["printer_id"] == 9001
            with pytest.raises(WouldBlock):
                foreign._send_rx.receive_nowait()
