"""Atomic, sanitized evidence writer shared by live certification modes."""

from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from jsonschema import validate as validate_schema

from ops.edu_readiness.artifact_scan import scan_tree
from ops.hardware_certification.implementation import (
    certification_fixture_sha256, certification_implementation_sha256,
    certification_simulator_sha256,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = Path(__file__).with_name("schemas")


class ArtifactError(RuntimeError):
    pass


def utc_iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def git_identity() -> tuple[str, bool]:
    commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    return commit, dirty


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def _junit(result: dict) -> str:
    counts = result["assertion_counts"]
    cases: list[str] = []
    for assertion in result["assertions"]:
        validate_schema(assertion, _schema("assertion-result.schema.json"))
        detail = ""
        if assertion["status"] == "fail":
            detail = f'<failure message="{escape(assertion["reason_code"])}"/>'
        elif assertion["status"] == "blocked":
            detail = f'<failure type="blocked" message="{escape(assertion["reason_code"])}"/>'
        cases.append(
            f'<testcase classname="hardware_certification.{escape(result["protocol"])}" '
            f'name="{escape(assertion["id"])}" time="{assertion["duration_ms"] / 1000:.6f}">{detail}</testcase>'
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite name="hardware_certification" tests="{counts["executed"]}" '
        f'failures="{counts["failed"] + counts["blocked"]}" errors="0" skipped="0">'
        + "".join(cases) + "</testsuite>\n"
    )


def _html(result: dict, dirty: bool) -> str:
    label = {
        "observe": "LIVE PASSIVE OBSERVATION",
        "exercise": "LIVE AUTHORIZED EXERCISE",
        "replay": "SIMULATED REPLAY ONLY",
    }[result["mode"]]
    color = {"pass": "#6ee7a0", "fail": "#ff7b7b", "blocked": "#ffd166"}[result["status"]]
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(item["id"]), html.escape(item["status"].upper()), html.escape(item["reason_code"])
        )
        for item in result["assertions"]
    )
    dirty_note = " · dirty source tree (not EDU-importable)" if dirty else ""
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ODIN Hardware Certification</title>
<style>body{{font:16px/1.5 system-ui;background:#101318;color:#f5f7fa;max-width:960px;margin:auto;padding:32px}}section{{background:#1a2029;border:1px solid #3b4655;border-radius:12px;padding:20px;margin:20px 0}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid #3b4655;padding:10px}}code{{color:#8fcdff}}.status{{color:{color};font-size:1.3rem;font-weight:800}}</style></head><body>
<h1>ODIN Hardware Certification</h1><p>{html.escape(label)}</p><p class="status">{html.escape(result["status"].upper())}</p>
<p>Run <code>{html.escape(result["run_id"])}</code> · commit <code>{html.escape(result["git_commit"])}</code>{dirty_note}</p>
<section><h2>Privacy boundary</h2><p>This report contains normalized results only. Endpoint, credentials, hardware identifiers, raw topics, payloads, and job names are intentionally omitted.</p></section>
<section><h2>Assertions</h2><table><thead><tr><th>Assertion</th><th>Status</th><th>Reason</th></tr></thead><tbody>{rows}</tbody></table></section>
</body></html>'''


def publish_result(result: dict, artifact_root: Path) -> Path:
    """Validate and atomically publish one protocol evidence directory."""
    validate_schema(result, _schema("protocol-result.schema.json"))
    counts = result["assertion_counts"]
    if counts["executed"] != counts["passed"] + counts["failed"] + counts["blocked"]:
        raise ArtifactError("assertion counts are incoherent")
    if len(result["assertions"]) != counts["executed"]:
        raise ArtifactError("every executed assertion requires a retained result")
    commit, dirty = git_identity()
    if result["git_commit"] != commit:
        raise ArtifactError("result commit does not match current source")
    root = Path(artifact_root)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root.chmod(0o700)
    final = root / result["run_id"]
    staging = root / f".{result['run_id']}.{os.getpid()}.tmp"
    if final.exists() or staging.exists():
        raise ArtifactError("evidence run directory already exists")
    staging.mkdir(mode=0o700)
    try:
        result_path = staging / f"{result['protocol']}.json"
        junit_path = staging / "junit.xml"
        index_path = staging / "index.html"
        diagnostics_path = staging / "diagnostics.json"
        atomic_write(result_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
        atomic_write(junit_path, _junit(result))
        atomic_write(index_path, _html(result, dirty))
        diagnostics = {
            "schema_version": 1, "summary": "sanitized_protocol_result",
            "assertion_count": counts["executed"],
        }
        validate_schema(diagnostics, _schema("diagnostics.schema.json"))
        atomic_write(diagnostics_path, json.dumps(diagnostics, indent=2, sort_keys=True) + "\n")
        files = {
            path.name: sha256(path)
            for path in (result_path, junit_path, index_path, diagnostics_path)
        }
        started = datetime.fromisoformat(result["started_at"].replace("Z", "+00:00"))
        ended = datetime.fromisoformat(result["ended_at"].replace("Z", "+00:00"))
        manifest = {
            "schema_version": 1, "run_id": result["run_id"], "git_commit": commit,
            "git_dirty": dirty, "mode": result["mode"], "protocol": result["protocol"],
            "status": result["status"], "started_at": result["started_at"],
            "ended_at": result["ended_at"],
            "duration_seconds": (ended - started).total_seconds(),
            "junit": {
                "tests": counts["executed"], "failures": counts["failed"] + counts["blocked"],
                "errors": 0, "skipped": 0, "xfailed": 0,
            },
            "implementation_sha256": certification_implementation_sha256(),
            "sanitizer_passed": False,
            "result_references": [result_path.name], "files": files,
        }
        if result["mode"] == "replay":
            manifest["fixture_sha256"] = certification_fixture_sha256()
            manifest["simulator_sha256"] = certification_simulator_sha256()
        if result["mode"] == "exercise":
            manifest["authorization_scope"] = result["authorization_scope"]
            manifest["authorization_scope_sha256"] = result["authorization_scope_sha256"]
            manifest["requested_actions"] = result["requested_actions"]
            manifest["executed_actions"] = result["executed_actions"]
        atomic_write(staging / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        if scan_tree(staging):
            raise ArtifactError("artifact privacy scan failed")
        manifest["sanitizer_passed"] = True
        atomic_write(staging / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        if scan_tree(staging):
            raise ArtifactError("artifact privacy scan failed after publication")
        validate_schema(manifest, _schema("manifest.schema.json"))
        os.replace(staging, final)
        try:
            from .evidence import verify_artifact

            verify_artifact(final, expected_mode=result["mode"], expected_protocol=result["protocol"])
        except BaseException:
            shutil.rmtree(final)
            raise
        return final
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def write_artifact(result: dict, *, output_root: Path, git_dirty: bool) -> Path:
    """Compatibility entry point backed by the same atomic publisher."""
    _commit, actual_dirty = git_identity()
    if actual_dirty is not git_dirty:
        raise ArtifactError("requested dirty-state claim does not match current source")
    return publish_result(result, output_root)
