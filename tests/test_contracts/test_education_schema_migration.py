from __future__ import annotations

import sys
import importlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _engine(tmp_path: Path, name: str = "education.db"):
    return create_engine(f"sqlite:///{tmp_path / name}")


def test_fresh_sqlite_education_schema_is_tenant_enforced_and_immutable(
    tmp_path: Path,
) -> None:
    from core.schema import bootstrap_database

    engine = _engine(tmp_path)
    result = bootstrap_database(engine, BACKEND)
    assert "python:002-education-tenant-integrity" in result["applied"]

    with engine.begin() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        connection.execute(
            text("INSERT INTO groups (id, name, is_org) VALUES (1, 'school-a', 1), (2, 'school-b', 1)")
        )
        connection.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, is_active, group_id) "
                "VALUES (1, 'admin-a', 'fixture', 'admin', 1, 1), "
                "(2, 'student-b', 'fixture', 'viewer', 1, 2)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO education_cost_centers "
                "(id, org_id, name_key, code_key, display_name, code, created_by) "
                "VALUES (1, 1, 'robotics', 'rob', 'Robotics', 'ROB', 1)"
            )
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO education_cost_center_grants "
                    "(org_id, cost_center_id, user_id, role, granted_by) "
                    "VALUES (1, 1, 2, 'student', 1)"
                )
            )

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO education_audit_events "
                "(event_id, org_id, actor_kind, actor_id, action, command_id, "
                "request_hash, resource_type, resource_id) VALUES "
                "('event-1', 1, 'user', '1', 'center.create', 'command-1', "
                "'hash-1', 'cost_center', '1')"
            )
        )
        with pytest.raises(IntegrityError, match="immutable"):
            connection.execute(
                text(
                    "UPDATE education_audit_events SET action='changed' "
                    "WHERE event_id='event-1'"
                )
            )
    engine.dispose()


def test_oidc_identity_is_unique_by_exact_issuer_and_subject(tmp_path: Path) -> None:
    from core.schema import bootstrap_database

    engine = _engine(tmp_path)
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(username, password_hash, oidc_issuer, oidc_subject) "
                "VALUES ('first', 'fixture', 'https://issuer-a.example', 'subject-1')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users "
                "(username, password_hash, oidc_issuer, oidc_subject) "
                "VALUES ('other-issuer', 'fixture', 'https://issuer-b.example', 'subject-1')"
            )
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(username, password_hash, oidc_issuer, oidc_subject) "
                    "VALUES ('duplicate', 'fixture', 'https://issuer-a.example', 'subject-1')"
                )
            )
    engine.dispose()


def test_sqlite_migration_failure_restores_foreign_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.schema import bootstrap_database
    engine = _engine(tmp_path)
    education_tenant_integrity = importlib.import_module(
        "core.schema.migrations.002_education_tenant_integrity"
    )

    def fail_after_foreign_keys_are_disabled(connection) -> None:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0
        raise RuntimeError("synthetic education migration failure")

    monkeypatch.setattr(education_tenant_integrity, "apply", fail_after_foreign_keys_are_disabled)
    with pytest.raises(RuntimeError, match="synthetic education migration failure"):
        bootstrap_database(engine, BACKEND)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM odin_schema_migrations "
                "WHERE migration_id='python:002-education-tenant-integrity'"
            )
        ).scalar_one() == 0
    engine.dispose()


def test_sqlite_migration_rejects_foreign_key_check_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.schema import bootstrap_database
    from core.schema.migrator import MigrationError

    engine = _engine(tmp_path)
    education_tenant_integrity = importlib.import_module(
        "core.schema.migrations.002_education_tenant_integrity"
    )
    original_apply = education_tenant_integrity.apply

    def apply_with_violation(connection) -> None:
        original_apply(connection)
        connection.execute(
            text(
                "INSERT INTO education_cost_center_grants "
                "(org_id, cost_center_id, user_id, role, granted_by) "
                "VALUES (999, 999, 999, 'student', 999)"
            )
        )

    monkeypatch.setattr(education_tenant_integrity, "apply", apply_with_violation)
    with pytest.raises(MigrationError, match="foreign-key violations"):
        bootstrap_database(engine, BACKEND)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM odin_schema_migrations "
                "WHERE migration_id='python:002-education-tenant-integrity'"
            )
        ).scalar_one() == 0
    engine.dispose()


def test_ledgered_education_schema_drift_fails_startup(tmp_path: Path) -> None:
    from core.schema import bootstrap_database

    engine = _engine(tmp_path)
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_users_id_group")

    with pytest.raises(RuntimeError, match="uq_users_id_group"):
        bootstrap_database(engine, BACKEND)
    engine.dispose()


