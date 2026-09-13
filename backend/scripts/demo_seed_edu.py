"""Idempotent EDU sandbox persona seeder.

Passwords are accepted from the caller or environment and are never logged.
The module contains no defaults so a partially configured sandbox fails loud.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from passlib.context import CryptContext

DEFAULT_DB_PATH = "/data/odin.db"
VALID_ROLES = frozenset({"admin", "operator", "viewer"})
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


@dataclass(frozen=True)
class Persona:
    email: str
    password: str
    role: str
    no_mfa: bool = False

    def validate(self) -> None:
        if not self.email or "@" not in self.email:
            raise ValueError("persona email must be a non-empty email address")
        if not self.password:
            raise ValueError(f"persona password is required for {self.email}")
        if self.role not in VALID_ROLES:
            raise ValueError(f"invalid ODIN role for {self.email}: {self.role}")


def _validated_personas(personas: Iterable[Persona]) -> list[Persona]:
    persona_list = list(personas)
    if not persona_list:
        raise ValueError("at least one sandbox persona is required")
    for persona in persona_list:
        persona.validate()
    emails = [persona.email for persona in persona_list]
    if len(emails) != len(set(emails)):
        raise ValueError("duplicate sandbox persona email")
    return persona_list


def upsert_personas_connection(
    conn: sqlite3.Connection, personas: Iterable[Persona]
) -> None:
    """Validate and upsert personas using the caller's transaction."""
    persona_list = _validated_personas(personas)

    if getattr(conn, "_postgres", False):
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=?",
                ("users",),
            ).fetchall()
        }
    else:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
    required = {
        "username", "email", "password_hash", "role",
        "is_active", "mfa_enabled", "mfa_secret",
    }
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(f"users table missing required columns: {', '.join(missing)}")

    for persona in persona_list:
        password_hash = _pwd_context.hash(persona.password)
        if persona.no_mfa:
            conn.execute(
                """
                INSERT INTO users (username, email, password_hash, role,
                                   is_active, mfa_enabled, mfa_secret)
                VALUES (?, ?, ?, ?, TRUE, FALSE, NULL)
                ON CONFLICT(username) DO UPDATE SET
                    email = excluded.email,
                    password_hash = excluded.password_hash,
                    role = excluded.role,
                    is_active = TRUE,
                    mfa_enabled = FALSE,
                    mfa_secret = NULL
                """,
                (persona.email, persona.email, password_hash, persona.role),
            )
        else:
            conn.execute(
                """
                INSERT INTO users (username, email, password_hash, role, is_active)
                VALUES (?, ?, ?, ?, TRUE)
                ON CONFLICT(username) DO UPDATE SET
                    email = excluded.email,
                    password_hash = excluded.password_hash,
                    role = excluded.role,
                    is_active = TRUE
                """,
                (persona.email, persona.email, password_hash, persona.role),
            )


def upsert_personas(db_path: str, personas: Iterable[Persona]) -> None:
    """Validate and atomically upsert sandbox personas by username/email."""
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            upsert_personas_connection(conn, personas)
    finally:
        conn.close()


def personas_from_environment() -> list[Persona]:
    """Build the required EDU personas and optional App Review persona."""
    def secret(prefix: str) -> str:
        direct = os.environ.get(f"{prefix}_PASSWORD", "")
        path = os.environ.get(f"{prefix}_PASSWORD_FILE", "").strip()
        if direct and path:
            raise ValueError(f"{prefix} password must use environment or file, not both")
        if not path:
            return direct
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError(f"{prefix}_PASSWORD_FILE must be a regular non-symlink file")
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
        if not value:
            raise ValueError(f"{prefix}_PASSWORD_FILE is empty")
        return value

    definitions = (
        ("ODIN_DEMO_EDU_ADMIN", "admin"),
        ("ODIN_DEMO_EDU_TEACHER", "operator"),
        ("ODIN_DEMO_EDU_STUDENT", "viewer"),
    )
    personas: list[Persona] = []
    missing: list[str] = []
    for prefix, role in definitions:
        email = os.environ.get(f"{prefix}_EMAIL", "").strip()
        password = secret(prefix)
        if not email:
            missing.append(f"{prefix}_EMAIL")
        if not password:
            missing.append(f"{prefix}_PASSWORD")
        if email and password:
            personas.append(Persona(email, password, role, no_mfa=True))

    reviewer_email = os.environ.get("ODIN_DEMO_REVIEWER_EMAIL", "").strip()
    reviewer_password = secret("ODIN_DEMO_REVIEWER")
    if bool(reviewer_email) != bool(reviewer_password):
        missing.append(
            "ODIN_DEMO_REVIEWER_EMAIL and ODIN_DEMO_REVIEWER_PASSWORD must be set together"
        )
    elif reviewer_email:
        personas.append(Persona(reviewer_email, reviewer_password, "viewer", no_mfa=True))

    if missing:
        raise ValueError("missing EDU sandbox credentials: " + ", ".join(missing))
    return personas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed EDU sandbox personas from environment")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    personas = personas_from_environment()
    upsert_personas(args.db_path, personas)
    print(f"EDU sandbox persona seed OK ({len(personas)} accounts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
