"""Exact-image PostgreSQL backup and offline-restore drill."""

from __future__ import annotations

import json
import hashlib
import os
import threading
import time

from sqlalchemy import text

from core.database_config import RESTORE_ADVISORY_LOCK, create_database_engine
from modules.system.backup_service import BackupValidationError
from modules.system.postgres_backup_service import (
    apply_pending_postgres_restore,
    canonical_enum_types,
    create_postgres_backup,
    postgres_paths,
    stage_postgres_restore,
)


TARGET_URL = os.environ["DATABASE_URL"]
MAINTENANCE_URL = os.environ["DATABASE_MAINTENANCE_URL"]
PASSWORD_FILE = os.environ["DATABASE_PASSWORD_FILE"]
MAINTENANCE_PASSWORD_FILE = os.environ["DATABASE_MAINTENANCE_PASSWORD_FILE"]


def backup(**kwargs):
    return create_postgres_backup(
        TARGET_URL,
        maintenance_url=MAINTENANCE_URL,
        password_file=PASSWORD_FILE,
        maintenance_password_file=MAINTENANCE_PASSWORD_FILE,
        **kwargs,
    )


def stage(candidate):
    return stage_postgres_restore(
        candidate,
        TARGET_URL,
        MAINTENANCE_URL,
        password_file=PASSWORD_FILE,
        maintenance_password_file=MAINTENANCE_PASSWORD_FILE,
    )


def apply(acknowledgement: str | None, **kwargs):
    return apply_pending_postgres_restore(
        TARGET_URL,
        MAINTENANCE_URL,
        acknowledgement=acknowledgement,
        password_file=PASSWORD_FILE,
        maintenance_password_file=MAINTENANCE_PASSWORD_FILE,
        **kwargs,
    )


def marker() -> str:
    engine = create_database_engine(TARGET_URL, role="api")
    try:
        with engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT value::text FROM system_config "
                    "WHERE key='restore_drill_marker'"
                )
            ).scalar_one()
    finally:
        engine.dispose()


def set_marker(value: str) -> None:
    engine = create_database_engine(TARGET_URL, role="api")
    try:
        with engine.begin() as connection:
            spool_result = connection.execute(
                text(
                    "INSERT INTO system_config (key, value) "
                    "VALUES ('restore_drill_marker', CAST(:value AS json)) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
                ),
                {"value": f'"{value}"'},
            )
    finally:
        engine.dispose()


