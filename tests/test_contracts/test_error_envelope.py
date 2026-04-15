"""
Contract test — OdinError + next_actions envelope (v1.8.9).

Verifies the machine-readable error shape that backs the MCP tool
layer. Agents branch on `error.code` so renaming a code = breaking
change.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def test_error_code_enum_has_expected_members():
    from core.errors import ErrorCode

    # Spot-check the codes the agent surface depends on. Adding codes
    # is fine; removing or renaming is a breaking change.
    required = {
        "printer_not_found",
        "job_not_found",
        "permission_denied",
        "scope_denied",
        "idempotency_conflict",
        "itar_outbound_blocked",
        "validation_failed",
        "quota_exceeded",
        "internal_error",
    }
    present = {c.value for c in ErrorCode}
    missing = required - present
    assert not missing, f"ErrorCode missing required members: {missing}"


def test_odin_error_default_retriable_derivation():
    from core.errors import OdinError, ErrorCode

    err = OdinError(ErrorCode.printer_not_found, "missing", status=404)
    assert err.retriable is False

    err2 = OdinError(ErrorCode.rate_limited, "slow down", status=429)
    assert err2.retriable is True

    err3 = OdinError(ErrorCode.internal_error, "oops", status=500)
    assert err3.retriable is True


def test_odin_error_explicit_retriable_override():
    from core.errors import OdinError, ErrorCode

    err = OdinError(
        ErrorCode.upstream_unavailable,
        "terminal — do not retry",
        status=503,
        retriable=False,
    )
    assert err.retriable is False


def test_odin_error_to_envelope_shape():
    from core.errors import OdinError, ErrorCode

    err = OdinError(
        ErrorCode.quota_exceeded,
        "Monthly quota exhausted",
        status=402,
        extra={"used_grams": 1050, "limit_grams": 1000},
    )
    env = err.to_envelope()
    assert env["error"]["code"] == "quota_exceeded"
    assert env["error"]["detail"] == "Monthly quota exhausted"
    assert env["error"]["retriable"] is False
    assert env["error"]["used_grams"] == 1050
    assert env["error"]["limit_grams"] == 1000


def test_odin_error_string_code_accepted():
    """OdinError(code_str) works as well as OdinError(ErrorCode.enum)."""
    from core.errors import OdinError

    err = OdinError("printer_not_found", "p42", status=404)
    assert err.code.value == "printer_not_found"


def test_odin_error_invalid_string_code_raises():
    from core.errors import OdinError

    with pytest.raises(ValueError):
        OdinError("not_a_real_code", "x", status=400)


def test_next_action_helper():
    from core.responses import next_action

    na = next_action("get_job", {"id": 42}, "check status")
    assert na == {"tool": "get_job", "args": {"id": 42}, "reason": "check status"}


def test_next_action_minimal():
    from core.responses import next_action

    na = next_action("list_queue")
    assert na == {"tool": "list_queue"}


def test_build_next_actions():
    from core.responses import build_next_actions, next_action

    out = build_next_actions(
        next_action("get_job", {"id": 1}, "check"),
        next_action("list_queue"),
    )
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["tool"] == "get_job"
    assert out[1]["tool"] == "list_queue"


def test_build_next_actions_skips_none_entries():
    from core.responses import build_next_actions, next_action

    out = build_next_actions(next_action("a"), None, next_action("b"))  # type: ignore
    assert len(out) == 2
    assert [e["tool"] for e in out] == ["a", "b"]
