"""Fail-closed result and artifact policy for the candidate gate."""

from __future__ import annotations

import re
from pathlib import Path

from defusedxml import ElementTree as ET


class GatePolicyError(RuntimeError):
    """A release-gate invariant was not satisfied."""


_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("authorization bearer token", re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    (
        "secret assignment",
        re.compile(
            r"(?i)\b(?:api_key|jwt_secret(?:_key)?|encryption_key|"
            r"(?:odin_[a-z0-9_]*_)?password)\s*[:=]\s*[^\s,;]+"
        ),
    ),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    (
        "jwt-like token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
)


def inspect_junit(path: Path) -> dict[str, int]:
    """Return testcase-derived totals or raise on any non-passing outcome."""
    if not path.is_file():
        raise GatePolicyError(f"JUnit report does not exist: {path}")
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        raise GatePolicyError(f"JUnit report is unreadable: {path}: {exc}") from exc

    testcases = list(root.iter("testcase"))
    tests = len(testcases)
    failures = sum(1 for case in testcases if case.find("failure") is not None)
    errors = sum(1 for case in testcases if case.find("error") is not None)
    skipped = sum(1 for case in testcases if case.find("skipped") is not None)
    passed = tests - failures - errors - skipped

    for suite in (node for node in root.iter() if node.tag in {"testsuite", "testsuites"}):
        suite_cases = list(suite.iter("testcase"))
        derived = {
            "tests": len(suite_cases),
            "failures": sum(1 for case in suite_cases if case.find("failure") is not None),
            "errors": sum(1 for case in suite_cases if case.find("error") is not None),
            "skipped": sum(1 for case in suite_cases if case.find("skipped") is not None),
        }
        for attribute, actual in derived.items():
            declared_text = suite.get(attribute)
            if declared_text is None:
                continue
            try:
                declared = int(declared_text)
            except ValueError as exc:
                raise GatePolicyError(
                    f"JUnit report has invalid {attribute} total: {declared_text!r}"
                ) from exc
            if declared < 0 or declared != actual:
                raise GatePolicyError(
                    f"JUnit report has inconsistent {attribute} total: "
                    f"declared={declared}, derived={actual}"
                )

    totals = {
        "tests": tests,
        "passed": passed,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }
    if tests <= 0:
        raise GatePolicyError(f"JUnit report contains zero testcases: {path}")
    if failures or errors or skipped or passed != tests:
        raise GatePolicyError(
            "JUnit report is not clean: "
            + ", ".join(f"{key}={value}" for key, value in totals.items())
        )
    return totals


def scan_text_for_secrets(text: str, known_secrets: list[str] | tuple[str, ...]) -> list[str]:
    """Describe secret material found in text without returning secret values."""
    findings: list[str] = []
    for secret in known_secrets:
        if len(secret) >= 8 and secret in text:
            findings.append("generated secret value")
            break
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(label)
    return sorted(set(findings))


def redact_text(text: str, known_secrets: list[str] | tuple[str, ...]) -> str:
    """Redact known and recognizable secret values before retaining a text artifact."""
    redacted = text
    for secret in known_secrets:
        if len(secret) >= 8:
            redacted = redacted.replace(secret, "[REDACTED]")
    for _label, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


_FORBIDDEN_SUITE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pytest.skip", re.compile(r"\bpytest\s*\.\s*skip\b")),
    ("pytest.xfail", re.compile(r"\bpytest\s*\.\s*xfail\b")),
    ("pytest skip marker", re.compile(r"\bpytest\s*\.\s*mark\s*\.\s*skip(?:if)?\b")),
    ("unittest skip", re.compile(r"(?:\bunittest\s*\.\s*)?\bskip(?:If|Unless)?\s*\(")),
    ("collection ignore hook", re.compile(r"\bpytest_ignore_collect\b|\bcollect_ignore(?:_glob)?\b")),
    ("deselection hook", re.compile(r"\bpytest_collection_modifyitems\b|--deselect\b")),
    ("module skip marker", re.compile(r"\bpytestmark\s*=.*\bskip")),
)


def assert_candidate_suite_has_no_skip_mechanisms(suite: Path) -> None:
    """Reject skip, xfail, ignore, and deselection mechanisms in candidate tests."""
    if not suite.is_dir():
        raise GatePolicyError(f"candidate suite does not exist: {suite}")
    python_files = sorted(suite.rglob("*.py"))
    if not python_files:
        raise GatePolicyError(f"candidate suite contains no Python files: {suite}")
    violations: list[str] = []
    for path in python_files:
        source = path.read_text(encoding="utf-8")
        for label, pattern in _FORBIDDEN_SUITE_PATTERNS:
            if pattern.search(source):
                violations.append(f"{path.relative_to(suite)}: {label}")
    if violations:
        raise GatePolicyError("candidate suite contains forbidden skip behavior: " + "; ".join(violations))