def test_ambiguous_legacy_file_ownership_is_not_guessed(tmp_path: Path) -> None:
    from core.schema import bootstrap_database

    engine = _engine(tmp_path)
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO groups (id, name, is_org) VALUES (1, 'one', 1), (2, 'two', 1)")
        )
        connection.execute(
            text("INSERT INTO models (id, name, org_id) VALUES (1, 'fixture-model', 1)")
        )
        connection.execute(
            text(
                "INSERT INTO jobs (id, item_name, charged_to_org_id) "
                "VALUES (1, 'fixture-job', 2)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO print_files (id, filename, model_id, job_id) "
                "VALUES (1, 'fixture.3mf', 1, 1)"
            )
        )
        connection.execute(
            text(
                "DELETE FROM odin_schema_migrations "
                "WHERE migration_id='python:002-education-tenant-integrity'"
            )
        )

    with pytest.raises(RuntimeError, match="1 print_files with ambiguous tenant ownership"):
        bootstrap_database(engine, BACKEND)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT org_id FROM print_files WHERE id=1")
        ).scalar_one() is None
    engine.dispose()


def test_malformed_preexisting_education_table_is_not_adopted(tmp_path: Path) -> None:
    from core.schema import bootstrap_database

    engine = _engine(tmp_path)
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM odin_schema_migrations "
                "WHERE migration_id='python:002-education-tenant-integrity'"
            )
        )
        connection.exec_driver_sql("DROP TABLE education_storage_accounts")
        connection.exec_driver_sql(
            "CREATE TABLE education_storage_accounts ("
            "org_id INTEGER NOT NULL, scope_kind VARCHAR(16) NOT NULL, "
            "scope_id INTEGER NOT NULL, user_id INTEGER, reserved_bytes BIGINT, "
            "accounted_bytes BIGINT, revision INTEGER, updated_at DATETIME, "
            "PRIMARY KEY (org_id, scope_kind, scope_id))"
        )

    with pytest.raises(RuntimeError, match="columns manifest mismatch"):
        bootstrap_database(engine, BACKEND)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM odin_schema_migrations "
                "WHERE migration_id='python:002-education-tenant-integrity'"
            )
        ).scalar_one() == 0
    engine.dispose()


def test_preexisting_table_with_inverted_check_is_not_adopted(tmp_path: Path) -> None:
    from core.schema import bootstrap_database

    migration = importlib.import_module(
        "core.schema.migrations.002_education_tenant_integrity"
    )
    engine = _engine(tmp_path)
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        statement = next(
            ddl
            for ddl in migration._table_statements(connection)
            if "CREATE TABLE IF NOT EXISTS education_storage_accounts" in ddl
        )
        connection.execute(
            text(
                "DELETE FROM odin_schema_migrations "
                "WHERE migration_id='python:002-education-tenant-integrity'"
            )
        )
        connection.exec_driver_sql("DROP TABLE education_storage_accounts")
        connection.exec_driver_sql(
            statement.replace("reserved_bytes >= 0", "reserved_bytes <= 0")
        )

    with pytest.raises(RuntimeError, match="checks manifest mismatch"):
        bootstrap_database(engine, BACKEND)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM odin_schema_migrations "
                "WHERE migration_id='python:002-education-tenant-integrity'"
            )
        ).scalar_one() == 0
    engine.dispose()


def test_preexisting_table_with_changed_string_operator_is_not_adopted(
    tmp_path: Path,
) -> None:
    from core.schema import bootstrap_database

    migration = importlib.import_module(
        "core.schema.migrations.002_education_tenant_integrity"
    )
    engine = _engine(tmp_path)
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        statement = next(
            ddl
            for ddl in migration._table_statements(connection)
            if "CREATE TABLE IF NOT EXISTS education_storage_accounts" in ddl
        )
        connection.execute(
            text(
                "DELETE FROM odin_schema_migrations "
                "WHERE migration_id='python:002-education-tenant-integrity'"
            )
        )
        connection.exec_driver_sql("DROP TABLE education_storage_accounts")
        connection.exec_driver_sql(
            statement.replace("scope_kind='tenant'", "scope_kind<>'tenant'", 1)
        )

    with pytest.raises(RuntimeError, match="checks manifest mismatch"):
        bootstrap_database(engine, BACKEND)
    engine.dispose()


def test_future_batch_tables_reject_cross_tenant_links_and_user_scopes(
    tmp_path: Path,
) -> None:
    from core.schema import bootstrap_database

    engine = _engine(tmp_path)
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO groups (id, name, is_org) VALUES (1, 'one', 1), (2, 'two', 1)")
        )
        connection.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, is_active, group_id) "
                "VALUES (1, 'one-user', 'fixture', 'viewer', 1, 1), "
                "(2, 'two-user', 'fixture', 'viewer', 1, 2)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO education_audit_events "
                "(event_id, org_id, actor_kind, actor_id, action, command_id, "
                "request_hash, resource_type, resource_id) VALUES "
                "('event-one', 1, 'user', '1', 'fixture', 'command-one', "
                "'hash', 'fixture', '1')"
            )
        )

        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO education_notification_outbox "
                    "(event_id, org_id, recipient_user_id) VALUES ('event-one', 2, 2)"
                )
            )

    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO education_rate_counters "
                    "(org_id, scope_kind, scope_id, user_id, bucket_kind, bucket_start) "
                    "VALUES (1, 'user', 2, 2, 'hour', CURRENT_TIMESTAMP)"
                )
            )
    engine.dispose()
