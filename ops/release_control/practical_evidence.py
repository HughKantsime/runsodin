"""Build and verify a deterministic, sanitized trusted-validation evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from ops.edu_readiness.artifact_scan import scan_text

from .policy import EXPECTED_COMPONENTS
from .run_gate import validate_result

ROOT = Path(__file__).parents[2]
TRUSTED_ROOT = ROOT / "artifacts/trusted-validation"
SCHEMA_PATH = Path(__file__).with_name("practical_evidence.schema.json")
ALLOWED_SUFFIXES = {".json", ".xml", ".html", ".log"}
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILE_COUNT = 4096
MAX_TOTAL_BYTES = 512 * 1024 * 1024
LEGACY_ZERO_COUNT_RUNS = {"local-20260914T231641Z"}
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,39}$")


class EvidenceError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_read(path: Path) -> bytes:
    try:
        initial = path.lstat()
    except OSError as exc:
        raise EvidenceError("EVIDENCE_NODE_FORBIDDEN", str(path)) from exc
    if not stat.S_ISREG(initial.st_mode):
        raise EvidenceError("EVIDENCE_NODE_FORBIDDEN", str(path))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceError("EVIDENCE_NODE_FORBIDDEN", str(path)) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceError("EVIDENCE_NODE_FORBIDDEN", str(path))
        if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ):
            raise EvidenceError("EVIDENCE_NODE_FORBIDDEN", f"file changed before open: {path}")
        if before.st_size > MAX_FILE_BYTES:
            raise EvidenceError("EVIDENCE_FILE_TOO_LARGE", str(path))
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise EvidenceError("EVIDENCE_NODE_FORBIDDEN", f"file changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_safe_read(path))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EvidenceError("EVIDENCE_INVALID", f"invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("EVIDENCE_INVALID", f"expected object: {path}")
    return payload


def _validate_schema(payload: dict[str, Any], schema_path: Path = SCHEMA_PATH) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda item: list(item.path),
    )
    if errors:
        raise EvidenceError("EVIDENCE_INVALID", errors[0].message)


def _resolve_source_dir(source: Path) -> Path:
    source = source.resolve()
    trusted = TRUSTED_ROOT.resolve()
    if source.parent != trusted or not source.is_dir():
        raise EvidenceError("EVIDENCE_NODE_FORBIDDEN", "source must be one trusted-validation run")
    if (source / "evidence").exists() or (source / "evidence").is_symlink():
        raise EvidenceError("EVIDENCE_ALREADY_EXISTS", str(source / "evidence"))
    return source


def _inventory(source: Path) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    entries: list[dict[str, Any]] = []
    content: dict[str, bytes] = {}
    for current, directory_names, file_names in os.walk(source, followlinks=False):
        current_path = Path(current)
        for name in list(directory_names):
            node = current_path / name
            if node.is_symlink():
                raise EvidenceError("EVIDENCE_NODE_FORBIDDEN", str(node.relative_to(source)))
        for name in sorted(file_names):
            path = current_path / name
            relative = path.relative_to(source).as_posix()
            if relative.startswith("evidence/"):
                continue
            if path.is_symlink():
                raise EvidenceError("EVIDENCE_NODE_FORBIDDEN", relative)
            if path.suffix.lower() not in ALLOWED_SUFFIXES:
                raise EvidenceError("EVIDENCE_TYPE_FORBIDDEN", relative)
            data = _safe_read(path)
            if len(entries) + 1 > MAX_FILE_COUNT or sum(item["bytes"] for item in entries) + len(data) > MAX_TOTAL_BYTES:
                raise EvidenceError("EVIDENCE_FILE_TOO_LARGE", "aggregate evidence input limit exceeded")
            content[relative] = data
            entries.append({"path": relative, "bytes": len(data), "sha256": digest_bytes(data)})
    entries.sort(key=lambda item: item["path"])
    return entries, content


def _artifact_path(source: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    resolved = path.resolve()
    try:
        resolved.relative_to(source)
    except ValueError as exc:
        raise EvidenceError("EVIDENCE_NODE_FORBIDDEN", raw_path) from exc
    return resolved


def _component_summaries(
    source: Path, aggregate: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    artifacts = aggregate.get("artifacts")
    if not isinstance(artifacts, list):
        raise EvidenceError("COMPONENT_SET_MISMATCH", "aggregate artifacts missing")
    found: dict[str, dict[str, Any]] = {}
    verified_native: dict[str, str] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise EvidenceError("COMPONENT_SET_MISMATCH", "invalid component reference")
        path = _artifact_path(source, artifact["path"])
        match = re.search(r"/components/(CV\d{2})/result\.json$", path.as_posix())
        if not match or match.group(1) in found:
            raise EvidenceError("COMPONENT_SET_MISMATCH", artifact["path"])
        component_id = match.group(1)
        actual_digest = digest_bytes(_safe_read(path))
        if actual_digest != artifact.get("sha256"):
            raise EvidenceError("COMPONENT_HASH_MISMATCH", component_id)
        result = _json(path)
        try:
            validate_result(result)
        except ValueError as exc:
            raise EvidenceError("EVIDENCE_INVALID", str(exc)) from exc
        counts = result["counts"]
        if (result.get("status") != "pass" or result.get("timed_out") is not False
                or result.get("exit_code") != 0 or result.get("findings") or not result.get("artifacts")
                or any(counts.get(key) for key in ("failures", "errors", "skipped", "xfailed"))):
            raise EvidenceError("AGGREGATE_NOT_PASSING", component_id)
        if counts["tests"] == 0:
            if source.name not in LEGACY_ZERO_COUNT_RUNS or counts["passed"] != 0:
                raise EvidenceError("AGGREGATE_NOT_PASSING", f"zero executed gates: {component_id}")
            normalized_counts = {"tests": 1, "passed": 1, "failures": 0, "errors": 0, "skipped": 0, "xfailed": 0}
        else:
            normalized_counts = counts
        for native_artifact in result["artifacts"]:
            native_path = _artifact_path(source, native_artifact["path"])
            native_relative = native_path.relative_to(source).as_posix()
            native_digest = digest_bytes(_safe_read(native_path))
            if native_digest != native_artifact["sha256"]:
                raise EvidenceError("NATIVE_ARTIFACT_HASH_MISMATCH", native_relative)
            existing = verified_native.setdefault(native_relative, native_digest)
            if existing != native_digest:
                raise EvidenceError("NATIVE_ARTIFACT_HASH_MISMATCH", native_relative)
        found[component_id] = {
            "id": component_id,
            "status": "pass",
            "started_at": result["started_at"],
            "ended_at": result["ended_at"],
            "duration_seconds": result["duration_seconds"],
            "counts": normalized_counts,
            "result_sha256": actual_digest,
        }
    if sorted(found) != EXPECTED_COMPONENTS:
        raise EvidenceError("COMPONENT_SET_MISMATCH", f"found {sorted(found)}")
    return [found[item] for item in EXPECTED_COMPONENTS], verified_native


def _one(source: Path, pattern: str, role: str) -> Path:
    paths = sorted(source.glob(pattern))
    if len(paths) != 1:
        raise EvidenceError("SOURCE_IDENTITY_MISMATCH", f"{role} manifest count={len(paths)}")
    return paths[0]


def _source_identity(
    source: Path, expected_sha: str, verified_native: dict[str, str],
) -> tuple[str, list[dict[str, Any]]]:
    if not FULL_SHA_RE.fullmatch(expected_sha):
        raise EvidenceError("EXPECTED_SHA_MISMATCH", "expected SHA must be full lowercase hex")
    definitions = [
        ("candidate", "native/candidate/*/manifest.json", "commit", "dirty"),
        ("database_parity", "native/database-parity/*/manifest.json", "commit", "dirty"),
        ("edu_sandbox", "native/edu-sandbox/*/manifest.json", "source_commit", "source_dirty"),
        ("hardware", "native/hardware/*/manifest.json", "git_commit", "git_dirty"),
    ]
    rows: list[dict[str, Any]] = []
    full_commits: list[str] = []
    hardware_commit = ""
    for role, pattern, commit_key, dirty_key in definitions:
        path = _one(source, pattern, role)
        relative = path.relative_to(source).as_posix()
        actual_digest = digest_bytes(_safe_read(path))
        if verified_native.get(relative) != actual_digest:
            raise EvidenceError("NATIVE_ARTIFACT_HASH_MISMATCH", relative)
        payload = _json(path)
        commit = payload.get(commit_key)
        if not isinstance(commit, str) or payload.get(dirty_key) is not False:
            if payload.get(dirty_key) is not False:
                raise EvidenceError("SOURCE_TREE_DIRTY", role)
            raise EvidenceError("SOURCE_IDENTITY_MISMATCH", role)
        if role == "hardware":
            hardware_commit = commit
            if not (FULL_SHA_RE.fullmatch(commit) or SHORT_SHA_RE.fullmatch(commit)):
                raise EvidenceError("SOURCE_IDENTITY_MISMATCH", role)
        else:
            if not FULL_SHA_RE.fullmatch(commit):
                raise EvidenceError("SOURCE_IDENTITY_MISMATCH", role)
            full_commits.append(commit)
        rows.append({
            "role": role,
            "path": relative,
            "reported_commit": commit,
            "dirty": False,
            "manifest_sha256": actual_digest,
        })
    if len(set(full_commits)) != 1:
        raise EvidenceError("SOURCE_IDENTITY_MISMATCH", "full source commits disagree")
    consensus = full_commits[0]
    if hardware_commit != consensus and not (len(hardware_commit) < 40 and consensus.startswith(hardware_commit)):
        raise EvidenceError("SOURCE_IDENTITY_MISMATCH", "hardware commit disagrees")
    if consensus != expected_sha:
        raise EvidenceError("EXPECTED_SHA_MISMATCH", f"{consensus} != {expected_sha}")
    return consensus, rows


def _render(manifest: dict[str, Any], digest: str) -> bytes:
    components = "".join(
        f"<tr><td>{html.escape(item['id'])}</td><td>PASS</td><td><code>{item['result_sha256']}</code></td></tr>"
        for item in manifest["components"]
    )
    files = "".join(
        f"<tr><td>{html.escape(item['path'])}</td><td>{item['bytes']}</td><td><code>{item['sha256']}</code></td></tr>"
        for item in manifest["files"]
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ODIN Practical Release Evidence</title><style>body{{font:16px/1.5 system-ui;max-width:1100px;margin:auto;padding:32px;color:#16202a}}.pass{{color:#08783e;font-weight:800}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #d7dde3;text-align:left}}code{{font-size:.78rem;overflow-wrap:anywhere}}</style></head><body><h1>ODIN Practical Release Evidence</h1><p class="pass">ELIGIBLE</p><p>Run <code>{html.escape(manifest['run_id'])}</code> · source <code>{manifest['source_commit']}</code></p><p>Manifest SHA-256: <code>{digest}</code></p><h2>Validation components</h2><table><thead><tr><th>Gate</th><th>Status</th><th>Result digest</th></tr></thead><tbody>{components}</tbody></table><h2>Raw source inventory</h2><p>Raw files were hashed on the trusted runner but are not included in this promotion artifact.</p><table><thead><tr><th>Relative path</th><th>Bytes</th><th>SHA-256</th></tr></thead><tbody>{files}</tbody></table></body></html>""".encode("utf-8")


