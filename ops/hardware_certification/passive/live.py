"""Real-device passive worker. This module has no command or upload imports."""

from __future__ import annotations

import ssl
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import paho.mqtt.client as mqtt
import requests
import websocket
from modules.printers.printer_models import normalize_model_name

from ..artifact import git_identity, utc_iso
from ..config import ResolvedTarget, load_target
from ..security import SensitiveValueRegistry, target_correlation_sha256
from .observers import (
    ObservationError, ObservationSummary, observe_bambu, observe_elegoo,
    observe_moonraker, observe_prusalink,
)
from .transports import (
    PassiveMqttTransport, PassiveWebSocketTransport, PinnedDigestHttpTransport,
    PinnedReadOnlyHttpTransport, PassivePolicyError,
)


MAX_MQTT_PAYLOAD = 262_144
OBSERVATION_TIMEOUT = 15.0
MAX_RECONNECTS = 1


def _cleanup_preserving_primary(*callbacks: Callable[[], object]) -> None:
    """Attempt every cleanup without replacing an in-flight failure or interrupt."""
    primary_active = sys.exc_info()[0] is not None
    cleanup_failure: BaseException | None = None
    for callback in callbacks:
        try:
            callback()
        except BaseException as exc:
            if cleanup_failure is None:
                cleanup_failure = exc
    if cleanup_failure is not None and not primary_active:
        raise cleanup_failure


def _http(target: ResolvedTarget) -> ObservationSummary:
    connection = target.connection
    transport = PinnedReadOnlyHttpTransport(
        host=connection["host"], address=target.address, port=connection["port"],
        protocol=target.protocol, use_tls=bool(connection.get("tls", False)), timeout=5.0,
    )
    try:
        if target.protocol == "moonraker":
            return observe_moonraker(transport)
        if "api_key" in connection:
            return observe_prusalink(transport, {"X-Api-Key": connection["api_key"]})
        digest = PinnedDigestHttpTransport(
            host=connection["host"], address=target.address, port=connection["port"],
            username=connection["username"], password=connection["password"], timeout=5.0,
        )
        return observe_prusalink(digest)
    except ObservationError:
        raise
    except PassivePolicyError as exc:
        raise ObservationError("passive_protocol_rejected") from exc
    except (OSError, requests.RequestException) as exc:
        raise ObservationError("passive_transport_unavailable") from exc


def _bambu(target: ResolvedTarget) -> ObservationSummary:
    connection = target.connection
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
    client.username_pw_set("bblp", connection["access_code"])
    tls = ssl.create_default_context()
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    client.tls_set_context(tls)
    transport = PassiveMqttTransport(client, connection["device_token"])
    messages: deque[bytes] = deque(maxlen=2)
    complete = threading.Event()
    failure = threading.Event()

    def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
        numeric = reason_code.value if hasattr(reason_code, "value") else reason_code
        if numeric != 0:
            failure.set()
            return
        transport.subscribe(f"device/{connection['device_token']}/report")

    def on_message(_client, _userdata, message):
        if message.topic != f"device/{connection['device_token']}/report" or len(message.payload) > MAX_MQTT_PAYLOAD:
            failure.set()
            return
        if complete.is_set():
            return
        messages.append(bytes(message.payload))
        if len(messages) >= 2:
            complete.set()

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        transport.connect(target.address, connection["port"], keepalive=30)
        client.loop_start()
        if not complete.wait(OBSERVATION_TIMEOUT) or failure.is_set():
            raise ObservationError("passive_samples_unavailable")
    except OSError as exc:
        raise ObservationError("passive_transport_unavailable") from exc
    finally:
        _cleanup_preserving_primary(client.loop_stop, transport.disconnect)
    return observe_bambu(tuple(messages))


def _elegoo(target: ResolvedTarget) -> ObservationSummary:
    connection = target.connection
    url = f"ws://{target.address}:{connection['port']}/websocket"  # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- Elegoo SDCP is LAN-only ws:// and the resolved private peer is pinned
    raw_socket = None
    try:
        raw_socket = socket.create_connection(
            (target.address, connection["port"]), timeout=OBSERVATION_TIMEOUT,
        )
        socket_client = websocket.create_connection(
            url, timeout=OBSERVATION_TIMEOUT, host=connection["host"], suppress_origin=True,
            socket=raw_socket, http_proxy_host=None, http_no_proxy=[target.address],
        )
    except (OSError, websocket.WebSocketException) as exc:
        if raw_socket is not None:
            _cleanup_preserving_primary(raw_socket.close)
        raise ObservationError("passive_transport_unavailable") from exc
    transport = PassiveWebSocketTransport(socket_client)
    try:
        try:
            return observe_elegoo(transport)
        except websocket.WebSocketException as exc:
            raise ObservationError("passive_samples_unavailable") from exc
    finally:
        _cleanup_preserving_primary(transport.close)


