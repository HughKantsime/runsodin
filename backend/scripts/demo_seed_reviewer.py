"""
Demo Reviewer Seeder
====================
Upserts the App Review reviewer account in the demo backend's SQLite DB.

Backs ``make demo-write-credentials`` (ops/demo/Makefile.demo). Called via::

    python3 -m backend.scripts.demo_seed_reviewer \\
        --email appreview@demo.subsystem.app \\
        --password "<rotated>" \\
        --no-mfa \\
        --read-only-printer x1c-demo

Idempotent: re-running updates the password and never duplicates the row.

Why role=viewer is sufficient for App Review
--------------------------------------------
The viewer role (lowest tier; ``ROLE_HIERARCHY=1`` in ``core/auth.py``)
already cannot create users/groups/instances, generate API tokens, or
reach admin/setup routes — those require ``require_role("admin")``.

A per-user printer allowlist is NOT modeled in the RBAC matrix today
(scoping is org-level via ``group_id``). The demo stack ships exactly
one printer (id ``x1c-demo`` per ``demo_scenarios/ams-swap-loop/scenario.yaml``),
so single-printer scoping is enforced by the deployment image, not RBAC.
We accept ``--read-only-printer`` to document intent and to fail loud if
the demo image ever grows beyond that one printer.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from passlib.context import CryptContext

DEFAULT_DB_PATH = "/data/odin.db"

# Mirrors core/auth.py — keep cost factor in sync so reviewer logins
# don't pay a different bcrypt cost than other accounts.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def upsert_reviewer(
    db_path: str,
    email: str,
    password: str,
    no_mfa: bool,
    read_only_printer: str | None,
) -> None:
    pw_hash = _pwd_context.hash(password)
    mfa_enabled = 0 if no_mfa else 1

    conn = sqlite3.connect(db_path)
    try:
        # Upsert by username (UNIQUE). username == email for the reviewer.
        conn.execute(
            """
            INSERT INTO users (username, email, password_hash, role,
                               is_active, mfa_enabled, mfa_secret)
            VALUES (:u, :e, :h, 'viewer', 1, :m, NULL)
            ON CONFLICT(username) DO UPDATE SET
                email = excluded.email,
                password_hash = excluded.password_hash,
                role = 'viewer',
                is_active = 1,
                mfa_enabled = excluded.mfa_enabled,
                mfa_secret = CASE WHEN excluded.mfa_enabled = 0
                                  THEN NULL
                                  ELSE users.mfa_secret END
            """,
            {"u": email, "e": email, "h": pw_hash, "m": mfa_enabled},
        )
        conn.commit()

        if read_only_printer:
            row = conn.execute(
                "SELECT COUNT(*) FROM printers WHERE is_active = 1"
            ).fetchone()
            n = row[0] if row else 0
            if n > 1:
                # Loud-fail the security assumption that the demo stack is
                # single-printer; per-user allowlists are not modeled in RBAC.
                print(
                    f"WARNING: demo DB has {n} active printers but "
                    f"--read-only-printer={read_only_printer} was requested. "
                    "RBAC has no per-user printer allowlist; reviewer will "
                    "see all active printers. Wipe extras or model an allowlist.",
                    file=sys.stderr,
                )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Upsert the App Review reviewer user (idempotent).",
    )
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument(
        "--no-mfa", action="store_true",
        help="Disable MFA on the reviewer account (App Review requirement).",
    )
    p.add_argument(
        "--read-only-printer", default=None,
        help="Demo printer id the reviewer should see (advisory; "
             "enforced by the demo image being single-printer).",
    )
    p.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = p.parse_args(argv)

    upsert_reviewer(
        args.db_path, args.email, args.password,
        args.no_mfa, args.read_only_printer,
    )
    mfa = "off" if args.no_mfa else "on"
    print(f"reviewer upsert OK — {args.email} (role=viewer, mfa={mfa})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
