"""Validate a signed ODIN license artifact without exposing its contents."""

from __future__ import annotations

import argparse
import base64
import json
from datetime import date, datetime
from pathlib import Path

try:
    from license_manager import _verify_signature
except ModuleNotFoundError:  # `python -m backend.scripts...` from repo root
    from backend.license_manager import _verify_signature


def validate_license_file(
    path: Path,
    expected_tier: str | None = None,
    expected_installation_id: str | None = None,
) -> dict:
    raw = path.read_text().strip()
    parts = raw.split(".")
    if len(parts) != 2:
        raise ValueError("invalid license file format")
    payload_b64, signature_b64 = parts
    payload_bytes = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    signature = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
    if not _verify_signature(payload_bytes, signature):
        raise ValueError("invalid license signature")
    payload = json.loads(payload_bytes.decode("utf-8"))
    for field in ("tier", "licensee", "expires_at"):
        if not payload.get(field):
            raise ValueError(f"license missing required field: {field}")
    if expected_tier and payload["tier"] != expected_tier:
        raise ValueError(
            f"expected {expected_tier} license, received {payload['tier']}"
        )
    if expected_installation_id:
        payload_installation_id = payload.get("installation_id")
        if not payload_installation_id:
            raise ValueError("license is not bound to an installation")
        if payload_installation_id != expected_installation_id:
            raise ValueError("license is bound to a different installation")
    expires = datetime.strptime(payload["expires_at"].split("T")[0], "%Y-%m-%d").date()
    if expires < date.today():
        raise ValueError(f"license expired on {payload['expires_at']}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a signed ODIN license file")
    parser.add_argument("path", type=Path)
    parser.add_argument("--expected-tier")
    parser.add_argument("--expected-installation-id")
    args = parser.parse_args(argv)
    validate_license_file(
        args.path,
        expected_tier=args.expected_tier,
        expected_installation_id=args.expected_installation_id,
    )
    print("license validation OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