def _privacy_check(data: bytes, label: str) -> None:
    try:
        findings = scan_text(data.decode("utf-8"), label)
    except UnicodeDecodeError as exc:
        raise EvidenceError("EVIDENCE_PRIVACY_REJECTED", label) from exc
    if findings:
        raise EvidenceError("EVIDENCE_PRIVACY_REJECTED", "; ".join(findings))


def _semantic_validate(manifest: dict[str, Any]) -> None:
    if manifest["candidate_ref"] != f"release-candidate/{manifest['source_commit']}":
        raise EvidenceError("EVIDENCE_INVALID", "candidate ref is not derived from source commit")
    aggregate_counts = manifest["aggregate"]["counts"]
    if aggregate_counts != {"tests": 10, "passed": 10, "failures": 0, "errors": 0, "skipped": 0, "xfailed": 0}:
        raise EvidenceError("EVIDENCE_INVALID", "aggregate is not an exact 10/10 pass")
    component_ids = [item["id"] for item in manifest["components"]]
    if component_ids != EXPECTED_COMPONENTS:
        raise EvidenceError("EVIDENCE_INVALID", "component IDs/order are not exact")
    for component in manifest["components"]:
        counts = component["counts"]
        if counts["tests"] < 1 or counts["passed"] != counts["tests"] \
                or any(counts[key] for key in ("failures", "errors", "skipped", "xfailed")):
            raise EvidenceError("EVIDENCE_INVALID", f"component is not a strict pass: {component['id']}")
    roles = [item["role"] for item in manifest["source_identities"]]
    expected_roles = ["candidate", "database_parity", "edu_sandbox", "hardware"]
    if roles != expected_roles:
        raise EvidenceError("EVIDENCE_INVALID", "source identity roles/order are not exact")
    for identity in manifest["source_identities"]:
        commit = identity["reported_commit"]
        if identity["role"] == "hardware":
            matches = commit == manifest["source_commit"] or (
                len(commit) < 40 and manifest["source_commit"].startswith(commit)
            )
        else:
            matches = commit == manifest["source_commit"]
        if not matches or identity["dirty"] is not False:
            raise EvidenceError("EVIDENCE_INVALID", "source identity does not match consensus")
    paths = [item["path"] for item in manifest["files"]]
    if paths != sorted(set(paths)):
        raise EvidenceError("EVIDENCE_INVALID", "file inventory paths are not unique and sorted")
    digests = {item["path"]: item["sha256"] for item in manifest["files"]}
    if digests.get("result.json") != manifest["aggregate"]["result_sha256"]:
        raise EvidenceError("EVIDENCE_INVALID", "aggregate digest is not in inventory")
    for component in manifest["components"]:
        if digests.get(f"components/{component['id']}/result.json") != component["result_sha256"]:
            raise EvidenceError("EVIDENCE_INVALID", f"component digest is not in inventory: {component['id']}")
    for identity in manifest["source_identities"]:
        if digests.get(identity["path"]) != identity["manifest_sha256"]:
            raise EvidenceError("EVIDENCE_INVALID", f"identity digest is not in inventory: {identity['role']}")


