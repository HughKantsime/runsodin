"""Offline PostgreSQL restore orchestration and recovery."""
from __future__ import annotations

def apply_pending_postgres_restore(
    target_url: str,
    maintenance_url: str,
    *,
    acknowledgement: str | None,
    password_file: str | None = None,
    maintenance_password_file: str | None = None,
    base_dir: service.Path | None = None,
    fail_after_restore: bool = False,
) -> dict[str, object] | None:
    """Apply, bootstrap, validate, finalize, or roll back a staged archive offline."""
    maintenance_password_file = service._require_distinct_maintenance_secret(
        password_file, maintenance_password_file
    )
    if target_url in service._FAILED_RESTORE_BARRIERS:
        raise service.BackupValidationError('PostgreSQL recovery incomplete; this process retains the offline barrier')
    paths = service.postgres_paths(base_dir)
    if not paths.pending.exists() and not paths.manifest.exists():
        return None
    if not paths.manifest.is_file():
        raise service.BackupValidationError("Pending PostgreSQL restore pair is incomplete")
    manifest = service._read_pending_manifest(paths)
    state = manifest["state"]
    candidate: dict[str, object] | None = None
    if state == "staged":
        if not paths.pending.is_file():
            raise service.BackupValidationError("Pending PostgreSQL restore pair is incomplete")
        if acknowledgement != manifest.get("sha256"):
            raise service.RestoreAcknowledgementRequired(
                "PostgreSQL restore acknowledgement must exactly match the staged SHA-256"
            )
        try:
            candidate = service._inspect_compatible_archive(paths.pending,target_url,password_file,paths.backups)
            if (
                candidate["sha256"] != manifest.get("sha256")
                or candidate["size_bytes"] != manifest.get("size_bytes")
            ):
                raise service.BackupValidationError(
                    "Pending PostgreSQL restore does not match its manifest"
                )
        except BaseException as error:
            raise service._as_archive_refusal(error)

    target = service.make_url(target_url)
    maintenance = service.make_url(maintenance_url)
    if not target.database or maintenance.database == target.database:
        raise service.BackupValidationError("Maintenance URL must target a different database")
    maintenance_engine = service.create_database_engine(
        maintenance_url,
        role="restore",
        password_file=maintenance_password_file,
    )
    barrier_engine = service.create_database_engine(
        target_url,
        role="restore",
        password_file=password_file,
    )
    barrier_connection = None
    recovery_incomplete = False
    barrier_pid: int | None = None
    rollback: service.Path | None = None
    try:
        with maintenance_engine.connect() as maintenance_connection:
            service._assert_no_target_sessions(maintenance_connection, target.database)
            barrier_connection = barrier_engine.connect()
            barrier_pid = int(
                barrier_connection.execute(service.text("SELECT pg_backend_pid()"))
                .scalar_one()
            )
            barrier_connection.commit()
            barrier_connection.execute(service.text("SET lock_timeout = '5s'"))
            barrier_connection.commit()
            lock_held = False
            try:
                try:
                    barrier_connection.execute(
                        service.text("SELECT pg_advisory_lock(:key)"),
                        {"key": service.RESTORE_ADVISORY_LOCK},
                    )
                    barrier_connection.commit()
                    lock_held = True
                except service.SQLAlchemyError as exc:
                    barrier_connection.rollback()
                    raise service.BackupValidationError(
                        "PostgreSQL restore could not establish the offline application barrier"
                    ) from exc
                excluded_pids = frozenset({barrier_pid})
                service._assert_no_target_sessions(
                    maintenance_connection,
                    target.database,
                    excluded_pids=excluded_pids,
                )
                if state == "restore_in_progress":
                    rollback = service._pending_rollback_path(paths, manifest)
                    rollback_metadata = service.inspect_postgres_archive(
                        rollback,
                        target_url,
                        password_file=password_file,
                        work_dir=paths.backups,
                        trusted_prevalidated=True,
                    )
                    if (
                        rollback_metadata["sha256"]
                        != manifest.get("rollback_sha256")
                        or rollback_metadata["size_bytes"]
                        != manifest.get("rollback_size_bytes")
                    ):
                        raise service.BackupValidationError(
                            "Interrupted PostgreSQL restore rollback archive is invalid"
                        )
                    with service.restore_lock(paths.lock):
                        service._assert_no_target_sessions(
                            maintenance_connection,
                            target.database,
                            excluded_pids=excluded_pids,
                        )
                        service._restore_postgres_archive(
                            rollback, target_url, password_file, paths.backups
                        )
                        service.validate_live_postgres_objects(
                            target_url, password_file=password_file
                        )
                        service._insert_postgres_restore_audit(
                            target_url,
                            password_file,
                            "restore_failed",
                            {
                                "reason": "unclean_restore_interruption",
                                "recovered_at": service.datetime.now(service.timezone.utc).isoformat(),
                            },
                        )
                        paths.pending.unlink(missing_ok=True)
                        paths.manifest.unlink(missing_ok=True)
                        service._fsync_directory(paths.manifest.parent)
                    return {
                        "status": "rolled_back_after_interruption",
                        "rollback_backup": rollback.name,
                    }

                assert candidate is not None
                target_engine = service.create_database_engine(
                    target_url, role="restore", password_file=password_file
                )
                try:
                    with target_engine.connect() as target_connection:
                        service._assert_no_external_dependencies(target_connection)
                finally:
                    target_engine.dispose()
                service._assert_no_target_sessions(
                    maintenance_connection,
                    target.database,
                    excluded_pids=excluded_pids,
                )
                rollback, rollback_metadata = service.create_postgres_backup(
                    target_url,
                    maintenance_url=maintenance_url,
                    password_file=password_file,
                    maintenance_password_file=maintenance_password_file,
                    base_dir=base_dir,
                    prefix="rollback_",
                )
                body_started = False
                try:
                    from modules.system.pre026_restore_stage import prepared_archive
                    with service._archive_phase(prepared_archive(paths.pending,target_url,maintenance_url,password_file,maintenance_password_file,paths.backups)) as prepared:
                        with service.restore_lock(paths.lock):
                            service._assert_no_target_sessions(
                                maintenance_connection,
                                target.database,
                                excluded_pids=excluded_pids,
                            )
                            if service._sha256(paths.pending)!=manifest['sha256'] or paths.pending.stat().st_size!=manifest['size_bytes']:raise service.BackupValidationError('Staged original archive changed')
                            body_started = True
                            manifest.update(
                                {
                                    "state": "restore_in_progress",
                                    "rollback_backup": rollback.name,
                                    "rollback_sha256": rollback_metadata["sha256"],
                                    "rollback_size_bytes": rollback_metadata["size_bytes"],
                                    "apply_started_at": service.datetime.now(service.timezone.utc).isoformat(),
                                }
                            )
                            service._write_pending_manifest(paths, manifest)
                            try:
                                service._restore_postgres_archive(
                                    prepared, target_url, password_file, paths.backups
                                )
                                if fail_after_restore:
                                    raise RuntimeError("Injected post-restore failure")
                                bootstrap_engine = service.create_database_engine(
                                    target_url, role="restore", password_file=password_file
                                )
                                try:
                                    service.bootstrap_database(bootstrap_engine)
                                finally:
                                    bootstrap_engine.dispose()
                                service.validate_live_postgres_objects(
                                    target_url, password_file=password_file
                                )
                                service._insert_postgres_restore_audit(
                                    target_url,
                                    password_file,
                                    "restore_completed",
                                    {
                                        "candidate_sha256": candidate["sha256"],
                                        "restored_at": service.datetime.now(service.timezone.utc).isoformat(),
                                    },
                                )
                            except Exception as exc:
                                if rollback is not None:
                                    service._restore_postgres_archive(
                                        rollback, target_url, password_file, paths.backups
                                    )
                                    service.validate_live_postgres_objects(
                                        target_url, password_file=password_file
                                    )
                                    service._insert_postgres_restore_audit(
                                        target_url,
                                        password_file,
                                        "restore_failed",
                                        {
                                            "reason": type(exc).__name__,
                                            "failed_at": service.datetime.now(service.timezone.utc).isoformat(),
                                        },
                                    )
                                    # Not only an audit row: keep the archive with a record, so
                                    # the backups list and the superadmin notice show it.
                                    service._set_aside_failed_restore(paths, manifest,
                                        reason=f"The restore failed while being applied ({type(exc).__name__}: {str(exc)[:300]}); "
                                               "the database was rolled back to its pre-restore backup.",
                                        error_type=type(exc).__name__, status="restore_failed_rolled_back",
                                        action=f"The database was restored from {rollback.name}. Re-stage the archive once the "
                                               "reason is fixed, or delete it.")
                                paths.pending.unlink(missing_ok=True)
                                paths.manifest.unlink(missing_ok=True)
                                service._fsync_directory(paths.manifest.parent)
                                raise
                            paths.pending.unlink(missing_ok=True)
                            paths.manifest.unlink(missing_ok=True)
                            service._fsync_directory(paths.manifest.parent)
                            return {
                                "status": "restored",
                                "rollback_backup": rollback.name,
                                "candidate_sha256": candidate["sha256"],
                            }
                except BaseException:
                    # The restore body never ran: this attempt's rollback dump is unused.
                    if not body_started and rollback is not None:
                        rollback.unlink(missing_ok=True)
                    raise
            except BaseException as exc:
                if lock_held and paths.manifest.exists() and manifest.get('state') == 'restore_in_progress':
                    recovery_incomplete = True
                    service._FAILED_RESTORE_BARRIERS[target_url] = (barrier_connection, barrier_engine)
                    manifest.update({'recovery_incomplete': True, 'recovery_error': type(exc).__name__})
                    service._write_pending_manifest(paths, manifest)
                    raise service.BackupValidationError('PostgreSQL recovery incomplete; offline barrier retained in this process') from exc
                raise
            finally:
                if lock_held and not recovery_incomplete:
                    barrier_connection.execute(
                        service.text("SELECT pg_advisory_unlock(:key)"),
                        {"key": service.RESTORE_ADVISORY_LOCK},
                    )
                    barrier_connection.commit()
    finally:
        if not recovery_incomplete:
            if barrier_connection is not None:
                barrier_connection.close()
            barrier_engine.dispose()
        maintenance_engine.dispose()

# Bind after definitions so either the service or this module can be imported first.
from modules.system import postgres_backup_service as service
