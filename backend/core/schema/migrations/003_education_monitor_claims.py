"""Add durable physical-dispatch correlation for Education print monitoring."""

from __future__ import annotations

from sqlalchemy import inspect, text


MIGRATION_ID = "python:003-education-monitor-claims"

REQUIRED_COLUMNS = frozenset(
    "claim_id org_id submission_id job_id printer_id authority_revision "
    "token_digest state created_at updated_at".split()
)

REQUIRED_INDEXES = {
    "uq_education_submissions_id_org": (("id", "org_id"), True),
    "uq_education_monitor_claim_submission": (("submission_id",), True),
    "uq_education_monitor_claim_job": (("job_id",), True),
    "uq_education_monitor_claim_printer": (("printer_id",), True),
    "uq_education_monitor_claim_token": (("token_digest",), True),
    "ix_education_monitor_claim_state": (("state", "updated_at"), False),
}

EXPECTED_FOREIGN_KEYS = {
    (("submission_id", "org_id"), "education_submissions", ("id", "org_id")),
    (("job_id", "org_id"), "jobs", ("id", "charged_to_org_id")),
    (("printer_id", "org_id"), "printers", ("id", "org_id")),
}


def _timestamp_type(connection) -> str:
    return (
        "TIMESTAMP WITH TIME ZONE"
        if connection.dialect.name == "postgresql"
        else "DATETIME"
    )


def apply(connection) -> None:
    timestamp = _timestamp_type(connection)
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_education_submissions_id_org "
            "ON education_submissions(id, org_id)"
        )
    )
    connection.execute(
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- DDL type fragment is selected only from fixed SQLite/PostgreSQL constants
        text(
            f"""CREATE TABLE IF NOT EXISTS education_monitor_claims (
                claim_id VARCHAR(36) PRIMARY KEY,
                org_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                job_id INTEGER NOT NULL,
                printer_id INTEGER NOT NULL,
                authority_revision INTEGER NOT NULL CHECK (authority_revision >= 1),
                token_digest VARCHAR(64) NOT NULL,
                state VARCHAR(16) NOT NULL CHECK (state IN ('reserved','awaiting','running')),
                created_at {timestamp} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (submission_id, org_id)
                    REFERENCES education_submissions(id, org_id) ON DELETE RESTRICT,
                FOREIGN KEY (job_id, org_id)
                    REFERENCES jobs(id, charged_to_org_id) ON DELETE RESTRICT,
                FOREIGN KEY (printer_id, org_id)
                    REFERENCES printers(id, org_id) ON DELETE RESTRICT
            )"""
        )
    )
    statements = (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_education_monitor_claim_submission "
        "ON education_monitor_claims(submission_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_education_monitor_claim_job "
        "ON education_monitor_claims(job_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_education_monitor_claim_printer "
        "ON education_monitor_claims(printer_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_education_monitor_claim_token "
        "ON education_monitor_claims(token_digest)",
        "CREATE INDEX IF NOT EXISTS ix_education_monitor_claim_state "
        "ON education_monitor_claims(state, updated_at)",
    )
    for statement in statements:
        connection.execute(text(statement))


def validate(connection) -> None:
    inspector = inspect(connection)
    if "education_monitor_claims" not in inspector.get_table_names():
        raise RuntimeError("Education monitor claim table is missing")
    columns = {item["name"] for item in inspector.get_columns("education_monitor_claims")}
    if columns != REQUIRED_COLUMNS:
        raise RuntimeError("Education monitor claim column shape mismatch")

    submission_indexes = {
        item["name"]: (tuple(item.get("column_names") or ()), bool(item.get("unique")))
        for item in inspector.get_indexes("education_submissions")
    }
    if submission_indexes.get("uq_education_submissions_id_org") != (("id", "org_id"), True):
        raise RuntimeError("Education submission tenant parent index is missing")

    indexes = {
        item["name"]: (tuple(item.get("column_names") or ()), bool(item.get("unique")))
        for item in inspector.get_indexes("education_monitor_claims")
    }
    for name, expected in REQUIRED_INDEXES.items():
        if name == "uq_education_submissions_id_org":
            continue
        if indexes.get(name) != expected:
            raise RuntimeError(f"Education monitor claim index mismatch: {name}")

    foreign_keys = {
        (
            tuple(item.get("constrained_columns") or ()),
            item.get("referred_table"),
            tuple(item.get("referred_columns") or ()),
        )
        for item in inspector.get_foreign_keys("education_monitor_claims")
    }
    if foreign_keys != EXPECTED_FOREIGN_KEYS:
        raise RuntimeError("Education monitor claim foreign-key shape mismatch")

    checks = {
        "".join(str(item.get("sqltext") or "").lower().split())
        for item in inspector.get_check_constraints("education_monitor_claims")
    }
    if not any("authority_revision>=1" in check for check in checks):
        raise RuntimeError("Education monitor claim revision check is missing")
    if not any(
        "state" in check
        and all(value in check for value in ("reserved", "awaiting", "running"))
        for check in checks
    ):
        raise RuntimeError("Education monitor claim state check is missing")