def relationship_graph_fingerprint() -> str:
    """Hash stable linked-domain rows without retaining their values."""
    engine = create_database_engine(TARGET_URL, role="api")
    try:
        with engine.connect() as connection:
            inventory = connection.execute(
                text(
                    "SELECT p.name, fs.slot_number, s.qr_code, f.brand, "
                    "f.material FROM printers p "
                    "JOIN filament_slots fs ON fs.printer_id=p.id "
                    "JOIN spools s ON s.id=fs.assigned_spool_id "
                    "JOIN filament_library f ON f.id=s.filament_id "
                    "WHERE p.name='ODIN Candidate Gate Printer' ORDER BY fs.slot_number"
                )
            ).fetchall()
            production = connection.execute(
                text(
                    "SELECT j.item_name, j.status, m.name, o.order_number, "
                    "pc.quantity_needed "
                    "FROM jobs j JOIN models m ON m.id=j.model_id "
                    "JOIN order_items oi ON oi.id=j.order_item_id "
                    "JOIN orders o ON o.id=oi.order_id "
                    "JOIN product_components pc ON pc.product_id=oi.product_id "
                    "WHERE o.order_number='ODIN-CANDIDATE-ORDER-001' "
                    "ORDER BY j.item_name"
                )
            ).fetchall()
        payload = json.dumps(
            {
                "inventory": [list(row) for row in inventory],
                "production": [list(row) for row in production],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    finally:
        engine.dispose()


def mutate_relationship_graph() -> None:
    engine = create_database_engine(TARGET_URL, role="api")
    try:
        with engine.begin() as connection:
            spool_result = connection.execute(
                text(
                    "UPDATE spools SET qr_code='ODIN-MUTATED-SPOOL' "
                    "WHERE qr_code='ODIN-CANDIDATE-SPOOL'"
                )
            )
            job_result = connection.execute(
                text(
                    "UPDATE jobs SET item_name=item_name || ' mutated' "
                    "WHERE item_name LIKE 'ODIN Candidate Cube%'"
                )
            )
            assert spool_result.rowcount == 1, spool_result.rowcount
            assert job_result.rowcount == 2, job_result.rowcount
    finally:
        engine.dispose()


def run() -> None:
    maintenance_engine = create_database_engine(
        MAINTENANCE_URL,
        role="restore",
        password_file=MAINTENANCE_PASSWORD_FILE,
    )
    try:
        with maintenance_engine.connect() as connection:
            roles = {
                row[0]: (row[1], row[2], row[3])
                for row in connection.execute(
                    text(
                        "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole "
                        "FROM pg_roles WHERE rolname IN ('odin', 'odin_maintenance')"
                    )
                )
            }
        assert roles == {
            "odin": (False, False, False),
            "odin_maintenance": (False, True, False),
        }
    finally:
        maintenance_engine.dispose()

    barrier_engine = create_database_engine(
        TARGET_URL,
        role="restore",
        password_file=PASSWORD_FILE,
    )
    barrier_acquired = threading.Event()
    barrier_errors: list[BaseException] = []

    def connect_as_api() -> None:
        api_engine = create_database_engine(TARGET_URL, role="api")
        try:
            with api_engine.connect():
                barrier_acquired.set()
        except BaseException as exc:
            barrier_errors.append(exc)
        finally:
            api_engine.dispose()

    try:
        with barrier_engine.connect() as maintenance_connection:
            maintenance_connection.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": RESTORE_ADVISORY_LOCK},
            )
            maintenance_connection.commit()
            waiter = threading.Thread(target=connect_as_api, daemon=True)
            waiter.start()
            time.sleep(0.5)
            assert not barrier_acquired.is_set()
            maintenance_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": RESTORE_ADVISORY_LOCK},
            )
            maintenance_connection.commit()
            waiter.join(timeout=5)
            assert barrier_acquired.is_set()
            assert not barrier_errors
    finally:
        barrier_engine.dispose()

    set_marker("candidate")
    candidate_graph = relationship_graph_fingerprint()
    candidate, candidate_metadata = backup()
    set_marker("before-success")
    mutate_relationship_graph()
    assert relationship_graph_fingerprint() != candidate_graph
    staged = stage(candidate)

    try:
        apply("0" * 64)
        raise AssertionError("wrong acknowledgement was accepted")
    except BackupValidationError as exc:
        assert "acknowledgement" in str(exc)
    assert marker() == '"before-success"'

    blocker_engine = create_database_engine(TARGET_URL, role="api")
    blocker = blocker_engine.connect()
    try:
        try:
            apply(str(staged["candidate_sha256"]))
            raise AssertionError("live session was accepted")
        except BackupValidationError as exc:
            assert "session" in str(exc)
    finally:
        blocker.close()
        blocker_engine.dispose()
    assert marker() == '"before-success"'

    dependency_engine = create_database_engine(TARGET_URL, role="restore")
    try:
        with dependency_engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA restore_guard_probe"))
            connection.execute(
                text(
                    "CREATE VIEW restore_guard_probe.users_dependency AS "
                    "SELECT id FROM public.users"
                )
            )
            enum_type = sorted(canonical_enum_types())[0]
            connection.execute(
                text(
                    "CREATE TABLE restore_guard_probe.enum_dependency "
                    f'(value public."{enum_type}")'
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE restore_guard_probe.sequence_dependency "
                    "(id BIGINT DEFAULT nextval('public.users_id_seq'::regclass))"
                )
            )
    finally:
        dependency_engine.dispose()
    try:
        try:
            apply(str(staged["candidate_sha256"]))
            raise AssertionError("external ODIN dependency was accepted")
        except BackupValidationError as exc:
            assert "dependencies" in str(exc)
    finally:
        cleanup_engine = create_database_engine(TARGET_URL, role="restore")
        try:
            with cleanup_engine.begin() as connection:
                connection.execute(text("DROP SCHEMA restore_guard_probe CASCADE"))
        finally:
            cleanup_engine.dispose()
    assert marker() == '"before-success"'

    restore_started = time.monotonic()
    result = apply(str(staged["candidate_sha256"]))
    restore_duration = time.monotonic() - restore_started
    assert result and result["status"] == "restored"
    assert marker() == '"candidate"'
    assert relationship_graph_fingerprint() == candidate_graph
    assert not postgres_paths().pending.exists()

    set_marker("fault-candidate")
    fault_candidate, _ = backup()
    set_marker("before-fault")
    fault_staged = stage(fault_candidate)
    try:
        apply(str(fault_staged["candidate_sha256"]), fail_after_restore=True)
        raise AssertionError("fault injection did not fail")
    except RuntimeError as exc:
        assert "Injected" in str(exc)
    assert marker() == '"before-fault"'
    assert not postgres_paths().pending.exists()
    assert not postgres_paths().manifest.exists()

    set_marker("before-interruption")
    interruption_rollback, interruption_metadata = backup(prefix="rollback_")
    set_marker("interrupted-state")
    interruption_candidate, _ = backup()
    stage(interruption_candidate)
    paths = postgres_paths()
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    manifest.update(
        {
            "state": "restore_in_progress",
            "rollback_backup": interruption_rollback.name,
            "rollback_sha256": interruption_metadata["sha256"],
            "rollback_size_bytes": interruption_metadata["size_bytes"],
        }
    )
    paths.manifest.write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    paths.manifest.chmod(0o600)
    recovery = apply(None)
    assert recovery and recovery["status"] == "rolled_back_after_interruption"
    assert marker() == '"before-interruption"'
    assert not paths.pending.exists()
    assert not paths.manifest.exists()
    evidence = {
        "dialect": "postgresql",
        "backup_size_bytes": candidate_metadata["size_bytes"],
        "backup_duration_seconds": candidate_metadata["backup_duration_seconds"],
        "validation_duration_seconds": candidate_metadata[
            "validation_duration_seconds"
        ],
        "restore_duration_seconds": round(restore_duration, 3),
        "table_count": candidate_metadata["table_count"],
        "toc_entries": candidate_metadata["toc_entries"],
        "toc_fingerprint": candidate_metadata["toc_fingerprint"],
        "schema_fingerprint": candidate_metadata["schema_fingerprint"],
        "relationship_graph_fingerprint": candidate_graph,
    }
    print("database-parity-evidence: " + json.dumps(evidence, sort_keys=True))
    print(
        "postgres-offline-restore: PASS wrong-ack live-session external-dependency "
        "success rollback interruption-recovery"
    )


if __name__ == "__main__":
    run()
