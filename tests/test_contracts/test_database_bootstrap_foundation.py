from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_postgres_password_must_come_from_restricted_secret_file(tmp_path: Path) -> None:
    from core.database_config import DatabaseConfigurationError, resolve_database_url

    with pytest.raises(DatabaseConfigurationError, match="Credential-bearing"):
        resolve_database_url("postgresql://odin:visible@db/odin")
    with pytest.raises(DatabaseConfigurationError, match="DATABASE_PASSWORD_FILE"):
        resolve_database_url("postgresql://odin@db/odin")

    secret = tmp_path / "password"
    secret.write_text("synthetic-secret\n", encoding="utf-8")
    secret.chmod(0o644)
    with pytest.raises(DatabaseConfigurationError, match="group or other"):
        resolve_database_url("postgresql://odin@db/odin", password_file=str(secret))

    secret.chmod(0o600)
    resolved = resolve_database_url(
        "postgresql://odin@db/odin", password_file=str(secret)
    )
    assert resolved.drivername == "postgresql+psycopg"
    assert resolved.password == "synthetic-secret"
    assert "synthetic-secret" not in str(resolved)


def test_postgres_application_names_are_allowlisted() -> None:
    from core.database_config import DatabaseConfigurationError, application_name

    assert application_name("api") == "odin-api"
    assert application_name("monitor-bambu") == "odin-monitor-bambu"
    with pytest.raises(DatabaseConfigurationError, match="Unsupported ODIN database role"):
        application_name("odin-api options=-c")


def test_daemon_dbapi_adapter_preserves_quoted_qmarks() -> None:
    from core.db_utils import _postgres_placeholders

    assert _postgres_placeholders("SELECT '?', ?, \"?\", 'it''s ?'") == (
        "SELECT '?', %s, \"?\", 'it''s ?'"
    )


def test_postgres_check_fingerprint_normalizes_dump_restore_array_casts() -> None:
    from core.schema.bootstrap import _normalize_check_definition

    canonical = (
        "state::text = ANY (ARRAY['pending'::character varying, "
        "'complete'::character varying]::text[])"
    )
    restored = (
        "state::text = ANY (ARRAY['pending'::character varying::text, "
        "'complete'::character varying::text])"
    )
    assert _normalize_check_definition(
        canonical, "postgresql"
    ) == _normalize_check_definition(restored, "postgresql")
    assert _normalize_check_definition(canonical, "sqlite") == canonical


def test_sqlite_bootstrap_is_checksummed_idempotent_and_preserves_data(
    tmp_path: Path,
) -> None:
    from core.schema import bootstrap_database

    database = tmp_path / "odin.db"
    engine = create_engine(f"sqlite:///{database}")
    first = bootstrap_database(engine, BACKEND)
    assert first["dialect"] == "sqlite"
    assert first["applied"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (username, password_hash, role, is_active) "
                "VALUES ('legacy-sentinel', 'not-a-real-hash', 'viewer', 1)"
            )
        )
        ledger_before = connection.execute(
            text(
                "SELECT migration_id, checksum, dialect "
                "FROM odin_schema_migrations ORDER BY migration_id"
            )
        ).fetchall()

    second = bootstrap_database(engine, BACKEND)
    assert second["applied"] == []
    assert second["schema_fingerprint"] == first["schema_fingerprint"]
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT username FROM users WHERE username='legacy-sentinel'")
        ).scalar() == "legacy-sentinel"
        assert connection.execute(
            text(
                "SELECT migration_id, checksum, dialect "
                "FROM odin_schema_migrations ORDER BY migration_id"
            )
        ).fetchall() == ledger_before
    engine.dispose()


