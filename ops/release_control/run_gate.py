"""Run one command and retain a strict, sanitized, structured result."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).parents[2]
SCHEMA_PATH = Path(__file__).with_name("result.schema.json")
SENSITIVE_ENV = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|AUTH|CREDENTIAL|COOKIE)", re.I)
SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("bearer-token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
)
BASE_ENVIRONMENT_ALLOWLIST = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TMP", "TEMP", "CI",
    "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "GITHUB_REF",
    "GITHUB_REF_NAME", "GITHUB_ACTOR", "GITHUB_ACTOR_ID", "GITHUB_TRIGGERING_ACTOR",
    "RUNNER_OS", "RUNNER_ARCH",
)
CONTROLLED_HOME_KEYS = {"HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"}


def command_environment(
    allowlist: tuple[str, ...] = (), overrides: dict[str, str] | None = None,
    controlled_home: Path | None = None,
) -> dict[str, str]:
    """Build the exact environment exposed to a gate subprocess.

    The caller must name every non-runtime variable. Unknown host variables are
    deliberately dropped so a long-lived runner cannot leak ambient credentials
    into validation tools.
    """
    allowed = set(BASE_ENVIRONMENT_ALLOWLIST) | set(allowlist)
    environment = {
        key: value for key, value in os.environ.items()
        if key in allowed and key not in CONTROLLED_HOME_KEYS
    }
    if controlled_home is not None:
        environment["HOME"] = str(controlled_home)
        environment["XDG_CONFIG_HOME"] = str(controlled_home / ".config")
        environment["XDG_CACHE_HOME"] = str(controlled_home / ".cache")
    for key, value in (overrides or {}).items():
        if key not in allowed:
            raise ValueError(f"environment override is not allowlisted: {key}")
        environment[key] = value
    return environment


def validate_result(result: dict[str, object]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(result), key=lambda item: list(item.path))
    if errors:
        detail = "; ".join(error.message for error in errors[:5])
        raise ValueError(f"release-control result schema violation: {detail}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sanitize(text: str, extra: tuple[str, ...] = ()) -> tuple[str, list[str]]:
    values = {
        value
        for name, value in os.environ.items()
        if value and len(value) >= 8 and SENSITIVE_ENV.search(name)
    }
    values.update(value for value in extra if len(value) >= 8)
    sanitized = text
    findings: list[str] = []
    for value in sorted(values, key=len, reverse=True):
        if value in sanitized:
            findings.append("sensitive environment value was removed")
        sanitized = sanitized.replace(value, "[REDACTED]")
    findings = list(dict.fromkeys(findings))
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(sanitized):
            findings.append(f"sanitized log still matches {label}")
            sanitized = pattern.sub(f"[REDACTED:{label}]", sanitized)
    return sanitized, findings


def sanitize_artifact(path: Path, extra: tuple[str, ...] = ()) -> list[str]:
    if path.suffix.lower() not in {".json", ".xml", ".html", ".log", ".txt", ".sarif", ".csv"}:
        return []
    original = path.read_text(encoding="utf-8", errors="replace")
    sanitized, findings = sanitize(original, extra)
    if sanitized != original:
        path.write_text(sanitized, encoding="utf-8")
    return [f"{path.name}: {finding}" for finding in findings]


@dataclass(frozen=True)
class JUnitCounts:
    tests: int = 0
    passed: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "tests": self.tests,
            "passed": self.passed,
            "failures": self.failures,
            "errors": self.errors,
            "skipped": self.skipped,
            "xfailed": self.xfailed,
        }


def _leaf_suites(root: ET.Element) -> list[ET.Element]:
    suites = [item for item in root.iter("testsuite") if item.findall("testcase")]
    if root.tag == "testsuite" and root.findall("testcase") and root not in suites:
        suites.append(root)
    return suites


def parse_junit(path: Path) -> JUnitCounts:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = errors = skipped = xfailed = 0
    for case in cases:
        if case.find("failure") is not None:
            failures += 1
        elif case.find("error") is not None:
            errors += 1
        else:
            skipped_node = case.find("skipped")
            if skipped_node is not None:
                detail = " ".join(
                    filter(None, (skipped_node.get("type"), skipped_node.get("message"), skipped_node.text))
                ).lower()
                if "xfail" in detail:
                    xfailed += 1
                else:
                    skipped += 1
    tests = len(cases)
    passed = tests - failures - errors - skipped - xfailed
    counts = JUnitCounts(tests, passed, failures, errors, skipped, xfailed)

    declared_tests = declared_failures = declared_errors = declared_skipped = 0
    suites = _leaf_suites(root)
    if suites and all(suite.get("tests") is not None for suite in suites):
        for suite in suites:
            declared_tests += int(suite.get("tests", "0"))
            declared_failures += int(suite.get("failures", "0"))
            declared_errors += int(suite.get("errors", "0"))
            declared_skipped += int(suite.get("skipped", "0"))
        if declared_tests != tests:
            raise ValueError(f"JUnit declared {declared_tests} tests but contains {tests} testcases")
        if declared_failures != failures or declared_errors != errors:
            raise ValueError("JUnit declared failure/error totals do not match testcase outcomes")
        if declared_skipped != skipped + xfailed:
            raise ValueError("JUnit declared skipped total does not match testcase outcomes")
    return counts


def _validate_json_status(path: Path, allowed: set[str]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    status = str(payload.get("status", ""))
    if status not in allowed:
        raise ValueError(f"unexpected status {status!r} in {path}")


def _artifacts(patterns: list[str], cwd: Path, not_before: float) -> tuple[list[Path], list[str]]:
    paths: list[Path] = []
    findings: list[str] = []
    for pattern in patterns:
        matches = sorted(Path(item) for item in glob.glob(str(cwd / pattern), recursive=True))
        files = [item for item in matches if item.is_file() and item.stat().st_mtime >= not_before - 1.0]
        if not files:
            findings.append(f"required artifact pattern produced no files: {pattern}")
        paths.extend(files)
    unique = sorted(set(path.resolve() for path in paths))
    return unique, findings


def execute(
    *, gate_id: str, command: list[str], output: Path, timeout: int,
    junit: Path | None = None, expected_artifacts: list[str] | None = None,
    json_status: list[str] | None = None, redact: tuple[str, ...] = (),
    cwd: Path = ROOT, environment_allowlist: tuple[str, ...] = (),
    environment_overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path = output.with_suffix(".log")
    if junit:
        junit.parent.mkdir(parents=True, exist_ok=True)
        command = [part.replace("{junit}", str(junit)) for part in command]
    started_at = utc_now()
    started_wall = time.time()
    started = time.monotonic()
    exit_code: int | None = None
    timed_out = False
    raw = ""
    try:
        with tempfile.TemporaryDirectory(prefix="odin-release-home-") as home:
            completed = subprocess.run(
                command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=timeout, check=False,
                env=command_environment(environment_allowlist, environment_overrides, Path(home)),
            )
        exit_code = completed.returncode
        raw = completed.stdout or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        raw_value = exc.stdout or ""
        raw = raw_value.decode(errors="replace") if isinstance(raw_value, bytes) else raw_value
    sanitized, findings = sanitize(raw, redact)
    log_path.write_text(sanitized, encoding="utf-8")
    safe_command: list[str] = []
    for argument in command:
        safe_argument, command_findings = sanitize(argument, redact)
        safe_command.append(safe_argument)
        findings.extend(f"command: {finding}" for finding in command_findings)
    if timed_out:
        findings.append(f"command timed out after {timeout}s")
    elif exit_code != 0:
        findings.append(f"command exited {exit_code}")

    counts = JUnitCounts()
    if junit:
        if not junit.is_file():
            findings.append("required JUnit file is missing")
        else:
            try:
                counts = parse_junit(junit)
                if counts.tests == 0:
                    findings.append("JUnit contains zero tests")
                if counts.failures or counts.errors or counts.skipped or counts.xfailed:
                    findings.append("JUnit contains non-passing testcase outcomes")
            except (ET.ParseError, OSError, ValueError) as exc:
                findings.append(f"invalid JUnit: {exc}")

    artifact_paths, artifact_findings = _artifacts(expected_artifacts or [], cwd, started_wall)
    findings.extend(artifact_findings)
    for artifact_path in artifact_paths:
        findings.extend(sanitize_artifact(artifact_path, redact))
    for item in json_status or []:
        pattern, separator, values = item.partition("=")
        if not separator:
            findings.append(f"invalid JSON status rule: {item}")
            continue
        matches = [path for path in artifact_paths if path.match(pattern) or path.name == pattern]
        if len(matches) != 1:
            findings.append(f"JSON status rule {pattern} matched {len(matches)} artifacts")
            continue
        try:
            _validate_json_status(matches[0], set(values.split("|")))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            findings.append(str(exc))

    result: dict[str, object] = {
        "schema_version": 1,
        "gate_id": gate_id,
        "status": "pass" if not findings else "fail",
        "command": safe_command,
        "working_directory": str(cwd.resolve()),
        "started_at": started_at,
        "ended_at": utc_now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "counts": counts.as_dict(),
        "tool_versions": {"python": platform.python_version()},
        "log": {"path": str(log_path), "sha256": sha256(log_path), "sanitized": True},
        "artifacts": [
            {"path": str(path), "sha256": sha256(path)} for path in artifact_paths
        ],
        "findings": findings,
    }
    validate_result(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--expected-artifact", action="append", default=[])
    parser.add_argument("--json-status", action="append", default=[])
    parser.add_argument("--redact", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    result = execute(
        gate_id=args.gate_id, command=command, output=args.output,
        timeout=args.timeout, junit=args.junit,
        expected_artifacts=args.expected_artifact,
        json_status=args.json_status, redact=tuple(args.redact),
    )
    print(json.dumps({"gate_id": args.gate_id, "status": result["status"]}))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
