"""TEST ONLY: let worker subprocesses reach a test device on 127.0.0.1.

Production code has no switch for this. A test that spawns a real worker
process against a loopback device server prepends this directory to
PYTHONPATH (tests/support/loopback_devices.allow_loopback_devices); Python then imports this file at interpreter
start-up and exempts exactly 127.0.0.1 and ::1 from the device destination policy.
"""
try:
    import core.webhook_utils as _webhook_utils
except Exception:  # pragma: no cover - backend not importable in this interpreter
    _webhook_utils = None

if _webhook_utils is not None:
    _real_blocked = _webhook_utils.is_blocked_device_address
    _webhook_utils.is_blocked_device_address = (
        lambda ip: False if str(ip) in ("127.0.0.1", "::1") else _real_blocked(ip))