def build_bundle(source_run_dir: Path, expected_sha: str) -> Path:
    source = _resolve_source_dir(source_run_dir)
    inventory, _ = _inventory(source)
    aggregate_path = source / "result.json"
    aggregate = _json(aggregate_path)
    try:
        validate_result(aggregate)
    except ValueError as exc:
        raise EvidenceError("EVIDENCE_INVALID", str(exc)) from exc
    counts = aggregate["counts"]
    if (aggregate.get("status") != "pass" or aggregate.get("timed_out") is not False
            or aggregate.get("findings") or counts != {
                "tests": 10, "passed": 10, "failures": 0, "errors": 0, "skipped": 0, "xfailed": 0,
            }):
        raise EvidenceError("AGGREGATE_NOT_PASSING", "aggregate must be an exact 10/10 pass")
    components, verified_native = _component_summaries(source, aggregate)
    source_commit, identities = _source_identity(source, expected_sha, verified_native)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "eligible",
        "run_id": source.name,
        "generated_at": aggregate["ended_at"],
        "source_commit": source_commit,
        "candidate_ref": f"release-candidate/{source_commit}",
        "aggregate": {
            "status": "pass", "started_at": aggregate["started_at"], "ended_at": aggregate["ended_at"],
            "duration_seconds": aggregate["duration_seconds"], "counts": counts,
            "result_sha256": digest_bytes(_safe_read(aggregate_path)),
        },
        "source_identities": identities,
        "components": components,
        "files": inventory,
    }
    _validate_schema(manifest)
    _semantic_validate(manifest)
    manifest_bytes = canonical_json(manifest)
    digest = digest_bytes(manifest_bytes)
    html_bytes = _render(manifest, digest)
    _privacy_check(manifest_bytes, "manifest.json")
    _privacy_check(html_bytes, "index.html")
    with tempfile.TemporaryDirectory(prefix=".evidence-", dir=source) as temporary:
        staging = Path(temporary)
        (staging / "manifest.json").write_bytes(manifest_bytes)
        (staging / "manifest.sha256").write_text(f"{digest}  manifest.json\n", encoding="ascii")
        (staging / "index.html").write_bytes(html_bytes)
        os.replace(staging, source / "evidence")
    return source / "evidence"


