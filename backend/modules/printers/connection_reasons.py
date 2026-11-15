"""Why a printer connection test failed, as a machine-readable reason.

A reason is derived only from what the test observed: the exception class a
transport raised, an HTTP status, or an MQTT CONNACK code. Anything the
protocol cannot tell apart is UNKNOWN; the reason is never guessed. No reason
or message built here carries the printer address, serial or credential.
"""
import errno
import socket
import ssl

UNREACHABLE = "unreachable"            # no route, timeout, refused, name does not resolve
AUTH = "auth"                          # reached; credentials rejected
NOT_THIS_TYPE = "not_this_type"        # answered, but not with this protocol
BLOCKED_ADDRESS = "blocked_address"    # refused by ODIN's device destination policy
CONTROL_DISABLED = "control_disabled"  # reserved: no current test path can observe it
UNKNOWN = "unknown"

REASONS = (UNREACHABLE, AUTH, NOT_THIS_TYPE, BLOCKED_ADDRESS, CONTROL_DISABLED, UNKNOWN)

# MQTT 3.1.1 CONNACK 4/5, and paho's MQTT 5 reason codes for the same refusals.
_MQTT_AUTH_REFUSALS = frozenset({4, 5, 134, 135})
_UNREACHABLE_ERRNOS = frozenset({
    errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN,
    errno.ENETDOWN, errno.ETIMEDOUT, errno.EADDRNOTAVAIL,
})


def failed(reason: str, error: str) -> dict:
    """The test-connection failure body: the existing fields plus `reason`."""
    return {"success": False, "error": error, "reason": reason if reason in REASONS else UNKNOWN}


def reason_for_exception(exc) -> str:
    """Classify what a transport raised. Unrecognised errors are UNKNOWN."""
    from core.webhook_utils import DeviceDestinationBlocked, DeviceHostUnresolved
    if isinstance(exc, DeviceHostUnresolved):
        return UNREACHABLE
    if isinstance(exc, DeviceDestinationBlocked):
        return BLOCKED_ADDRESS
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency of the HTTP probes
        httpx = None
    if httpx is not None and isinstance(exc, httpx.HTTPError):
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
            return UNREACHABLE
        if isinstance(exc, httpx.DecodingError):
            return NOT_THIS_TYPE
        # RemoteProtocolError also covers a valid server closing without a response.
        return UNKNOWN
    if isinstance(exc, (TimeoutError, ConnectionRefusedError, socket.gaierror)):
        return UNREACHABLE
    if isinstance(exc, ssl.SSLError):
        # Only an explicit TLS protocol mismatch identifies the wrong service.
        # Internal-error alerts and closed handshakes can come from a valid printer.
        if getattr(exc, "reason", None) in ("WRONG_VERSION_NUMBER", "HTTP_REQUEST"):
            return NOT_THIS_TYPE
        return UNKNOWN
    if isinstance(exc, OSError) and exc.errno in _UNREACHABLE_ERRNOS:
        return UNREACHABLE
    return UNKNOWN


def reason_for_http_status(status_code) -> str:
    """A non-200 answer from the protocol's own endpoint."""
    if status_code in (401, 403):
        return AUTH
    if status_code == 404:
        return NOT_THIS_TYPE
    return UNKNOWN


def reason_for_mqtt_refusal(code) -> str:
    """A CONNACK refusal: only bad-credential codes are AUTH."""
    value = getattr(code, "value", code)
    return AUTH if value in _MQTT_AUTH_REFUSALS else UNKNOWN
