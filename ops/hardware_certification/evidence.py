"""Verify certification artifacts and convert physical evidence to EDU rows."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from defusedxml import ElementTree

from ops.edu_readiness.artifact_scan import scan_tree
from ops.hardware_certification.artifact import git_full_commit, git_identity
from ops.hardware_certification.implementation import (
    certification_fixture_sha256, certification_implementation_sha256,
    certification_simulator_sha256,
)
from ops.hardware_certification.security import canonical_json_sha256
from ops.hardware_certification.replay_contracts import REPLAY_ASSERTION_CASES


class EvidenceError(ValueError):
    pass


class EvidenceExpired(EvidenceError):
    """Verified evidence no longer describes the current code or freshness window."""


PROTOCOLS = {"bambu", "elegoo", "moonraker", "prusalink"}
SCHEMAS = Path(__file__).with_name("schemas")


def _load_object(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("evidence JSON is unreadable") from exc
    if not isinstance(loaded, dict):
        raise EvidenceError("evidence JSON root must be an object")
    return loaded


def _validate(payload: dict[str, Any], schema_name: str) -> None:
    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    if list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload)):
        raise EvidenceError(f"evidence does not match {schema_name}")


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise EvidenceError("evidence timestamp is invalid") from exc


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_private(path: Path, expected_mode: int) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise EvidenceError("evidence file metadata is unavailable") from exc
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise EvidenceError("evidence ownership or mode is invalid")


def strict_junit_counts(path: Path) -> dict[str, int]:
    """Reconcile suite attributes with concrete testcase outcome elements."""
    try:
        root = ElementTree.parse(path).getroot()
        if root.tag == "testsuite":
            suites = [root]
        elif root.tag == "testsuites":
            suites = list(root.findall("testsuite"))
        else:
            raise ValueError
        if not suites:
            raise ValueError
        if any("hostname" in element.attrib for element in root.iter()):
            raise EvidenceError("evidence JUnit retains forbidden host identity")
        counts = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
        xfailed = 0
        for suite in suites:
            cases = list(suite.findall("testcase"))
            actual = {
                "tests": len(cases),
                "failures": sum(bool(case.findall("failure")) for case in cases),
                "errors": sum(bool(case.findall("error")) for case in cases),
                "skipped": sum(bool(case.findall("skipped")) for case in cases),
            }
            for case in cases:
                outcomes = case.findall("failure") + case.findall("error") + case.findall("skipped")
                if len(outcomes) > 1:
                    raise ValueError
                for skipped in case.findall("skipped"):
                    if "xfail" in (
                        skipped.attrib.get("type", "") + skipped.attrib.get("message", "")
                    ).lower():
                        xfailed += 1
            declared = {
                name: int(suite.attrib.get(name, "0"))
                for name in ("tests", "failures", "errors", "skipped")
            }
            if any(value < 0 for value in declared.values()) or declared != actual:
                raise ValueError
            for name in counts:
                counts[name] += actual[name]
    except EvidenceError:
        raise
    except (OSError, ValueError, TypeError, ElementTree.ParseError) as exc:
        raise EvidenceError("evidence JUnit is malformed") from exc
    counts["xfailed"] = xfailed
    return counts


def strict_junit_outcomes(path: Path) -> dict[tuple[str, str], str]:
    """Return unique testcase identities with normalized, tamper-checkable outcomes."""
    strict_junit_counts(path)
    try:
        root = ElementTree.parse(path).getroot()
        cases: dict[tuple[str, str], str] = {}
        for case in root.iter("testcase"):
            identity = (case.attrib.get("classname", ""), case.attrib.get("name", ""))
            if not all(identity) or identity in cases:
                raise ValueError
            failures = case.findall("failure")
            errors = case.findall("error")
            skipped = case.findall("skipped")
            if failures:
                outcome = "blocked" if failures[0].attrib.get("type") == "blocked" else "fail"
            elif errors:
                outcome = "error"
            elif skipped:
                marker = skipped[0].attrib.get("type", "") + skipped[0].attrib.get("message", "")
                outcome = "xfailed" if "xfail" in marker.lower() else "skipped"
            else:
                outcome = "pass"
            cases[identity] = outcome
        if not cases:
            raise ValueError
        return cases
    except (OSError, ValueError, TypeError, ElementTree.ParseError) as exc:
        raise EvidenceError("evidence JUnit testcase identities are malformed") from exc


def verify_artifact(
    run_dir: Path, *, expected_mode: str | None = None,
    expected_protocol: str | None = None,
    expected_model_family: str | None = None,
    expected_firmware_version: str | None = None,
    expected_api_version: str | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Fail closed on schema, hashes, freshness, sanitation, or claim drift."""
    unresolved_root = Path(run_dir)
    if unresolved_root.is_symlink():
        raise EvidenceError("evidence directory is invalid")
    root = unresolved_root.resolve()
    if not root.is_dir():
        raise EvidenceError("evidence directory is invalid")
    _assert_private(root, 0o700)
    _assert_private(root / "manifest.json", 0o600)
    manifest = _load_object(root / "manifest.json")
    _validate(manifest, "manifest.schema.json")
    current_short_commit, _current_dirty = git_identity()
    current_commits = {current_short_commit, git_full_commit()}
    if manifest["git_commit"] not in current_commits:
        raise EvidenceExpired("evidence commit identity mismatch")
    if manifest["git_dirty"] is not False:
        raise EvidenceError("evidence dirty-state identity mismatch")
    if manifest["implementation_sha256"] != certification_implementation_sha256():
        raise EvidenceExpired("evidence implementation identity mismatch")
    if manifest["mode"] == "replay" and manifest.get("fixture_sha256") != certification_fixture_sha256():
        raise EvidenceExpired("evidence replay fixture identity mismatch")
    if manifest["mode"] == "replay" and manifest.get("simulator_sha256") != certification_simulator_sha256():
        raise EvidenceExpired("evidence simulator identity mismatch")
    if expected_mode and manifest["mode"] != expected_mode:
        raise EvidenceError("evidence mode mismatch")
    if expected_protocol and manifest["protocol"] != expected_protocol:
        raise EvidenceError("evidence protocol mismatch")
    if manifest.get("sanitizer_passed") is not True:
        raise EvidenceError("evidence sanitizer did not pass")
    started = _timestamp(manifest["started_at"])
    ended = _timestamp(manifest["ended_at"])
    elapsed = (ended - started).total_seconds()
    if elapsed < 0 or abs(manifest["duration_seconds"] - elapsed) > 0.001:
        raise EvidenceError("evidence duration or timestamp ordering is invalid")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if ended > current + timedelta(minutes=5):
        raise EvidenceError("evidence is future-dated")
    if current - ended > timedelta(days=30):
        raise EvidenceExpired("evidence is stale")
    expected_files = set(manifest["files"])
    required_files = {"index.html", "junit.xml", "diagnostics.json"}
    required_files |= (
        {f"{protocol}.json" for protocol in PROTOCOLS}
        if manifest["protocol"] == "all"
        else {f"{manifest['protocol']}.json"}
    )
    if expected_files != required_files:
        raise EvidenceError("evidence manifest file inventory is not format-exact")
    expected_results = sorted(name for name in required_files if name.endswith(".json") and name != "diagnostics.json")
    if manifest["result_references"] != expected_results:
        raise EvidenceError("evidence result references are incoherent")
    entries = list(root.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise EvidenceError("evidence file inventory contains a non-file entry")
    actual_files = {path.name for path in entries if path.name != "manifest.json"}
    if actual_files != expected_files:
        raise EvidenceError("evidence file inventory mismatch")
    results: dict[str, dict[str, Any]] = {}
    diagnostics: dict[str, Any] | None = None
    for name, expected_hash in manifest["files"].items():
        path = root / name
        if path.is_symlink() or not path.is_file() or _sha(path) != expected_hash:
            raise EvidenceError("evidence file hash mismatch")
        _assert_private(path, 0o600)
        if name == "diagnostics.json":
            diagnostics = _load_object(path)
            _validate(diagnostics, "diagnostics.schema.json")
        elif name.endswith(".json"):
            if name.removesuffix(".json") not in PROTOCOLS:
                raise EvidenceError("evidence JSON filename is not allowlisted")
            result = _load_object(path)
            _validate(result, "protocol-result.schema.json")
            for assertion in result["assertions"]:
                _validate(assertion, "assertion-result.schema.json")
            if result["run_id"] != manifest["run_id"] or result["git_commit"] != manifest["git_commit"] or result["mode"] != manifest["mode"]:
                raise EvidenceError("evidence result coherence mismatch")
            result_started = _timestamp(result["started_at"])
            result_ended = _timestamp(result["ended_at"])
            if result_started > result_ended or result_started < started or result_ended > ended:
                raise EvidenceError("evidence result timestamp ordering is invalid")
            if result["protocol"] != manifest["protocol"] and manifest["protocol"] != "all":
                raise EvidenceError("evidence protocol coherence mismatch")
            if manifest["protocol"] != "all" and result["status"] != manifest["status"]:
                raise EvidenceError("evidence status coherence mismatch")
            if manifest["mode"] == "exercise":
                scope = result.get("authorization_scope")
                if not isinstance(scope, dict) or scope != manifest.get("authorization_scope"):
                    raise EvidenceError("active authorization scope projection is incoherent")
                scope_hash = canonical_json_sha256(scope)
                if (
                    result.get("authorization_scope_sha256") != scope_hash
                    or manifest.get("authorization_scope_sha256") != scope_hash
                ):
                    raise EvidenceError("active authorization scope hash is incoherent")
                if (
                    result.get("requested_actions") != manifest.get("requested_actions")
                    or result.get("executed_actions") != manifest.get("executed_actions")
                    or result.get("requested_actions") != scope.get("actions")
                    or result.get("run_id") != scope.get("run_id")
                    or result.get("protocol") != scope.get("protocol")
                    or result.get("metrics", {}).get("model_family") != scope.get("model_family")
                    or result.get("metrics", {}).get("target_correlation_sha256")
                    != scope.get("target_correlation_sha256")
                ):
                    raise EvidenceError("active action evidence is incoherent")
                requested = result["requested_actions"]
                executed = result["executed_actions"]
                if executed != requested[:len(executed)]:
                    raise EvidenceError("active actions are not an authorized prefix")
                expected_ids = [f"action_{action}" for action in executed]
                assertion_ids = [item["id"] for item in result["assertions"]]
                terminal_assertions = result["assertions"][len(expected_ids):]
                if assertion_ids[:len(expected_ids)] != expected_ids or (
                    terminal_assertions
                    and terminal_assertions != [{
                        "id": "active_failure", "status": "fail",
                        "reason_code": "unexpected_failure", "duration_ms": 0.0,
                    }]
                ) or len(terminal_assertions) > 1:
                    raise EvidenceError("active assertions do not match executed actions")
                all_passed = all(item["status"] == "pass" for item in result["assertions"])
                expected_status = "pass" if executed == requested and all_passed else "fail"
                if result["status"] != expected_status:
                    raise EvidenceError("active aggregate status is incoherent")
            elif any(
                name in manifest or name in result
                for name in (
                    "authorization_scope", "authorization_scope_sha256",
                    "requested_actions", "executed_actions",
                )
            ):
                raise EvidenceError("authorization scope hash is permitted only for active evidence")
            counts = result["assertion_counts"]
            if counts["executed"] != counts["passed"] + counts["failed"] + counts["blocked"]:
                raise EvidenceError("evidence assertion counts are incoherent")
            actual = {
                value: sum(item["status"] == value for item in result["assertions"])
                for value in ("pass", "fail", "blocked")
            }
            count_names = {"pass": "passed", "fail": "failed", "blocked": "blocked"}
            if len(result["assertions"]) != counts["executed"] or any(
                actual[value] != counts[count_names[value]] for value in actual
            ):
                raise EvidenceError("evidence assertion details are incoherent")
            expected_result_status = (
                "fail" if counts["failed"] else
                "blocked" if counts["blocked"] else
                "pass"
            )
            if result["status"] != expected_result_status:
                raise EvidenceError("evidence result aggregate status is incoherent")
            if result["protocol"] in results:
                raise EvidenceError("evidence contains duplicate protocol results")
            results[result["protocol"]] = result
    if manifest["protocol"] == "all" and set(results) != PROTOCOLS:
        raise EvidenceError("evidence does not contain all protocol results")
    if manifest["protocol"] != "all" and set(results) != {manifest["protocol"]}:
        raise EvidenceError("evidence protocol result set is incoherent")
    expected_identity = {
        "model_family": expected_model_family,
        "firmware_version": expected_firmware_version,
        "api_version": expected_api_version,
    }
    if any(value is not None for value in expected_identity.values()):
        if len(results) != 1:
            raise EvidenceError("device identity expectations require one protocol result")
        metrics = next(iter(results.values()))["metrics"]
        for name, expected in expected_identity.items():
            if expected is not None and metrics.get(name, "unknown") != expected:
                raise EvidenceExpired(f"evidence {name} identity mismatch")
    expected_diagnostic_count = (
        manifest["junit"]["tests"] if manifest["protocol"] == "all"
        else next(iter(results.values()))["assertion_counts"]["executed"]
    )
    if diagnostics is None or diagnostics["assertion_count"] != expected_diagnostic_count:
        raise EvidenceError("evidence diagnostics are incoherent")
    junit = strict_junit_counts(root / "junit.xml")
    junit_outcomes = strict_junit_outcomes(root / "junit.xml")
    if junit != manifest["junit"]:
        raise EvidenceError("evidence JUnit counts are incoherent")
    if manifest["mode"] == "replay":
        for protocol, result in results.items():
            expected_cases = REPLAY_ASSERTION_CASES[protocol]
            if set(expected_cases) != {item["id"] for item in result["assertions"]}:
                raise EvidenceError("replay assertion identity set is incoherent")
            for assertion in result["assertions"]:
                outcome = junit_outcomes.get(expected_cases[assertion["id"]])
                normalized = "pass" if outcome == "pass" else "fail"
                if outcome is None or assertion["status"] != normalized:
                    raise EvidenceError("replay assertion JUnit outcome is incoherent")
    else:
        result = next(iter(results.values()))
        expected_outcomes = {
            (f"hardware_certification.{result['protocol']}", assertion["id"]): assertion["status"]
            for assertion in result["assertions"]
        }
        if junit_outcomes != expected_outcomes:
            raise EvidenceError("live assertion JUnit identity or outcome is incoherent")
    if manifest["protocol"] == "all":
        aggregate_status = "pass" if (
            all(item["status"] == "pass" for item in results.values())
            and not any(junit[name] for name in ("failures", "errors", "skipped", "xfailed"))
        ) else "fail"
        if manifest["status"] != aggregate_status:
            raise EvidenceError("evidence aggregate status is incoherent")
    if manifest["status"] == "pass" and any(
        junit[name] for name in ("failures", "errors", "skipped", "xfailed")
    ):
        raise EvidenceError("passing evidence contains non-passing JUnit outcomes")
    if scan_tree(root):
        raise EvidenceError("evidence privacy scan failed")
    return manifest, results


def import_live_gate(
    manifest: dict[str, Any], *, expected_protocol: str, expected_commit: str,
    result: dict[str, Any] | None = None, source_manifest_sha256: str = "",
) -> dict[str, Any]:
    if expected_protocol not in PROTOCOLS:
        raise EvidenceError("expected protocol is invalid")
    if manifest.get("mode") != "observe":
        raise EvidenceError("live EDU gates require observe evidence")
    if manifest.get("protocol") != expected_protocol:
        raise EvidenceError("evidence protocol mismatch")
    if manifest.get("git_commit") != expected_commit or manifest.get("git_dirty") is not False:
        raise EvidenceError("evidence commit identity mismatch")
    if manifest.get("status") not in {"pass", "fail", "blocked"}:
        raise EvidenceError("evidence status is invalid")
    if not isinstance(result, dict) or not result:
        raise EvidenceError("matching protocol result is required")
    _validate(result, "protocol-result.schema.json")
    for assertion in result["assertions"]:
        _validate(assertion, "assertion-result.schema.json")
    if result.get("mode") != "observe" or result.get("certification_level") != "live_passive_observation":
        raise EvidenceError("live EDU gate result is not passive observation evidence")
    if result.get("protocol") != expected_protocol or result.get("git_commit") != expected_commit:
        raise EvidenceError("live EDU gate result identity mismatch")
    if result.get("status") != manifest.get("status"):
        raise EvidenceError("live EDU gate result status mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", source_manifest_sha256):
        raise EvidenceError("source manifest SHA-256 is required")
    raw_metrics = result["metrics"]
    for required in ("valid_sample_count", "freshness_seconds", "reconnect_count", "model_family", "capabilities"):
        if required not in raw_metrics:
            raise EvidenceError("live observation metrics are incomplete")
    if result["status"] == "pass" and (
        raw_metrics["valid_sample_count"] < 2
        or not 0 < raw_metrics["freshness_seconds"] <= 30
        or not raw_metrics["model_family"]
        or not raw_metrics["capabilities"]
    ):
        raise EvidenceError("passing live observation metrics are insufficient")
    metrics = {
        "certification_level": "observe",
        "valid_sample_count": raw_metrics["valid_sample_count"],
        "freshness_seconds": raw_metrics["freshness_seconds"],
        "reconnect_count": raw_metrics["reconnect_count"],
        "model_family": raw_metrics["model_family"],
        "capabilities": list(raw_metrics["capabilities"]),
        "source_manifest_sha256": source_manifest_sha256,
    }
    findings = [
        item["reason_code"] for item in result["assertions"]
        if item["status"] in {"fail", "blocked"}
    ]
    return {
        "gate_id": f"hardware_{expected_protocol}_live",
        "status": result["status"], "metrics": metrics, "findings": findings,
    }
