"""Validate normalized promotion request records (context, not authority)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = Path(__file__).with_name("release_authorization.schema.json")


class AuthorizationError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_and_validate(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorizationError("AUTHORIZATION_INVALID", str(path)) from exc
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda item: list(item.path),
    )
    if errors:
        raise AuthorizationError("AUTHORIZATION_INVALID", errors[0].message)
    if text_sha256(payload["authorization_text"]) != payload["authorization_text_sha256"]:
        raise AuthorizationError("AUTHORIZATION_TEXT_MISMATCH", "verbatim text digest mismatch")
    return payload
