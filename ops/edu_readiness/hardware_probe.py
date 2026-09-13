"""Compatibility exports for the dedicated hardware-certification package.

This module intentionally does not import ODIN's mixed read/write adapters.
Its public transports expose only the operations allowed by the certification
policy, and every mutating operation raises before reaching the network.
"""

from __future__ import annotations

from typing import Any

from ops.hardware_certification.passive.transports import (
    PassiveMqttTransport,
    PassivePolicyError,
    PassiveWebSocketTransport,
    ReadOnlyHttpTransport,
)

CertificationMutationBlocked = PassivePolicyError


def redact_observation(protocol: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return capability/version evidence without identity, host, or job content."""
    allowed = {
        "protocol",
        "model_family",
        "firmware_version",
        "api_version",
        "capabilities",
        "latency_ms",
        "freshness_seconds",
        "status",
        "reason",
    }
    redacted = {key: value for key, value in payload.items() if key in allowed}
    redacted["protocol"] = protocol
    return redacted
