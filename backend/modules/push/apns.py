"""
modules/push/apns.py — Apple Push Notification service (APNs) HTTP/2 provider.

Uses httpx with HTTP/2 support for efficient connection pooling.
JWT-based authentication (.p8 key) — no certificates required.

Configuration (environment variables):
  APNS_KEY_ID      — 10-char key ID from Apple Developer portal
  APNS_TEAM_ID     — 10-char Apple Developer Team ID
  APNS_KEY_PATH    — path to .p8 private key file (or APNS_KEY_CONTENT for inline)
  APNS_BUNDLE_ID   — app bundle ID, e.g. com.jlls.odin

Two provider instances are maintained:
  - production  (api.push.apple.com)
  - sandbox     (api.sandbox.push.apple.com)

Platform field on PushDevice determines which endpoint to use.
"""

import json
import logging
import os
import time
from typing import Optional
from dataclasses import dataclass
import re

import httpx
import jwt as pyjwt

log = logging.getLogger("push.apns")

APNS_PRODUCTION_HOST = "https://api.push.apple.com"
APNS_SANDBOX_HOST = "https://api.sandbox.push.apple.com"

# JWT auth token is valid for 60 minutes; regenerate at 50-minute mark
_TOKEN_TTL = 60 * 60
_TOKEN_REFRESH_MARGIN = 10 * 60  # regenerate when < 10 min remaining


@dataclass(frozen=True)
class PushResult:
    status: str

    @property
    def sent(self):
        return self.status == "sent"


def failure_class(status, reason):
    if reason in {"BadDeviceToken", "Unregistered", "ExpiredToken"} or status == 410:
        return "bad_device_token"
    if reason in {"DeviceTokenNotForTopic", "BadTopic", "TopicDisallowed", "MissingTopic"}:
        return "topic_mismatch"
    if status in {401, 403} or reason in {"InvalidProviderToken", "ExpiredProviderToken", "MissingProviderToken"}:
        return "auth_error"
    return "other"


class _HideDeviceURL(logging.Filter):
    def filter(self, record):
        # HTTP/2's HPACK debug traces contain raw Authorization and path headers.
        # They are protocol internals, never suitable for application logs.
        if record.name.startswith("hpack.") and record.levelno < logging.WARNING:
            return False
        # httpx logs request URLs even when our own provider logging is safe.
        record.msg = re.sub(r"/3/device/[^\s\"]+", "/3/device/[redacted]", record.getMessage())
        record.args = ()
        return True


for _logger_name in ("httpx", "hpack.hpack", "hpack.table"):
    logging.getLogger(_logger_name).addFilter(_HideDeviceURL())