def test_migration_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    from core.schema.migrator import MigrationError, run_migration_files

    engine = create_engine(f"sqlite:///{tmp_path / 'checksum.db'}")
    migration = tmp_path / "001.sql"
    migration.write_text(
        "CREATE TABLE fixture (id INTEGER PRIMARY KEY);\n", encoding="utf-8"
    )
    run_migration_files(engine, [("fixture/001.sql", migration)])
    migration.write_text(
        "CREATE TABLE fixture (id INTEGER PRIMARY KEY, changed TEXT);\n",
        encoding="utf-8",
    )
    with pytest.raises(MigrationError, match="fixture/001.sql"):
        run_migration_files(engine, [("fixture/001.sql", migration)])
    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(fixture)"))
        }
    assert columns == {"id"}
    engine.dispose()


def test_unledgered_migration_rejects_wrong_existing_column_shape(
    tmp_path: Path,
) -> None:
    from core.schema.migrator import MigrationError, run_migration_files

    engine = create_engine(f"sqlite:///{tmp_path / 'wrong-shape.db'}")
    migration = tmp_path / "shape.sql"
    migration.write_text(
        "ALTER TABLE fixture ADD COLUMN state TEXT NOT NULL DEFAULT 'pending';\n",
        encoding="utf-8",
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE fixture (id INTEGER PRIMARY KEY, state INTEGER)"
        )
    with pytest.raises(MigrationError, match="fixture.state"):
        run_migration_files(engine, [("fixture/shape.sql", migration)])
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM odin_schema_migrations")
        ).scalar_one() == 0
    engine.dispose()


def test_python_legacy_migration_rejects_wrong_existing_column_shape(
    tmp_path: Path,
) -> None:
    from core.schema.migrator import MigrationError, apply_python_migration

    engine = create_engine(f"sqlite:///{tmp_path / 'wrong-python-shape.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, mfa_enabled TEXT DEFAULT 'bad')"
        )
        with pytest.raises(MigrationError, match="users.mfa_enabled"):
            apply_python_migration(
                connection, "core.schema.migrations.001_legacy_columns"
            )
        assert connection.execute(
            text("SELECT COUNT(*) FROM odin_schema_migrations")
        ).scalar_one() == 0
    engine.dispose()


def test_unledgered_migration_rejects_wrong_existing_index_shape(
    tmp_path: Path,
) -> None:
    from core.schema.migrator import MigrationError, run_migration_files

    engine = create_engine(f"sqlite:///{tmp_path / 'wrong-index.db'}")
    migration = tmp_path / "index.sql"
    migration.write_text(
        "CREATE INDEX IF NOT EXISTS ix_fixture_state ON fixture(state);\n",
        encoding="utf-8",
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE fixture (id INTEGER PRIMARY KEY, state TEXT, other TEXT)"
        )
        connection.exec_driver_sql("CREATE INDEX ix_fixture_state ON fixture(other)")
    with pytest.raises(MigrationError, match="ix_fixture_state"):
        run_migration_files(engine, [("fixture/index.sql", migration)])
    engine.dispose()


def test_schema_validation_rejects_incomplete_raw_table(tmp_path: Path) -> None:
    from core.schema import bootstrap_database
    from core.schema.bootstrap import validate_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'incomplete.db'}")
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE users DROP COLUMN theme_json")
        with pytest.raises(RuntimeError, match="users: theme_json"):
            validate_schema(connection)
    engine.dispose()


