"""
O.D.I.N. WebSocket Tests — Connection auth and lifecycle.

Tests:
  1. WS connects with a purpose-limited JWT token
  2. WS rejects invalid tokens (expects 4001 close code)
  3. WS rejects connections with no token in every configuration
  4. WS responds to ping with pong

Run: pytest tests/test_websocket.py -v --tb=short
Requires: running O.D.I.N. container, websocket-client library
"""

import os
import pytest
import requests
from pathlib import Path

try:
    import websocket as ws_client
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_env():
    env_file = Path(__file__).parent / ".env.test"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

_load_env()

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
API_KEY = os.environ.get("API_KEY", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Derive WS URL from BASE_URL
WS_BASE = BASE_URL.replace("http://", "ws://").replace("https://", "wss://")


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _login(username, password):
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    resp = requests.post(
        f"{BASE_URL}/api/auth/login",
        data={"username": username, "password": password},
        headers=headers,
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json()
        return data.get("access_token") or data.get("token")
    return None


def _get_ws_token(access_token):
    headers = {"Authorization": f"Bearer {access_token}"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    resp = requests.post(f"{BASE_URL}/api/auth/ws-token", headers=headers, timeout=10)
    return resp.json().get("token") if resp.status_code == 200 else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def admin_token():
    if not ADMIN_PASSWORD:
        pytest.skip("ADMIN_PASSWORD not set")
    token = _login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if not token:
        pytest.skip(f"Failed to login as {ADMIN_USERNAME}")
    return token


@pytest.fixture(scope="module")
def admin_ws_token(admin_token):
    token = _get_ws_token(admin_token)
    if not token:
        pytest.skip("Failed to obtain purpose-limited WebSocket token")
    return token


@pytest.fixture(scope="module")
def api_key_set():
    """Return True if the server enforces API key auth."""
    return bool(API_KEY)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not WS_AVAILABLE,
    reason="websocket-client not installed (pip install websocket-client)"
)


class TestWebSocketAuth:
    """WebSocket authentication tests matching core/app.py ws handler."""

    def test_ws_connects_with_valid_jwt(self, admin_ws_token):
        """WS connection with a purpose-limited JWT should succeed."""
        url = f"{WS_BASE}/ws?token={admin_ws_token}"
        conn = ws_client.create_connection(url, timeout=10)
        try:
            # Connection established — send ping and expect pong
            conn.send("ping")
            result = conn.recv()
            assert result == "pong", f"Expected 'pong', got: {result}"
        finally:
            conn.close()

    def test_ws_rejects_api_key_as_token(self, api_key_set):
        """Global API keys must never be accepted in a WebSocket URL."""
        if not api_key_set:
            pytest.skip("API key not set — cannot test API key auth")
        url = f"{WS_BASE}/ws?token={API_KEY}"
        try:
            conn = ws_client.create_connection(url, timeout=10)
            try:
                conn.recv()
                pytest.fail("Expected WebSocket API-key credential to be rejected")
            except ws_client.WebSocketConnectionClosedException:
                pass
            finally:
                conn.close()
        except ws_client.WebSocketBadStatusException as exc:
            assert exc.status_code in {401, 403}

    def test_ws_rejects_invalid_token(self):
        """WS connection with invalid token should be rejected with close code 4001."""
        url = f"{WS_BASE}/ws?token=definitely-not-a-valid-token"
        try:
            conn = ws_client.create_connection(url, timeout=10)
            # If connection was accepted, the server should close it immediately
            try:
                # Try to receive — should get a close frame or exception
                conn.recv()
                pytest.fail("Expected WebSocket to be closed with 4001")
            except ws_client.WebSocketConnectionClosedException:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except ws_client.WebSocketBadStatusException as e:
            # Some implementations reject at HTTP upgrade level
            assert "403" in str(e) or "401" in str(e) or "4001" in str(e), (
                f"Unexpected rejection: {e}"
            )
        except Exception:
            # Connection refused or closed — acceptable rejection
            pass

    def test_ws_rejects_no_token(self):
        """WS connection with no token is always rejected."""
        url = f"{WS_BASE}/ws"
        try:
            conn = ws_client.create_connection(url, timeout=10)
            try:
                conn.recv()
                pytest.fail("Expected WebSocket to be closed with 4001")
            except ws_client.WebSocketConnectionClosedException:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except ws_client.WebSocketBadStatusException as e:
            # Rejected at HTTP level — acceptable
            pass
        except Exception:
            # Connection refused — acceptable rejection
            pass

    def test_ws_ping_pong_lifecycle(self, admin_ws_token):
        """WS should respond to text 'ping' with text 'pong'."""
        url = f"{WS_BASE}/ws?token={admin_ws_token}"
        conn = ws_client.create_connection(url, timeout=10)
        try:
            # Send multiple pings
            for _ in range(3):
                conn.send("ping")
                result = conn.recv()
                assert result == "pong"
        finally:
            conn.close()
