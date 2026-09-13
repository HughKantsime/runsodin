"""Unlicensed smoke and license-gated full lifecycle certification."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from ops.release_gate.policy import scan_text_for_secrets

from .errors import SandboxError
from .report import render_report
from .runtime import BROKER_IMAGE, DEFAULT_STATE_ROOT, ROOT, SandboxRuntime
from .secrets import sanitized_text
from .state import LifecycleState, Phase, load_state


def _run_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower()
    return f"{prefix}-{stamp}-{secrets.token_hex(3)}"


def _phase(
    phases: list[dict[str, object]],
    name: str,
    status: str,
    detail: str,
    started: float,
) -> None:
    phases.append(
        {
            "name": name,
            "status": status,
            "detail": detail,
            "duration_seconds": round(max(0.0, time.monotonic() - started), 3),
        }
    )


def _junit(path: Path, phases: list[dict[str, object]]) -> None:
    nonpassing = {"FAIL", "BLOCKED_EXTERNAL"}
    suite = ET.Element(
        "testsuite",
        tests=str(len(phases)),
        failures=str(sum(item.get("status") in nonpassing for item in phases)),
        errors="0",
        skipped="0",
    )
    for item in phases:
        case = ET.SubElement(suite, "testcase", name=str(item.get("name", "unknown")))
        if item.get("status") in nonpassing:
            failure = ET.SubElement(case, "failure", message=str(item.get("status")))
            failure.text = str(item.get("detail", ""))
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def _sanitize_value(value: object, known_secrets: list[str]) -> object:
    if isinstance(value, str):
        return sanitized_text(value, known_secrets)
    if isinstance(value, list):
        return [_sanitize_value(item, known_secrets) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item, known_secrets) for key, item in value.items()}
    return value


def _write_manifest(directory: Path, manifest: dict[str, object], known_secrets: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    safe_manifest = _sanitize_value(manifest, known_secrets)
    if not isinstance(safe_manifest, dict):
        raise SandboxError("certification manifest must be an object")
    path = directory / "manifest.json"
    path.write_text(json.dumps(safe_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    phases = list(safe_manifest.get("phases", []))
    (directory / "phase-log.json").write_text(
        json.dumps({"schema_version": 1, "phases": phases}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _junit(directory / "junit.xml", phases)
    render_report(safe_manifest, directory / "index.html")
    findings: list[str] = []
    for artifact in directory.rglob("*"):
        if artifact.is_file() and artifact.suffix in {".json", ".xml", ".html", ".log", ".txt"}:
            labels = scan_text_for_secrets(
                artifact.read_text(encoding="utf-8", errors="replace"), known_secrets
            )
            findings.extend(f"{artifact.name}: {label}" for label in labels)
    if findings:
        raise SandboxError("certification artifacts failed secret scan")


def _known_secrets(runtime: SandboxRuntime, sandbox_id: str) -> list[str]:
    paths = runtime._paths(sandbox_id)
    values: list[str] = []
    if not paths.secrets.is_dir():
        return values

    sensitive_fields = {
        "key", "nonce", "device_pubkey", "bootstrap_signature", "signature",
        "payload", "licensee", "email",
    }

    def collect(value: object, field: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                collect(item, str(key))
        elif isinstance(value, list):
            for item in value:
                collect(item, field)
        elif field in sensitive_fields and isinstance(value, str) and len(value) >= 8:
            values.append(value)

    for path in sorted(paths.secrets.iterdir()):
        if not path.is_file() or path.is_symlink():
            continue
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(content) >= 8:
            values.append(content)
        try:
            collect(json.loads(content))
        except json.JSONDecodeError:
            parts = content.split(".")
            if len(parts) == 2:
                values.extend(part for part in parts if len(part) >= 8)
                try:
                    payload = json.loads(base64.urlsafe_b64decode(parts[0] + "=="))
                    collect(payload)
                except (ValueError, json.JSONDecodeError):
                    pass
    return sorted(set(values))


def _source_privacy_preflight() -> dict[str, object]:
    fixture = ROOT / "tests/fixtures/telemetry/bambu-x1c-ams-swap.demo.jsonl"
    seed = ROOT / "backend/scripts/seed_edu_sandbox.py"
    digest = hashlib.sha256()
    for path in (fixture, seed):
        if not path.is_file() or path.is_symlink():
            raise SandboxError(f"required EDU fixture is missing: {path.name}")
        content = path.read_bytes()
        digest.update(path.name.encode("utf-8") + b"\0" + content)
        if path == fixture and scan_text_for_secrets(content.decode("utf-8", errors="replace"), []):
            raise SandboxError("telemetry fixture failed privacy scan")
    source = seed.read_text(encoding="utf-8")
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", source)
    if not emails or any(not email.endswith(".example.invalid") for email in emails):
        raise SandboxError("EDU seed contains a non-fictional email domain")
    return {"files_scanned": 2, "fixture_set_sha256": digest.hexdigest()}


def _status_evidence(value: dict[str, object]) -> dict[str, object]:
    return {
        key: value.get(key)
        for key in (
            "phase",
            "observed_status",
            "candidate_image_id",
            "broker_image_id",
            "broker_digest",
            "observed_license_expires_at",
            "observed_license_expired",
            "lease_expires_at",
        )
        if key in value
    }


def _activation_request_digest(runtime: SandboxRuntime, sandbox_id: str) -> str:
    receipt = runtime._paths(sandbox_id).public / "activation-request-receipt.json"
    if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_mode & 0o077:
        raise SandboxError("activation-request receipt is missing or has unsafe permissions")
    value = json.loads(receipt.read_text(encoding="utf-8"))
    digest = value.get("activation_request_sha256") if isinstance(value, dict) else None
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SandboxError("activation-request receipt digest is invalid")
    return digest


def _assert_complete_pass_evidence(evidence: dict[str, object]) -> None:
    request_digest = evidence.get("activation_request_sha256")
    privacy = evidence.get("privacy")
    status_before = evidence.get("status_before")
    readiness = evidence.get("active_readiness")
    graph = evidence.get("school_graph")
    reset = evidence.get("reset")
    expiry = evidence.get("expiry")
    purge = evidence.get("purge")
    if not isinstance(request_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", request_digest):
        raise SandboxError("certification lacks activation-request proof")
    if not isinstance(privacy, dict) or privacy.get("files_scanned") != 2:
        raise SandboxError("certification lacks source privacy proof")
    if (
        not isinstance(status_before, dict)
        or status_before.get("phase") != "ACTIVE"
        or status_before.get("observed_status") != "READY"
        or status_before.get("observed_license_expired") is not False
    ):
        raise SandboxError("certification did not begin from a ready unexpired sandbox")
    if (
        not isinstance(readiness, dict)
        or not readiness.get("heartbeat")
        or not readiness.get("images")
        or not readiness.get("network_isolation")
    ):
        raise SandboxError("certification lacks retained runtime readiness proof")
    if not isinstance(graph, dict) or graph.get("printer_count") != 4 or not graph.get("student_write_denied"):
        raise SandboxError("certification lacks retained school graph/RBAC proof")
    if (
        not isinstance(reset, dict)
        or reset.get("identity_before") != reset.get("identity_after")
        or not isinstance(reset.get("verification"), dict)
    ):
        raise SandboxError("certification lacks reset identity preservation proof")
    after = expiry.get("after") if isinstance(expiry, dict) else None
    if not isinstance(after, dict) or after.get("observed_status") != "EXPIRED":
        raise SandboxError("certification lacks reconciled expiry proof")
    absence = purge.get("absence") if isinstance(purge, dict) else None
    if not isinstance(absence, dict) or not all(value is True for value in absence.values()):
        raise SandboxError("certification lacks complete purge absence proof")


def _assert_prepared_blocked_evidence(
    state: LifecycleState,
    observed: dict[str, object],
    topology: object,
    current: object,
) -> None:
    if (
        observed.get("phase") != Phase.PREPARED.value
        or observed.get("observed_status") != "STOPPED"
        or observed.get("candidate_image_id") != state.candidate_image_id
    ):
        raise SandboxError("blocked certification requires a healthy PREPARED / STOPPED sandbox")
    if not isinstance(topology, dict) or any(
        topology.get(key) is not True
        for key in (
            "sandbox_network_internal",
            "application_has_no_edge_route",
            "proxy_is_only_edge_member",
            "proxy_loopback_only",
        )
    ):
        raise SandboxError("blocked certification lacks prepared network-isolation proof")
    members = topology.get("network_members")
    expected_app = f"{state.compose_project}-prepare"
    expected_proxy = f"{state.compose_project}-prepare-proxy"
    if (
        not isinstance(members, dict)
        or members.get("sandbox") != sorted([expected_app, expected_proxy])
        or members.get("edge") != [expected_proxy]
    ):
        raise SandboxError("blocked certification has inconsistent prepared network membership")
    current_members = current.get("network_members") if isinstance(current, dict) else None
    if (
        not isinstance(current, dict)
        or current.get("identity_matches") is not True
        or current.get("owned_resources_match") is not True
        or current.get("owned_containers_absent") is not True
        or current.get("networks_quiesced") is not True
        or current_members != {"sandbox": [], "edge": []}
    ):
        raise SandboxError("blocked certification lacks current prepared resource proof")


def run_unlicensed_smoke(
    sandbox_id: str,
    *,
    state_root: Path,
    artifact_root: Path,
    allow_dirty: bool,
) -> tuple[int, Path]:
    runtime = SandboxRuntime(state_root)
    run_started = time.monotonic()
    run_id = _run_id("smoke")
    directory = artifact_root / run_id
    phases: list[dict[str, object]] = []
    known: list[str] = []
    state = None
    error: BaseException | None = None
    evidence: dict[str, object] = {}
    try:
        phase_started = time.monotonic()
        privacy = _source_privacy_preflight()
        evidence["privacy"] = privacy
        _phase(phases, "privacy-preflight", "PASS", f"files={privacy['files_scanned']}", phase_started)
        phase_started = time.monotonic()
        state = runtime.prepare(sandbox_id, allow_dirty=allow_dirty)
        known = _known_secrets(runtime, sandbox_id)
        _phase(phases, "prepare", "PASS", "exact candidate prepared and stopped", phase_started)
        phase_started = time.monotonic()
        observed = runtime.status(sandbox_id)
        if observed.get("phase") != "PREPARED" or observed.get("observed_status") != "STOPPED":
            raise SandboxError("prepared status evidence is inconsistent")
        _phase(phases, "status", "PASS", "PREPARED / STOPPED independently observed", phase_started)
        evidence["prepared_status"] = _status_evidence(observed)
        evidence["prepared_current"] = observed.get("prepared_current", {})
        evidence["prepared_identity"] = {
            "installation_id_sha256": hashlib.sha256(state.installation_id.encode()).hexdigest(),
            "device_public_key_sha256": state.device_public_key_sha256,
        }
        evidence["prepared_topology"] = state.verification.get(
            "prepare_network_isolation", {}
        )
    except BaseException as exc:
        error = exc
        try:
            state = load_state(runtime._paths(sandbox_id).state)
            known = _known_secrets(runtime, sandbox_id)
        except Exception:
            pass
        _phase(phases, "smoke", "FAIL", str(exc)[:500], run_started)
    finally:
        cleanup_started = time.monotonic()
        try:
            paths = runtime._paths(sandbox_id)
            tombstone = paths.root / ".tombstones" / f"{sandbox_id}.json"
            if (
                paths.state.exists()
                or paths.directory.exists()
                or paths.directory.is_symlink()
                or tombstone.is_file()
            ):
                evidence["purge"] = runtime.purge(sandbox_id, confirm=sandbox_id)
            _phase(phases, "purge", "PASS", "owned Docker and file resources absent", cleanup_started)
        except BaseException as cleanup_exc:
            error = error or cleanup_exc
            _phase(phases, "purge", "FAIL", str(cleanup_exc)[:500], cleanup_started)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "sandbox_id": sandbox_id,
        "status": "FAIL" if error else "PASS",
        "source_commit": state.source_commit if state else "unknown",
        "source_dirty": state.source_dirty if state else True,
        "candidate_image_id": state.candidate_image_id if state else "unknown",
        "broker_image_id": state.broker_image_id if state else "unknown",
        "broker_digest": state.broker_digest if state else BROKER_IMAGE.split("@sha256:", 1)[-1],
        "duration_seconds": round(time.monotonic() - run_started, 3),
        "cleanup_status": "FAIL" if any(item["name"] == "purge" and item["status"] != "PASS" for item in phases) else "PASS",
        "evidence": evidence,
        "phases": phases,
        "summary": "Unlicensed exact-image prepare/status/purge smoke completed." if not error else "Unlicensed lifecycle smoke failed.",
    }
    _write_manifest(directory, manifest, known)
    return (1 if error else 0), directory / "index.html"


def certify_existing(
    sandbox_id: str,
    *,
    confirm: str,
    state_root: Path,
    artifact_root: Path,
) -> tuple[int, Path]:
    if confirm != sandbox_id:
        raise SandboxError("certify confirmation must exactly match the sandbox ID")
    runtime = SandboxRuntime(state_root)
    run_started = time.monotonic()
    state = load_state(runtime._paths(sandbox_id).state)
    known = _known_secrets(runtime, sandbox_id)
    run_id = _run_id("certify")
    directory = artifact_root / run_id
    phases: list[dict[str, object]] = []
    evidence: dict[str, object] = {}
    status = "FAIL"
    summary = "Full lifecycle certification failed."
    try:
        phase_started = time.monotonic()
        privacy = _source_privacy_preflight()
        evidence["privacy"] = privacy
        _phase(phases, "privacy-preflight", "PASS", f"files={privacy['files_scanned']}", phase_started)
        if state.phase == Phase.PREPARED:
            phase_started = time.monotonic()
            prepared_status = runtime.status(sandbox_id)
            evidence["prepared_status"] = _status_evidence(prepared_status)
            prepared_topology = state.verification.get(
                "prepare_network_isolation", {}
            )
            prepared_current = prepared_status.get("prepared_current")
            evidence["prepared_topology"] = prepared_topology
            evidence["prepared_current"] = prepared_current
            _assert_prepared_blocked_evidence(
                state,
                prepared_status,
                prepared_topology,
                prepared_current,
            )
            receipt = runtime._paths(sandbox_id).public / "activation-request-receipt.json"
            if receipt.is_file() and not receipt.is_symlink():
                evidence["activation_request_sha256"] = _activation_request_digest(runtime, sandbox_id)
            _phase(phases, "prepared-candidate", "PASS", "installation identity and exact candidate are prepared", phase_started)
            _phase(phases, "activation", "BLOCKED_EXTERNAL", "normally signed installation-bound Education license required", time.monotonic())
            status = "BLOCKED_EXTERNAL"
            summary = "Code-controlled preparation passed; activation remains externally blocked by license issuance."
        elif state.phase == Phase.ACTIVE:
            phase_started = time.monotonic()
            evidence["activation_request_sha256"] = _activation_request_digest(runtime, sandbox_id)
            status_before = runtime.status(sandbox_id)
            evidence["status_before"] = _status_evidence(status_before)
            if (
                status_before.get("phase") != "ACTIVE"
                or status_before.get("observed_status") != "READY"
                or status_before.get("observed_license_expired") is not False
            ):
                raise SandboxError("full certification requires an initially ready unexpired ACTIVE sandbox")
            evidence["active_readiness"] = runtime._assert_runtime_evidence(state)
            evidence["school_graph"] = runtime._verify_personas_and_graph(state)
            _phase(phases, "active-readiness", "PASS", "license, exact image, personas, RBAC, tenant graph, and simulator evidence passed", phase_started)
            phase_started = time.monotonic()
            identity_before = {
                "installation_id_sha256": hashlib.sha256(state.installation_id.encode()).hexdigest(),
                "device_public_key_sha256": state.device_public_key_sha256,
                "license_sha256": state.license_sha256,
            }
            state = runtime.reset(sandbox_id, confirm=sandbox_id)
            identity_after = {
                "installation_id_sha256": hashlib.sha256(state.installation_id.encode()).hexdigest(),
                "device_public_key_sha256": state.device_public_key_sha256,
                "license_sha256": state.license_sha256,
            }
            evidence["reset"] = {
                "identity_before": identity_before,
                "identity_after": identity_after,
                "generation": state.reset_generation,
                "verification": state.verification,
            }
            _phase(phases, "reset", "PASS", f"identity preserved; mutation sentinel removed; generation {state.reset_generation}", phase_started)
            phase_started = time.monotonic()
            runtime.expire(sandbox_id, confirm=sandbox_id)
            expiry_before = _status_evidence(runtime.status(sandbox_id))
            runtime.reconcile(sandbox_id)
            expiry_after = _status_evidence(runtime.status(sandbox_id))
            evidence["expiry"] = {"before": expiry_before, "after": expiry_after}
            _phase(phases, "expiry", "PASS", "application and simulator stopped after lease expiry", phase_started)
            phase_started = time.monotonic()
            evidence["purge"] = runtime.purge(sandbox_id, confirm=sandbox_id)
            _phase(phases, "purge", "PASS", "owned resources absent", phase_started)
            _assert_complete_pass_evidence(evidence)
            status = "PASS"
            summary = "Full signed-license Education sandbox lifecycle passed."
        else:
            raise SandboxError(f"certify requires PREPARED or ACTIVE phase, got {state.phase.value}")
    except BaseException as exc:
        _phase(phases, "certification", "FAIL", type(exc).__name__, run_started)
        status = "FAIL"
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "sandbox_id": sandbox_id,
        "status": status,
        "source_commit": state.source_commit,
        "source_dirty": state.source_dirty,
        "candidate_image_id": state.candidate_image_id,
        "broker_image_id": state.broker_image_id,
        "broker_digest": state.broker_digest,
        "license_sha256": state.license_sha256,
        "license_tier": state.license_tier,
        "license_expires_at": state.license_expires_at,
        "lease_expires_at": state.lease_expires_at,
        "reset_generation": state.reset_generation,
        "duration_seconds": round(time.monotonic() - run_started, 3),
        "cleanup_status": "PASS" if any(item["name"] == "purge" and item["status"] == "PASS" for item in phases) else "NOT_RUN",
        "evidence": evidence,
        "phases": phases,
        "summary": summary,
    }
    _write_manifest(directory, manifest, known)
    # A missing issuer-provided license is an expected external dependency, but
    # it is not a passing certification.  Preserve a distinct exit code so CI
    # and operators cannot mistake the blocked artifact for release evidence.
    return (0 if status == "PASS" else 3 if status == "BLOCKED_EXTERNAL" else 1), directory / "index.html"


def smoke_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run unlicensed exact-image EDU sandbox smoke")
    parser.add_argument("--sandbox-id", default=_run_id("ci")[:47].rstrip("-"))
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=ROOT / "artifacts/edu-sandbox")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    def terminate(_signum, _frame):
        raise KeyboardInterrupt("termination requested; running bounded cleanup")

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        code, report = run_unlicensed_smoke(
            args.sandbox_id,
            state_root=args.state_root,
            artifact_root=args.artifact_root,
            allow_dirty=args.allow_dirty,
        )
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(f"ODIN EDU sandbox smoke {'PASS' if code == 0 else 'FAIL'}: {report}")
    return code


if __name__ == "__main__":
    raise SystemExit(smoke_main())
