"""Fail closed when generated EDU evidence contains likely secret or personal data."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

try:
    from .common import utc_now, write_result
except ImportError:
    from common import utc_now, write_result


TEXT_SUFFIXES = {".json", ".html", ".txt", ".log", ".csv", ".md", ".xml"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b")
PRIVATE_IP_RE = re.compile(r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:password|access[_-]?code|session|cookie|token|license)[\"']?\s*[:=]\s*[\"'](?!\[?redacted\]?|synthetic|none|null)[^\"'\s]{6,}"
)
PRINTER_SERIAL_RE = re.compile(r"(?i)\b(?:serial|device[_-]?id|mainboard[_-]?id)[\"']?\s*[:=]\s*[\"'][A-Z0-9]{8,}")


def scan_text(text: str, relative_path: str) -> list[str]:
    findings = []
    for match in EMAIL_RE.finditer(text):
        domain = match.group(1).lower()
        if not domain.endswith(".test") and domain not in {"example.com", "example.org", "example.net"}:
            findings.append(f"{relative_path}: non-reserved email domain")
            break
    checks = [
        (PRIVATE_IP_RE, "private IP address"),
        (JWT_RE, "JWT-like value"),
        (KEY_RE, "private key material"),
        (SENSITIVE_ASSIGNMENT_RE, "sensitive assigned value"),
        (PRINTER_SERIAL_RE, "printer identifier"),
    ]
    for pattern, label in checks:
        if pattern.search(text):
            findings.append(f"{relative_path}: {label}")
    return findings


def scan_tree(root: Path) -> list[str]:
    findings = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            findings.append(f"{path.relative_to(root)}: symlink not allowed")
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            data = path.read_bytes()
            valid_magic = data.startswith(b"\x89PNG\r\n\x1a\n") if suffix == ".png" else data.startswith(b"\xff\xd8\xff")
            if not valid_magic:
                findings.append(f"{path.relative_to(root)}: invalid image evidence")
                continue
            # Scan binary chunks, then require an explicit OCR/review sidecar
            # for visible pixels. Fail closed when rendered text was not
            # inspected instead of claiming byte scanning is OCR.
            findings.extend(scan_text(data.decode("latin-1", errors="ignore"), str(path.relative_to(root))))
            sidecar = path.with_suffix(path.suffix + ".ocr.txt")
            if not sidecar.is_file():
                findings.append(f"{path.relative_to(root)}: image lacks required OCR review sidecar")
            else:
                findings.extend(
                    scan_text(
                        sidecar.read_text(encoding="utf-8"),
                        str(sidecar.relative_to(root)),
                    )
                )
            continue
        if suffix not in TEXT_SUFFIXES:
            findings.append(f"{path.relative_to(root)}: unsupported binary evidence file")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"{path.relative_to(root)}: non-UTF-8 evidence file")
            continue
        findings.extend(scan_text(text, str(path.relative_to(root))))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    root = args.root.resolve()
    findings = scan_tree(root)
    if findings:
        for finding in findings:
            print(f"FAIL {finding}")
    else:
        print("PASS artifact scan: no secret/PII patterns found")
    if args.run_id:
        result = {
            "schema_version": 1,
            "run_id": args.run_id,
            "gate_id": "artifact_scan",
            "mandatory": True,
            "status": "fail" if findings else "pass",
            "started_at": utc_now(),
            "ended_at": utc_now(),
            "duration_seconds": 0,
            "tool_versions": {"scanner": "odin-edu-1"},
            "executed_count": sum(1 for path in root.rglob("*") if path.is_file()),
            "skipped_count": 0,
            "xfailed_count": 0,
            "metrics": {"finding_count": len(findings)},
            "findings": findings,
            "artifacts": [],
        }
        write_result(root, result)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
