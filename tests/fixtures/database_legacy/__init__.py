"""Deterministic fictional database histories for upgrade parity tests."""

from __future__ import annotations

import json

from sqlalchemy import inspect, text

from core.schema import bootstrap_database


FIXTURE_NAMES = (
    "pre_mfa_org",
    "intermediate_capabilities",
    "narrow_idempotency",
    "uppercase_enums",
    "current_noop",
)


def _json_scalar(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _fresh_unledgered(engine, backend_root) -> dict[str, object]:
    result = bootstrap_database(engine, backend_root)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE odin_schema_migrations")
    return result


def _drop_columns(engine, table_name: str, columns: tuple[str, ...]) -> None:
    with engine.begin() as connection:
        for column_name in columns:
            connection.exec_driver_sql(
                f"ALTER TABLE {table_name} DROP COLUMN {column_name}"
            )


def build_fixture(engine, backend_root, fixture_name: str) -> dict[str, object]:
    """Build one unledgered legacy state without customer-derived data."""
    if fixture_name not in FIXTURE_NAMES:
        raise ValueError(f"Unknown legacy fixture: {fixture_name}")

    if fixture_name == "pre_mfa_org":
        serial = "BIGSERIAL" if engine.dialect.name == "postgresql" else "INTEGER"
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"""CREATE TABLE users (
                    id {serial} PRIMARY KEY,
                    username VARCHAR(100) UNIQUE NOT NULL,
                    email VARCHAR(200),
                    password_hash VARCHAR(200) NOT NULL,
                    role VARCHAR(20) DEFAULT 'viewer',
                    is_active BOOLEAN DEFAULT TRUE,
                    last_login TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    oidc_subject VARCHAR(200),
                    oidc_provider VARCHAR(50)
                )"""
            )
            connection.exec_driver_sql(
                f"""CREATE TABLE groups (
                    id {serial} PRIMARY KEY,
                    name VARCHAR(100) UNIQUE NOT NULL,
                    description TEXT,
                    owner_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, username, email, password_hash, role, is_active) "
                    "VALUES (7101, 'legacy-user@school.test', 'legacy-user@school.test', "
                    "'fictional-not-a-real-hash', 'admin', TRUE)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO groups (id, name, description, owner_id) "
                    "VALUES (7101, 'Legacy Fictional School', 'synthetic fixture', 7101)"
                )
            )
        return {"sentinel": "legacy-user@school.test"}

    if fixture_name == "current_noop":
        before = bootstrap_database(engine, backend_root)
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO system_config (key, value) VALUES (:key, :value)"),
                {"key": "current_noop_sentinel", "value": json.dumps("preserve-me")},
            )
        return {"sentinel": "current_noop_sentinel", "before": before}

    before = _fresh_unledgered(engine, backend_root)
    if fixture_name == "intermediate_capabilities":
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO system_config (key, value) VALUES (:key, :value)"),
                {"key": "legacy_intermediate_sentinel", "value": json.dumps("preserve-me")},
            )
        for table_name, columns in (
            ("printers", ("tags",)),
            ("jobs", ("queue_position",)),
            ("print_files", ("bed_x_mm",)),
            ("print_archives", ("energy_kwh",)),
            ("vision_settings", ("build_plate_empty_threshold",)),
        ):
            _drop_columns(engine, table_name, columns)
        return {"sentinel": "legacy_intermediate_sentinel", "before": before}

    if fixture_name == "narrow_idempotency":
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, role, is_active) "
                    "VALUES (7101, 'idempotency-fixture@school.test', "
                    "'fictional-not-a-real-hash', 'viewer', TRUE)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO idempotency_keys "
                    "(key, user_id, method, path, request_hash, auth_fingerprint, state, "
                    "response_status, response_body, response_media_type, created_at, updated_at) "
                    "VALUES ('legacy-idempotency-key', 7101, 'POST', '/synthetic', 'hash', '', "
                    "'complete', 201, '{}', 'application/json', '2026-01-01T00:00:00+00:00', "
                    "'2026-01-01T00:00:00+00:00')"
                )
            )
            connection.exec_driver_sql("DROP INDEX IF EXISTS ix_idempotency_keys_state")
        _drop_columns(
            engine,
            "idempotency_keys",
            ("state", "updated_at", "auth_fingerprint", "response_media_type"),
        )
        return {"sentinel": "legacy-idempotency-key", "before": before}

    if fixture_name == "uppercase_enums":
        with engine.begin() as connection:
            if engine.dialect.name == "postgresql":
                for table_name in ("jobs", "spools", "orders"):
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table_name} ALTER COLUMN status TYPE TEXT "
                        "USING status::text"
                    )
            connection.execute(
                text("INSERT INTO jobs (id, item_name, status) VALUES (7101, 'Synthetic Legacy Job', 'PENDING')")
            )
            connection.execute(
                text("INSERT INTO filament_library (id, brand, name) VALUES (7101, 'Fixture', 'Fixture PLA')")
            )
            connection.execute(
                text("INSERT INTO spools (id, filament_id, status) VALUES (7101, 7101, 'ACTIVE')")
            )
            connection.execute(
                text("INSERT INTO orders (id, order_number, status) VALUES (7101, 'FIXTURE-7101', 'PENDING')")
            )
        return {"sentinel": 7101, "before": before}

    raise AssertionError(f"Unhandled legacy fixture: {fixture_name}")


def assert_fixture(engine, fixture_name: str) -> None:
    inspector = inspect(engine)
    with engine.connect() as connection:
        if fixture_name == "pre_mfa_org":
            assert connection.execute(
                text("SELECT username FROM users WHERE id=7101")
            ).scalar_one() == "legacy-user@school.test"
            assert connection.execute(
                text("SELECT owner_id FROM groups WHERE id=7101")
            ).scalar_one() == 7101
            assert "mfa_enabled" in {c["name"] for c in inspector.get_columns("users")}
            assert "branding_json" in {c["name"] for c in inspector.get_columns("groups")}
        elif fixture_name == "intermediate_capabilities":
            value = connection.execute(
                text("SELECT value FROM system_config WHERE key='legacy_intermediate_sentinel'")
            ).scalar_one()
            assert _json_scalar(value) == "preserve-me"
            for table_name, column_name in (
                ("printers", "tags"),
                ("jobs", "queue_position"),
                ("print_files", "bed_x_mm"),
                ("print_archives", "energy_kwh"),
                ("vision_settings", "build_plate_empty_threshold"),
            ):
                assert column_name in {
                    c["name"] for c in inspector.get_columns(table_name)
                }
        elif fixture_name == "narrow_idempotency":
            row = connection.execute(
                text(
                    "SELECT key, state, updated_at, auth_fingerprint, response_media_type "
                    "FROM idempotency_keys WHERE key='legacy-idempotency-key'"
                )
            ).one()
            assert row[0] == "legacy-idempotency-key"
            assert row[1] == "pending"
            assert row[2] == "2026-01-01T00:00:00+00:00"
            assert row[3] == ""
            assert row[4] == "application/json"
        elif fixture_name == "uppercase_enums":
            assert connection.execute(text("SELECT status FROM jobs WHERE id=7101")).scalar_one() == "pending"
            assert connection.execute(text("SELECT status FROM spools WHERE id=7101")).scalar_one() == "active"
            assert connection.execute(text("SELECT status FROM orders WHERE id=7101")).scalar_one() == "pending"
        else:
            value = connection.execute(
                text("SELECT value FROM system_config WHERE key='current_noop_sentinel'")
            ).scalar_one()
            assert _json_scalar(value) == "preserve-me"
