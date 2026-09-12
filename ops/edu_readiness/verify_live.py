"""Read-only EDU live verification for TLS, sources, and hardware rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import ssl
import threading
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .common import utc_now, write_result
except ImportError:
    from common import utc_now, write_result

try:
    from .hardware_probe import PassiveMqttTransport, PassiveWebSocketTransport, ReadOnlyHttpTransport
except ImportError:
    from hardware_probe import PassiveMqttTransport, PassiveWebSocketTransport, ReadOnlyHttpTransport


def result(run_id: str, gate_id: str, status: str, started: float, findings: list[str], metrics: dict) -> dict:
    return {
        "schema_version": 1, "run_id": run_id, "gate_id": gate_id,
        "mandatory": True, "status": status, "started_at": utc_now(),
        "ended_at": utc_now(), "duration_seconds": round(time.perf_counter() - started, 3),
        "tool_versions": {"python_ssl": ssl.OPENSSL_VERSION}, "executed_count": 1,
        "skipped_count": 0, "xfailed_count": 0, "metrics": metrics,
        "findings": findings, "artifacts": [],
    }


def verify_tls(run_id: str, run_dir: Path, hostname: str) -> bool:
    started = time.perf_counter()
    findings: list[str] = []
    metrics = {"hostname": hostname, "normal_verification": True}
    try:
        context = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=hostname) as connection:
                cert = connection.getpeercert()
        expiry = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
        metrics.update({"expires_at": expiry.isoformat(), "days_remaining": round(days, 2)})
        if days < 30:
            findings.append("TLS certificate has fewer than 30 days remaining")
    except Exception as exc:
        findings.append(f"normal TLS verification failed: {type(exc).__name__}")
    write_result(run_dir, result(run_id, "tls_live", "fail" if findings else "pass", started, findings, metrics))
    return not findings


def verify_sources(run_id: str, run_dir: Path, manifest_path: Path) -> bool:
    started = time.perf_counter()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    findings = validate_source_manifest(manifest)
    observations = []
    for source in manifest["sources"]:
        try:
            request = urllib.request.Request(source["url"], headers={"User-Agent": "ODIN-EDU-Readiness/1"})
            with urllib.request.urlopen(request, timeout=20) as response:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- committed HTTPS URL and final authoritative domain are verified
                final_url = response.geturl()
                body = response.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
                status = response.status
            if urlsplit(final_url).hostname != source["authoritative_domain"]:
                findings.append(f"{source['id']}: redirect left authoritative domain")
            missing = [marker for marker in source["required_markers"] if marker not in body]
            if missing:
                findings.append(f"{source['id']}: {len(missing)} required markers changed or missing")
            observations.append({
                "id": source["id"],
                "status": status,
                "final_url": final_url,
                "accessed_at": utc_now(),
                "sha256": hashlib.sha256(body.encode()).hexdigest(),
            })
        except Exception as exc:
            findings.append(f"{source['id']}: fetch failed ({type(exc).__name__})")
    metrics = {"source_count": len(manifest["sources"]), "observations": observations}
    write_result(run_dir, result(run_id, "legal_sources_live", "blocked" if findings else "pass", started, findings, metrics))
    return not findings


def validate_source_manifest(manifest: dict, *, today: date | None = None) -> list[str]:
    """Reject missing, future-dated, or stale authoritative-source records."""
    findings: list[str] = []
    current = today or datetime.now(timezone.utc).date()
    maximum_age = manifest.get("maximum_age_days")
    if not isinstance(maximum_age, int) or maximum_age <= 0:
        return ["legal source maximum_age_days must be a positive integer"]
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        return ["legal source manifest must contain sources"]
    for source in sources:
        source_id = source.get("id", "unknown") if isinstance(source, dict) else "unknown"
        try:
            accessed = date.fromisoformat(source["accessed_at"])
        except (KeyError, TypeError, ValueError):
            findings.append(f"{source_id}: invalid accessed_at")
            continue
        age = (current - accessed).days
        if age < 0:
            findings.append(f"{source_id}: accessed_at is in the future")
        elif age > maximum_age:
            findings.append(f"{source_id}: source attestation is {age} days old")
    return findings


def validate_elegoo_passive_frame(frame: str) -> str:
    """Accept only unsolicited Elegoo SDCP status/notice telemetry frames."""
    try:
        payload = json.loads(frame)
    except (TypeError, ValueError) as exc:
        raise ValueError("Elegoo passive frame is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Elegoo passive frame must be a JSON object")
    topic = payload.get("Topic")
    if not isinstance(topic, str):
        raise ValueError("Elegoo passive frame is not unsolicited status or notice telemetry")
    allowed = {
        "status": "sdcp/status/",
        "notice": "sdcp/notice/",
    }
    for frame_type, marker in allowed.items():
        if topic.startswith(marker) and len(topic) > len(marker):
            return frame_type
    raise ValueError("Elegoo passive frame is not unsolicited status or notice telemetry")


def _probe_hardware(gate_id: str, endpoint: str) -> tuple[str, list[str], dict]:
    """Perform one configured passive/read-only protocol observation."""
    started = time.perf_counter()
    try:
        if gate_id == "hardware_moonraker_live":
            transport = ReadOnlyHttpTransport(endpoint, "moonraker")
            server = transport.get("/server/info")
            printer = transport.get("/printer/info")
            objects = transport.get("/printer/objects/list")
            transport.get("/printer/objects/query?print_stats")
            metrics = {
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "api_version_observed": bool(server.get("result")),
                "printer_info_observed": bool(printer.get("result")),
                "object_list_observed": bool(objects.get("result")),
            }
        elif gate_id == "hardware_prusalink_live":
            transport = ReadOnlyHttpTransport(endpoint, "prusalink")
            observations = [transport.get(path) for path in ("/api/version", "/api/v1/status", "/api/printer", "/api/job")]
            metrics = {
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "responses_observed": sum(isinstance(item, dict) for item in observations),
            }
        elif gate_id == "hardware_elegoo_live":
            import websocket

            url = endpoint if endpoint.startswith("ws://") else f"ws://{endpoint}:3030/websocket"  # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- Elegoo firmware certification protocol is LAN-only ws://
            socket_client = websocket.create_connection(url, timeout=10)
            transport = PassiveWebSocketTransport(socket_client)
            try:
                frame = transport.receive()
            finally:
                transport.close()
            frame_type = validate_elegoo_passive_frame(frame)
            metrics = {
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "passive_frame_observed": True,
                "passive_frame_type": frame_type,
            }
        elif gate_id == "hardware_bambu_live":
            import paho.mqtt.client as mqtt

            device = os.environ.get("EDU_BAMBU_CERT_DEVICE_TOKEN")
            access_code = os.environ.get("EDU_BAMBU_CERT_ACCESS_CODE")
            if not device or not access_code:
                return "blocked", ["Bambu passive topic token and access code are not configured"], {"endpoint_configured": True}
            observed = threading.Event()
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.username_pw_set("bblp", access_code)
            client.tls_set()
            client.on_message = lambda *_args: observed.set()
            transport = PassiveMqttTransport(client, device)
            try:
                transport.connect(endpoint, 8883)
                transport.subscribe(f"device/{device}/report")
                client.loop_start()
                if not observed.wait(10):
                    return "blocked", ["no passive Bambu telemetry received"], {"endpoint_configured": True}
            finally:
                client.loop_stop()
                transport.disconnect()
            metrics = {"latency_ms": round((time.perf_counter() - started) * 1000, 2), "passive_report_observed": True}
        else:
            raise ValueError("unknown hardware gate")
        return "pass", [], metrics
    except (TimeoutError, OSError, ConnectionError) as exc:
        return "blocked", [f"passive observation unavailable ({type(exc).__name__})"], {"endpoint_configured": True}
    except Exception as exc:
        return "fail", [f"passive observation failed ({type(exc).__name__})"], {"endpoint_configured": True}


def hardware_rows(run_id: str, run_dir: Path) -> None:
    env_names = {
        "hardware_bambu_live": "EDU_BAMBU_CERT_HOST",
        "hardware_moonraker_live": "EDU_MOONRAKER_CERT_URL",
        "hardware_prusalink_live": "EDU_PRUSALINK_CERT_URL",
        "hardware_elegoo_live": "EDU_ELEGOO_CERT_HOST",
    }
    for gate_id, env_name in env_names.items():
        started = time.perf_counter()
        endpoint = os.environ.get(env_name)
        if endpoint:
            status, findings, metrics = _probe_hardware(gate_id, endpoint)
        else:
            status, findings, metrics = "blocked", [f"{env_name} is not configured"], {"endpoint_configured": False}
        write_result(run_dir, result(run_id, gate_id, status, started, findings, metrics))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--hostname", default="odin.subsystem.app")
    parser.add_argument("--sources", type=Path, default=Path("ops/edu_readiness/legal_sources.json"))
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    tls_ok = verify_tls(args.run_id, args.run_dir, args.hostname)
    sources_ok = verify_sources(args.run_id, args.run_dir, args.sources)
    hardware_rows(args.run_id, args.run_dir)
    write_result(args.run_dir, result(
        args.run_id, "manual_legal_contract", "blocked", time.perf_counter(),
        ["school DPA/terms, breach notice, deletion certification, and W-9 acceptance require human approval"],
        {"decision_maker_acceptance_present": False},
    ))
    return 0 if tls_ok and sources_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