class APNsProvider:
    """
    HTTP/2 APNs provider. One instance per environment (production / sandbox).

    Thread-safety: httpx.Client is not thread-safe for concurrent sends;
    use separate provider instances or wrap calls in a lock if needed.
    For the ODIN use case (low-volume farm alerts), sequential delivery is fine.
    """

    def __init__(self, sandbox: bool = False):
        self.sandbox = sandbox
        self.host = APNS_SANDBOX_HOST if sandbox else APNS_PRODUCTION_HOST
        self._client: Optional[httpx.Client] = None
        self._jwt_token: Optional[str] = None
        self._jwt_issued_at: float = 0.0

    def _get_config(self) -> dict:
        """Read APNs config from environment / settings."""
        key_id = os.getenv("APNS_KEY_ID", "")
        team_id = os.getenv("APNS_TEAM_ID", "")
        bundle_id = os.getenv("APNS_BUNDLE_ID", "com.jlls.odin")

        key_content = os.getenv("APNS_KEY_CONTENT", "")
        if not key_content:
            key_path = os.getenv("APNS_KEY_PATH", "")
            if key_path and os.path.exists(key_path):
                with open(key_path) as f:
                    key_content = f.read()

        return {
            "key_id": key_id,
            "team_id": team_id,
            "bundle_id": bundle_id,
            "key_content": key_content,
        }

    def _is_configured(self) -> bool:
        # v1.8.9 (codex pass 18): APNs targets api.push.apple.com, which
        # is by definition public infrastructure. Under ODIN_ITAR_MODE=1
        # we hard-disable the provider regardless of credentials — DNS
        # pinning can't make Apple's endpoint non-public. Operators who
        # need mobile push in an ITAR deployment must stand up an
        # internal gateway and route through user-configured webhooks.
        from core.itar import is_itar_mode
        if is_itar_mode():
            return False
        cfg = self._get_config()
        return bool(cfg["key_id"] and cfg["team_id"] and cfg["key_content"])

    def _get_jwt(self) -> str:
        """Return a valid JWT auth token, regenerating if near expiry."""
        now = time.time()
        if self._jwt_token and (now - self._jwt_issued_at) < (_TOKEN_TTL - _TOKEN_REFRESH_MARGIN):
            return self._jwt_token

        cfg = self._get_config()
        payload = {
            "iss": cfg["team_id"],
            "iat": int(now),
        }
        headers = {
            "alg": "ES256",
            "kid": cfg["key_id"],
        }
        self._jwt_token = pyjwt.encode(
            payload,
            cfg["key_content"],
            algorithm="ES256",
            headers=headers,
        )
        self._jwt_issued_at = now
        return self._jwt_token

    def _client_instance(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(http2=True, base_url=self.host, timeout=10.0)
        return self._client

    def send_push(self, device_token: str, title: str, body: str, category: str,
                  badge: Optional[int] = None, data: Optional[dict] = None, priority: int = 10) -> bool:
        """Compatibility boolean API for alert and activity callers."""
        return self.send_push_result(device_token, title, body, category, badge, data, priority).sent

    def send_push_result(self, device_token: str, title: str, body: str, category: str,
                         badge: Optional[int] = None, data: Optional[dict] = None,
                         priority: int = 10) -> PushResult:
        """Return only an allowlisted outcome; never expose provider text or credentials."""
        try:
            if not self._is_configured():
                return PushResult("not_configured")
            cfg = self._get_config()
            aps = {"alert": {"title": title, "body": body}, "sound": "default"}
            if category:
                aps["category"] = category
            if badge is not None:
                aps["badge"] = badge
            payload = {**(data or {}), "aps": aps}
            return self._send_result(device_token, payload, cfg["bundle_id"], priority=priority)
        except Exception:
            log.warning("APNs delivery failed: other")
            return PushResult("other")

    def send_background_push(
        self,
        device_token: str,
        content_state: dict,
        bundle_id_suffix: str = "",
    ) -> bool:
        """
        Send a background Live Activity content-state update (content-available=1).
        No visible notification — iOS delivers directly to the Live Activity.
        """
        if not self._is_configured():
            return False

        cfg = self._get_config()
        payload = {
            "aps": {
                "content-available": 1,
                "timestamp": int(time.time()),
                "event": "update",
                "content-state": content_state,
            }
        }

        # Live Activity pushes go to the activity-specific bundle ID suffix
        bundle_id = cfg["bundle_id"] + bundle_id_suffix
        return self._send(device_token, payload, bundle_id, priority=5, push_type="liveactivity")

    def send_live_activity_end(self, device_token: str, content_state: dict) -> bool:
        """Signal the Live Activity to end (dismissal-date in the future = user can dismiss)."""
        if not self._is_configured():
            return False

        cfg = self._get_config()
        payload = {
            "aps": {
                "timestamp": int(time.time()),
                "event": "end",
                "content-state": content_state,
                "dismissal-date": int(time.time()) + 3600,  # keep on lock screen for 1h
            }
        }
        return self._send(device_token, payload, cfg["bundle_id"], priority=10, push_type="liveactivity")

    def _send(self, device_token, payload, bundle_id, priority=10, push_type="alert") -> bool:
        return self._send_result(device_token, payload, bundle_id, priority, push_type).sent

    def _send_result(self, device_token, payload, bundle_id, priority=10, push_type="alert") -> PushResult:
        try:
            headers = {
                "authorization": f"bearer {self._get_jwt()}",
                "apns-push-type": push_type, "apns-priority": str(priority),
                "apns-topic": bundle_id, "content-type": "application/json",
            }
            resp = self._client_instance().post(
                f"/3/device/{device_token}", content=json.dumps(payload), headers=headers)
            if resp.status_code == 200:
                return PushResult("sent")
            try:
                reason = resp.json().get("reason", "")
            except (ValueError, AttributeError):
                reason = ""
            result = PushResult(failure_class(resp.status_code, reason))
        except Exception:
            # Exceptions may embed the URL, token, signing key, or Authorization header.
            result = PushResult("other")
        log.warning("APNs delivery failed: %s", result.status)
        return result

    def close(self):
        if self._client:
            self._client.close()
            self._client = None


# Module-level singletons — created lazily on first use
_production: Optional[APNsProvider] = None
_sandbox: Optional[APNsProvider] = None


def get_provider(sandbox: bool = False) -> APNsProvider:
    global _production, _sandbox
    if sandbox:
        if _sandbox is None:
            _sandbox = APNsProvider(sandbox=True)
        return _sandbox
    if _production is None:
        _production = APNsProvider(sandbox=False)
    return _production


def warn_if_unconfigured(engine):
    """One startup warning if registrations exist without usable APNs config."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM push_devices LIMIT 1")).first()
        if exists and not get_provider()._is_configured():
            log.warning("Push devices are registered but APNs is not configured; native alerts cannot be delivered")
    except Exception:
        log.warning("APNs startup configuration check failed")