def test_foundation_source_contracts() -> None:
    requirements = (BACKEND / "requirements.txt").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "psycopg[binary]==" in requirements
    assert "postgresql-client-16" in dockerfile
    assert "scripts.bootstrap_database" in entrypoint
    assert "--database-url" not in entrypoint
    assert "sqlite3" not in entrypoint
    assert 'exec "$@"' in entrypoint
    assert "/app/backend/.env" not in entrypoint
    assert "/data/.env.supervisor" not in entrypoint
    entrypoint_lines = entrypoint.splitlines()
    umask_lines = [
        index for index, line in enumerate(entrypoint_lines) if line.strip() == "umask 077"
    ]
    assert len(umask_lines) == 3
    for index in umask_lines:
        assert entrypoint_lines[index - 1].strip() == "("
        assert entrypoint_lines[index + 2].strip() == ")"
    db_utils = (BACKEND / "core" / "db_utils.py").read_text(encoding="utf-8")
    assert "sqlite3.connect" not in db_utils
    assert "engine.raw_connection" in db_utils
    alembic_env = (BACKEND / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "create_database_engine" in alembic_env
    assert "engine_from_config" not in alembic_env


def test_postgres_restore_drill_uses_a_docker_shared_secret_path() -> None:
    runner = (ROOT / "ops" / "database_parity" / "run_restore_drill.sh").read_text(
        encoding="utf-8"
    )
    assert 'mktemp "${PWD}/.odin-pg-restore.XXXXXX"' in runner
    assert "mktemp /tmp/odin-pg-restore" not in runner
    assert "-s /run/secrets/password" in runner
    assert 'type=volume,src=${data_volume},dst=/var/lib/postgresql/data' in runner
    assert "PYTHONPATH=/app/backend" in runner
    assert "POSTGRES_USER=odin_admin" in runner
    assert "postgresql://odin_maintenance@" in runner
    assert "ODIN_PARITY_RESOURCE_SUFFIX" in runner
    assert 'test "$postmaster_pid" = 1' in runner
    assert "SELECT COUNT(*) FROM pg_roles" in runner


def test_enterprise_postgres_uses_separate_least_privilege_identities() -> None:
    compose = (ROOT / "docker-compose.enterprise.yml").read_text(encoding="utf-8")
    init_roles = (
        ROOT / "docker" / "postgres-init" / "10-odin-roles.sh"
    ).read_text(encoding="utf-8")

    assert "POSTGRES_USER: odin_admin" in compose
    assert "postgresql://odin@postgres:5432/odin" in compose
    assert "postgresql://odin_maintenance@postgres:5432/postgres" in compose
    assert "DATABASE_MAINTENANCE_PASSWORD_FILE" in compose
    assert "CREATE ROLE odin_maintenance" in init_roles
    assert "NOSUPERUSER CREATEDB NOCREATEROLE" in init_roles
    assert "CREATE ROLE odin\n    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE" in init_roles
    monitors = compose.split("  monitors:", 1)[1].split("  vision:", 1)[0]
    assert "postgres_maintenance_password" not in monitors


def test_postgres_maintenance_secret_cannot_fall_back_to_application_secret(
    tmp_path: Path,
) -> None:
    from modules.system.postgres_backup_service import (
        _require_distinct_maintenance_secret,
    )
    from modules.system.backup_service import BackupValidationError

    application = tmp_path / "application-password"
    maintenance = tmp_path / "maintenance-password"
    application.write_text("fixture-app\n", encoding="utf-8")
    maintenance.write_text("fixture-maintenance\n", encoding="utf-8")
    application.chmod(0o600)
    maintenance.chmod(0o600)

    with pytest.raises(BackupValidationError, match="dedicated maintenance"):
        _require_distinct_maintenance_secret(str(application), None)
    with pytest.raises(BackupValidationError, match="distinct"):
        _require_distinct_maintenance_secret(str(application), str(application))
    assert _require_distinct_maintenance_secret(
        str(application), str(maintenance)
    ) == str(maintenance)


def test_postgres_legacy_drill_covers_every_fixture_and_fail_closed_case() -> None:
    drill = (
        ROOT / "ops" / "database_parity" / "legacy_upgrade_drill.py"
    ).read_text(encoding="utf-8")
    assert "for fixture_name in FIXTURE_NAMES" in drill
    assert 'second["applied"] == []' in drill
    assert "malformed PostgreSQL legacy schema was accepted" in drill
    assert "odin_schema_migrations" in drill


def test_database_parity_runner_is_exact_repeatable_and_emits_html() -> None:
    runner = (ROOT / "ops" / "database_parity" / "runner.py").read_text(
        encoding="utf-8"
    )
    inventory = (ROOT / "ops" / "release_control" / "inventory.json").read_text(
        encoding="utf-8"
    )
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert '"docker", "build", "--pull", "--iidfile", str(iid_file_path)' in runner
    assert "verify_built_image(image, iid_file_path, command=_command)" in runner
    assert "DisposableImageLifecycle(image, command=_command)" in runner
    assert "image_lifecycle.finalize()" in runner
    assert '"image_lifecycle": image_lifecycle.evidence()' in runner
    assert "for attempt in (1, 2)" in runner
    assert "inspect_junit(junit)" in runner
    assert "_assert_image_metadata(image)" in runner
    assert 'active_phase = "sqlite-exact-image"' in runner
    assert "_run_sqlite_exact_image(" in runner
    assert "running_image != image_id" in runner
    assert '["docker", "restart", container]' in runner
    assert "_extract_database_evidence" in runner
    assert '"database_evidence": database_evidence' in runner
    assert '"runtime_posture": {' in runner
    assert '"postgresql-runtime: PASS"' in runner
    postgres_service = (
        BACKEND / "modules" / "system" / "postgres_backup_service.py"
    ).read_text(encoding="utf-8")
    assert 'environment["PGPASSWORD"]' in postgres_service
    assert ".odin-pgpass-" not in postgres_service
    assert 'command.extend(["--table", f"public.{table_name}"])' in postgres_service
    assert "timeout=timeout" in runner
    assert "_cleanup_resources(leaked)" in runner
    assert 'render_report(manifest, artifact_dir / "index.html")' in runner
    assert "test-database-parity:" in makefile
    assert '"name":"database-parity"' in inventory
    assert '"make","test-database-parity","CANDIDATE_PYTHON=python3.11"' in inventory
    runtime_probe = (
        ROOT / "ops" / "database_parity" / "runtime_probe.py"
    ).read_text(encoding="utf-8")
    assert "/health/ready" in runtime_probe
    assert 'status_code == 403' in runtime_probe
    assert "/api/v1/ws?token=" in runtime_probe
    assert "database_parity_background_write" in runtime_probe
    assert "verify-restored" in runtime_probe
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "unlink /var/run/supervisord.pid" in entrypoint
    assert entrypoint.index("unlink /var/run/supervisord.pid") < entrypoint.index(
        "python3 -m modules.system.restore_coordinator"
    )
    sqlite_restore = (
        ROOT / "ops" / "database_parity" / "sqlite_restore_drill.py"
    ).read_text(encoding="utf-8")
    assert "relationship_graph_fingerprint" in sqlite_restore
    assert "stage_restore(candidate, DATABASE_URL)" in sqlite_restore
    assert "validate_database(candidate)" in sqlite_restore
    restore_drill = (
        ROOT / "ops" / "database_parity" / "restore_drill.py"
    ).read_text(encoding="utf-8")
    assert "relationship_graph_fingerprint" in restore_drill
    assert "database-parity-evidence:" in restore_drill


def test_organization_creation_binds_native_boolean() -> None:
    source = (BACKEND / "modules" / "organizations" / "routes.py").read_text(
        encoding="utf-8"
    )

    assert '"is_org": True' in source
    assert "VALUES (:name, :desc, :owner, 1)" not in source


def test_sqlite_compatibility_inventory_is_complete_and_current() -> None:
    from ops.database_parity.sqlite_compatibility_inventory import (
        INVENTORY,
        serialized_inventory,
    )

    committed = INVENTORY.read_text(encoding="utf-8")
    assert committed == serialized_inventory()
    payload = json.loads(committed)
    assert set(payload["patterns"]) == {
        "sqlite3",
        "PRAGMA",
        "check_same_thread",
        "qmark-parameter",
        "last_insert_rowid",
        "lastrowid",
    }
    assert payload["entries"]
    assert {item["classification"] for item in payload["entries"]} <= {
        "sqlite-provider-only",
        "dialect-neutral",
    }
    qmark_sources = {
        (item["path"], item["source"])
        for item in payload["entries"]
        if item["pattern"] == "qmark-parameter"
    }
    assert any(
        path.endswith("notifications/job_events.py")
        and source.startswith('f"""INSERT INTO print_jobs')
        for path, source in qmark_sources
    )
    assert any(
        path.endswith("notifications/printer_health.py")
        and "updates.append" in source
        for path, source in qmark_sources
    )
    assert any(
        path.endswith("monitors/mqtt_job_lifecycle.py")
        and source.startswith('cur.execute(f"""')
        for path, source in qmark_sources
    )
    assert any(
        item["path"] == "backend/core/database_config.py"
        and item["pattern"] == "check_same_thread"
        and '"check_same_thread"' in item["source"]
        for item in payload["entries"]
    )
    assert any(
        path.endswith("monitors/mqtt_job_lifecycle.py")
        and "join('?' * len(" in source
        for path, source in qmark_sources
    )


def test_sqlite_compatibility_inventory_detects_dynamic_construction_forms() -> None:
    from ops.database_parity.sqlite_compatibility_inventory import scan_source

    engine_sites = scan_source(
        "backend/core/database_config.py",
        'create_engine(url, connect_args={"check_same_thread": False})\n',
    )
    assert {(item["pattern"], item["line"]) for item in engine_sites} == {
        ("check_same_thread", 1)
    }

    runtime_sites = scan_source(
        "backend/modules/printers/monitors/mqtt_job_lifecycle.py",
        """cur.execute(
    f\"UPDATE jobs SET state = ? WHERE id IN ({ids})\",
    values,
)
cur.execute(
    \"DELETE FROM jobs WHERE id IN ({})\".format(','.join('?' * len(ids))),
    ids,
)
""",
    )
    qmark_lines = {
        item["line"]
        for item in runtime_sites
        if item["pattern"] == "qmark-parameter"
    }
    assert qmark_lines == {2, 6}


def test_sqlite_compatibility_inventory_normalizes_fstring_child_locations() -> None:
    from ops.database_parity.sqlite_compatibility_inventory import scan_source

    sites = scan_source(
        "backend/modules/notifications/error_handling.py",
        """cur.execute(f\"\"\"
    UPDATE printers
    SET last_error_at = {sql.now()}, message = ?
    WHERE id = ?
\"\"\", values)
""",
    )
    assert [
        (item["line"], item["source"])
        for item in sites
        if item["pattern"] == "qmark-parameter"
    ] == [(1, 'cur.execute(f"""')]


def test_database_parity_artifact_sanitizer_redacts_and_rescans(
    tmp_path: Path,
) -> None:
    from ops.database_parity.runner import _sanitize_and_assert_artifacts

    secret = "Synthetic-Parity-Secret-Aa1-123456"
    artifact = tmp_path / "runtime.log"
    artifact.write_text(
        f"API_KEY={secret}\nAuthorization: Bearer {secret}\n", encoding="utf-8"
    )

    _sanitize_and_assert_artifacts(tmp_path, [secret])

    retained = artifact.read_text(encoding="utf-8")
    assert secret not in retained
    assert "API_KEY=" not in retained
    assert "Bearer" not in retained


def test_postgres_parity_boots_and_records_exact_role_images() -> None:
    shell = (
        ROOT / "ops" / "database_parity" / "run_restore_drill.sh"
    ).read_text(encoding="utf-8")
    runner = (ROOT / "ops" / "database_parity" / "runner.py").read_text(
        encoding="utf-8"
    )

    assert 'api_container="odin-pg-api-${suffix}"' in shell
    assert 'worker_container="odin-pg-worker-${suffix}"' in shell
    assert "ODIN_DB_BOOTSTRAP_OWNER=1" in shell
    assert "ODIN_DB_BOOTSTRAP_OWNER=0" in shell
    assert "database-parity-topology:" in shell
    assert 'drill_env["ODIN_PARITY_IMAGE"] = image_id' in runner
    assert 'drill_env["ODIN_PARITY_IMAGE_ID"] = image_id' in runner
    assert "_extract_topology_evidence" in runner
    assert '"topology_evidence": topology_evidence' in runner
    assert "scan_text_for_secrets" in runner
    assert "redact_text" in runner