def observe_resolved_target(target: ResolvedTarget) -> ObservationSummary:
    worker = {
        "bambu": _bambu, "elegoo": _elegoo,
        "moonraker": _http, "prusalink": _http,
    }.get(target.protocol)
    if worker is None:
        raise ObservationError("unsupported_protocol")
    retryable = {"passive_samples_unavailable", "passive_transport_unavailable"}
    for reconnect_count in range(MAX_RECONNECTS + 1):
        try:
            return replace(worker(target), reconnect_count=reconnect_count)
        except ObservationError as exc:
            if exc.reason_code not in retryable or reconnect_count == MAX_RECONNECTS:
                raise
    raise ObservationError("passive_transport_unavailable")


def observe_target_file(
    target_path: Path, *, artifact_root: Path,
    observer: Callable[[ResolvedTarget], ObservationSummary] = observe_resolved_target,
) -> dict:
    """Return one sanitized protocol result; caller owns artifact publication."""
    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    commit, _dirty = git_identity()
    target = load_target(target_path, artifact_root=artifact_root)
    sensitive_values = SensitiveValueRegistry.from_target(
        {"target_alias": target.target_alias, "model_family": target.model_family,
         "evidence_correlation_key": target.evidence_correlation_key,
         "connection": target.connection}, target.address,
    )
    run_id = f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{commit}"
    correlation = target_correlation_sha256(target.evidence_correlation_key)
    try:
        summary = observer(target)
        expected_model = normalize_model_name(target.protocol, target.model_family) or target.model_family
        observed_model = summary.observed_model_family
        if not observed_model:
            raise ObservationError("model_identity_unavailable")
        if observed_model.casefold() != expected_model.casefold():
            raise ObservationError("model_family_mismatch")
        if not 0 < summary.freshness_seconds <= 30:
            raise ObservationError("sample_freshness_invalid")
        assertions = [
            {"id": "two_valid_samples", "status": "pass", "reason_code": "observed", "duration_ms": round((time.monotonic() - started) * 1000, 2)},
            {"id": "production_parser", "status": "pass", "reason_code": "mapped", "duration_ms": 0.0},
            {"id": "passive_transport", "status": "pass", "reason_code": "read_only", "duration_ms": 0.0},
            {"id": "model_consistency", "status": "pass", "reason_code": "observed", "duration_ms": 0.0},
        ]
        status = "pass"
        metrics = {
            "valid_sample_count": len(summary.samples),
            "freshness_seconds": summary.freshness_seconds,
            "model_family": observed_model, "firmware_version": summary.firmware_version,
            "api_version": summary.api_version, "capabilities": list(summary.capabilities),
            "reconnect_count": summary.reconnect_count,
            "target_correlation_sha256": correlation,
        }
    except ObservationError as exc:
        status = "blocked" if exc.reason_code in {
            "insufficient_samples", "passive_samples_unavailable",
            "passive_transport_unavailable",
        } else "fail"
        assertions = [{
            "id": "passive_observation", "status": status,
            "reason_code": exc.reason_code, "duration_ms": round((time.monotonic() - started) * 1000, 2),
        }]
        metrics = {
            "valid_sample_count": 0, "freshness_seconds": 0.0,
            "model_family": "unknown", "capabilities": [],
            "reconnect_count": 0, "target_correlation_sha256": correlation,
        }
    except Exception as exc:
        sensitive_values.redact(exc)
        status = "fail"
        assertions = [{
            "id": "passive_observation", "status": "fail",
            "reason_code": "unexpected_failure",
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        }]
        metrics = {
            "valid_sample_count": 0, "freshness_seconds": 0.0,
            "model_family": "unknown", "capabilities": [],
            "reconnect_count": 0, "target_correlation_sha256": correlation,
        }
    ended_at = datetime.now(timezone.utc)
    passed = sum(item["status"] == "pass" for item in assertions)
    failed = sum(item["status"] == "fail" for item in assertions)
    blocked = sum(item["status"] == "blocked" for item in assertions)
    return {
        "schema_version": 1, "run_id": run_id, "git_commit": commit,
        "mode": "observe", "protocol": target.protocol,
        "certification_level": "live_passive_observation", "status": status,
        "started_at": utc_iso(started_at), "ended_at": utc_iso(ended_at),
        "assertion_counts": {
            "executed": len(assertions), "passed": passed, "failed": failed,
            "blocked": blocked, "skipped": 0, "xfailed": 0,
        },
        "metrics": metrics, "assertions": assertions,
    }
