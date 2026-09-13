"""Read-only EDU live verification for TLS, sources, and hardware rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import ssl
import time
import urllib.request
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .common import utc_now, write_result
except ImportError:
    from common import utc_now, write_result

from ops.hardware_certification.evidence import (
    EvidenceError, EvidenceExpired, import_live_gate, verify_artifact,
)
from ops.hardware_certification.security import load_protected_json
from jsonschema import Draft202012Validator


IDENTITY_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "hardware_certification" / "schemas" / "identity-expectations.schema.json"
)


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
    """Retired compatibility entry point; it never reads credentials or connects."""
    return (
        "blocked",
        ["legacy one-shot hardware probe is retired; use ops.hardware_certification observe"],
        {"certification_level": "none", "endpoint_ignored": bool(endpoint), "gate_id_ignored": bool(gate_id)},
    )


def _identity_expectations(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    payload = load_protected_json(path)
    schema = json.loads(IDENTITY_SCHEMA.read_text(encoding="utf-8"))
    if list(Draft202012Validator(schema).iter_errors(payload)):
        raise EvidenceError("hardware identity expectations are invalid")
    return payload["protocols"]


def hardware_rows(
    run_id: str, run_dir: Path, evidence_dir: Path | None = None,
    identity_file: Path | None = None,
) -> None:
    """Import verified physical artifacts; never infer permission from environment."""
    commit = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], text=True,
        cwd=Path(__file__).resolve().parents[2],
    ).strip()
    try:
        expectations = _identity_expectations(identity_file)
    except (EvidenceError, OSError, ValueError):
        expectations = {}
        identity_input_invalid = True
    else:
        identity_input_invalid = False
    for protocol in ("bambu", "moonraker", "prusalink", "elegoo"):
        gate_id = f"hardware_{protocol}_live"
        started = time.perf_counter()
        artifact_dir = evidence_dir / protocol if evidence_dir else None
        if artifact_dir is None or not artifact_dir.is_dir():
            status = "blocked"
            findings = ["verified physical certification artifact is not configured"]
            metrics = {"certification_level": "none", "valid_sample_count": 0}
        else:
            try:
                expected_identity = expectations.get(protocol)
                if identity_input_invalid or expected_identity is None:
                    raise EvidenceError("current hardware identity expectation is required")
                manifest, results = verify_artifact(
                    artifact_dir, expected_mode="observe",
                    expected_protocol=protocol,
                    expected_model_family=expected_identity["model_family"],
                    expected_firmware_version=expected_identity["firmware_version"],
                    expected_api_version=expected_identity["api_version"],
                )
                protocol_result = results.get(protocol)
                if protocol_result is None:
                    raise EvidenceError("matching protocol result is missing")
                imported = import_live_gate(
                    manifest, expected_protocol=protocol, expected_commit=commit,
                    result=protocol_result,
                    source_manifest_sha256=hashlib.sha256((artifact_dir / "manifest.json").read_bytes()).hexdigest(),
                )
                status = imported["status"]
                metrics = imported["metrics"]
                findings = imported["findings"]
            except EvidenceExpired:
                status = "blocked"
                findings = ["physical certification artifact expired and must be recaptured"]
                metrics = {"certification_level": "expired", "valid_sample_count": 0}
            except EvidenceError as exc:
                status = "fail"
                findings = [f"physical certification artifact rejected ({type(exc).__name__})"]
                metrics = {"certification_level": "invalid", "valid_sample_count": 0}
        write_result(run_dir, result(run_id, gate_id, status, started, findings, metrics))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--hostname", default="odin.subsystem.app")
    parser.add_argument("--sources", type=Path, default=Path("ops/edu_readiness/legal_sources.json"))
    parser.add_argument("--hardware-evidence-dir", type=Path)
    parser.add_argument("--hardware-identity-file", type=Path)
    args = parser.parse_args()
    if bool(args.hardware_evidence_dir) != bool(args.hardware_identity_file):
        parser.error("hardware evidence import requires both --hardware-evidence-dir and --hardware-identity-file")
    args.run_dir.mkdir(parents=True, exist_ok=True)
    tls_ok = verify_tls(args.run_id, args.run_dir, args.hostname)
    sources_ok = verify_sources(args.run_id, args.run_dir, args.sources)
    hardware_rows(
        args.run_id, args.run_dir, args.hardware_evidence_dir,
        args.hardware_identity_file,
    )
    write_result(args.run_dir, result(
        args.run_id, "manual_legal_contract", "blocked", time.perf_counter(),
        ["school DPA/terms, breach notice, deletion certification, and W-9 acceptance require human approval"],
        {"decision_maker_acceptance_present": False},
    ))
    return 0 if tls_ok and sources_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