def verify_bundle(evidence_dir: Path) -> dict[str, Any]:
    directory = evidence_dir.resolve()
    manifest_path = directory / "manifest.json"
    digest_path = directory / "manifest.sha256"
    manifest_bytes = _safe_read(manifest_path)
    try:
        expected = _safe_read(digest_path).decode("ascii").split()[0]
    except (UnicodeDecodeError, IndexError) as exc:
        raise EvidenceError("EVIDENCE_DIGEST_MISMATCH", "invalid digest file") from exc
    actual = digest_bytes(manifest_bytes)
    if expected != actual:
        raise EvidenceError("EVIDENCE_DIGEST_MISMATCH", f"{expected} != {actual}")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise EvidenceError("EVIDENCE_DIGEST_MISMATCH", "invalid manifest JSON") from exc
    if canonical_json(manifest) != manifest_bytes:
        raise EvidenceError("EVIDENCE_DIGEST_MISMATCH", "manifest is not canonical")
    _validate_schema(manifest)
    _semantic_validate(manifest)
    _privacy_check(manifest_bytes, "manifest.json")
    return {"status": "integrity_verified", "evidence_sha256": actual, "manifest": manifest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--source-run-dir", required=True, type=Path)
    build.add_argument("--expected-sha", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build":
            output = build_bundle(args.source_run_dir, args.expected_sha)
            result = verify_bundle(output)
            print(json.dumps({"status": "eligible", "evidence_dir": str(output),
                              "evidence_sha256": result["evidence_sha256"]}, sort_keys=True))
        else:
            result = verify_bundle(args.evidence_dir)
            print(json.dumps({key: value for key, value in result.items() if key != "manifest"}, sort_keys=True))
        return 0
    except EvidenceError as exc:
        print(json.dumps({"status": "fail", "code": exc.code, "detail": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
