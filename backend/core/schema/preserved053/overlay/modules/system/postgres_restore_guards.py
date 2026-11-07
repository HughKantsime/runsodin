"""Pending restore manifests, connection barriers, and archive application."""
from __future__ import annotations

def _read_pending_manifest(paths: service.PostgreSQLBackupPaths) -> dict[str, object]:
    try:
        manifest = service.json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, service.json.JSONDecodeError) as exc:
        raise service.BackupValidationError("Pending PostgreSQL restore manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("dialect") != "postgresql"
        or manifest.get("state") not in {"staged", "restore_in_progress"}
    ):
        raise service.BackupValidationError("Pending PostgreSQL restore manifest is unsupported")
    return manifest


def _pending_rollback_path(
    paths: service.PostgreSQLBackupPaths, manifest: dict[str, object]
) -> service.Path:
    name = manifest.get("rollback_backup")
    checksum = manifest.get("rollback_sha256")
    size = manifest.get("rollback_size_bytes")
    if (
        not isinstance(name, str)
        or not service._SAFE_ROLLBACK_NAME.fullmatch(name)
        or not isinstance(checksum, str)
        or not service.re.fullmatch(r"[0-9a-f]{64}", checksum)
        or not isinstance(size, int)
        or size < 100
        or size > service.MAX_BACKUP_BYTES
    ):
        raise service.BackupValidationError(
            "Interrupted PostgreSQL restore has invalid rollback metadata"
        )
    rollback = (paths.backups / name).resolve()
    if rollback.parent != paths.backups.resolve() or not rollback.is_file():
        raise service.BackupValidationError(
            "Interrupted PostgreSQL restore rollback archive is unavailable"
        )
    if rollback.stat().st_size != size or service._sha256(rollback) != checksum:
        raise service.BackupValidationError(
            "Interrupted PostgreSQL restore rollback archive changed"
        )
    return rollback


def _write_pending_manifest(
    paths: service.PostgreSQLBackupPaths, manifest: dict[str, object]
) -> None:
    temporary = paths.manifest.with_suffix(".json.tmp")
    temporary.write_text(service.json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    service._secure(temporary)
    service.os.replace(temporary, paths.manifest)
    service._fsync_directory(paths.manifest.parent)



TARGET_SESSION_DRAIN_SECONDS = 15
TARGET_SESSION_DRAIN_POLL_SECONDS = 0.25


def _assert_no_target_sessions(
    connection,
    database_name: str,
    *,
    excluded_pids: frozenset[int] = frozenset(),
) -> None:
    # A connection closed a moment ago, or an autovacuum worker, shows here briefly and
    # is no reason to refuse. Anything still here after the wait refuses as before.
    deadline = service.time.monotonic() + service.TARGET_SESSION_DRAIN_SECONDS
    while True:
        # Without this, a transaction keeps reading the snapshot it took first.
        connection.execute(service.text("SELECT pg_stat_clear_snapshot()"))
        sessions = connection.execute(
            service.text(
                # backend_type is hidden from this role for other roles' sessions, while a
                # missing usesysid still marks a background worker such as autovacuum.
                "SELECT pid, usesysid IS NULL, COALESCE(usename::text, ''), "
                "COALESCE(application_name, ''), COALESCE(backend_type, 'unknown') "
                "FROM pg_stat_activity WHERE datname=:database_name AND pid<>pg_backend_pid()"
            ),
            {"database_name": database_name},
        ).all()
        sessions = [row for row in sessions if int(row[0]) not in excluded_pids]
        if not sessions:
            return
        if service.time.monotonic() >= deadline:
            break
        service.time.sleep(service.TARGET_SESSION_DRAIN_POLL_SECONDS)
    service.logger.warning(
        "PostgreSQL restore refused; target database sessions (no_user, user, application, type): %s",
        sorted((bool(row[1]), str(row[2]), str(row[3]), str(row[4])) for row in sessions),
    )
    raise service.BackupValidationError(
        f"Refusing PostgreSQL restore while {len(sessions)} target database session(s) are active"
    )


def _assert_no_external_dependencies(connection) -> None:
    """Reject noncanonical objects that depend on canonical ODIN relations."""
    # Dependency catalogs live in the target database, so this check uses a
    # short-lived target connection created by the caller after session refusal.
    dependencies = connection.execute(
        service.text(
            "WITH target_relations AS ("
            " SELECT c.oid, c.relname FROM pg_class c"
            " JOIN pg_namespace n ON n.oid=c.relnamespace"
            " WHERE n.nspname='public' AND c.relname = ANY(:table_names)"
            "), target_types AS ("
            " SELECT t.oid, t.typname FROM pg_type t"
            " JOIN pg_namespace n ON n.oid=t.typnamespace"
            " WHERE n.nspname='public' AND t.typname = ANY(:type_names)"
            "), target_sequences AS ("
            " SELECT sequence.oid, sequence.relname FROM pg_class sequence"
            " JOIN pg_namespace sn ON sn.oid=sequence.relnamespace"
            " JOIN pg_depend owned ON owned.objid=sequence.oid"
            "  AND owned.classid='pg_class'::regclass"
            "  AND owned.refclassid='pg_class'::regclass"
            "  AND owned.deptype IN ('a', 'i')"
            " JOIN target_relations target ON target.oid=owned.refobjid"
            " WHERE sn.nspname='public' AND sequence.relkind='S'"
            "), dependencies AS ("
            " SELECT format('relation:%I.%I', dn.nspname, dependent.relname) AS identity"
            " FROM target_relations target"
            " JOIN pg_depend d ON d.refobjid=target.oid"
            "  AND d.classid='pg_rewrite'::regclass"
            " JOIN pg_rewrite rewrite ON rewrite.oid=d.objid"
            " JOIN pg_class dependent ON dependent.oid=rewrite.ev_class"
            " JOIN pg_namespace dn ON dn.oid=dependent.relnamespace"
            " WHERE NOT (dn.nspname='public' AND dependent.relname = ANY(:table_names))"
            " UNION"
            " SELECT format('constraint:%I.%I:%I', dn.nspname, dependent.relname, constraint_row.conname)"
            " FROM target_relations target"
            " JOIN pg_constraint constraint_row ON constraint_row.confrelid=target.oid"
            "  AND constraint_row.contype='f'"
            " JOIN pg_class dependent ON dependent.oid=constraint_row.conrelid"
            " JOIN pg_namespace dn ON dn.oid=dependent.relnamespace"
            " WHERE NOT (dn.nspname='public' AND dependent.relname = ANY(:table_names))"
            " UNION"
            " SELECT format('function:%I.%I', pn.nspname, procedure.proname)"
            " FROM target_relations target"
            " JOIN pg_depend d ON d.refobjid=target.oid"
            "  AND d.classid='pg_proc'::regclass"
            " JOIN pg_proc procedure ON procedure.oid=d.objid"
            " JOIN pg_namespace pn ON pn.oid=procedure.pronamespace"
            " WHERE pn.nspname NOT IN ('pg_catalog', 'information_schema')"
            " UNION"
            " SELECT format('inheritance:%I.%I', cn.nspname, child.relname)"
            " FROM target_relations target"
            " JOIN pg_inherits inheritance ON inheritance.inhparent=target.oid"
            " JOIN pg_class child ON child.oid=inheritance.inhrelid"
            " JOIN pg_namespace cn ON cn.oid=child.relnamespace"
            " WHERE NOT (cn.nspname='public' AND child.relname = ANY(:table_names))"
            " UNION"
            " SELECT format('type-use:%I.%I', dn.nspname, dependent.relname)"
            " FROM target_types target_type"
            " JOIN pg_depend d ON d.refobjid=target_type.oid"
            "  AND d.refclassid='pg_type'::regclass"
            "  AND d.classid='pg_class'::regclass"
            " JOIN pg_class dependent ON dependent.oid=d.objid"
            " JOIN pg_namespace dn ON dn.oid=dependent.relnamespace"
            " WHERE NOT (dn.nspname='public' AND dependent.relname = ANY(:table_names))"
            " UNION"
            " SELECT format('sequence-use:%I.%I', dn.nspname, dependent.relname)"
            " FROM target_sequences target_sequence"
            " JOIN pg_depend d ON d.refobjid=target_sequence.oid"
            "  AND d.refclassid='pg_class'::regclass"
            "  AND d.classid='pg_attrdef'::regclass"
            " JOIN pg_attrdef attribute_default ON attribute_default.oid=d.objid"
            " JOIN pg_class dependent ON dependent.oid=attribute_default.adrelid"
            " JOIN pg_namespace dn ON dn.oid=dependent.relnamespace"
            " WHERE NOT (dn.nspname='public' AND dependent.relname = ANY(:table_names))"
            ") SELECT identity FROM dependencies ORDER BY identity LIMIT 13"
        ),
        {
            "table_names": sorted(service.canonical_tables()),
            "type_names": sorted(service.canonical_enum_types()),
        },
    ).scalars().all()
    if dependencies:
        visible = dependencies[:12]
        suffix = ", …" if len(dependencies) > 12 else ""
        raise service.BackupValidationError(
            "PostgreSQL restore is blocked by non-ODIN object dependencies: "
            + ", ".join(visible)
            + suffix
        )


def _schema_sql_evidence(archive, database_url, password_file, directory, *, allowed_structure=None, schema_descriptors=None, _contract=None):
    listing = service._validated_restore_list(archive, database_url, password_file, directory,
        allowed_structure=allowed_structure, trusted_prevalidated=True, phase='schema-evidence',
        schema_descriptors=schema_descriptors,_contract=_contract)
    try:
        # No --dbname: emitted SQL is evidence only, including untrusted executable definitions.
        return service._run_pg_tool(['pg_restore', '--schema-only', '--no-owner', '--no-privileges',
            '--use-list', str(listing), '--file', '-', str(archive)], database_url, password_file, directory)
    finally:
        listing.unlink(missing_ok=True)


def _restore_postgres_archive(archive: service.Path, target_url: str, password_file: str | None, directory: service.Path, *, _contract=None, _stage=None) -> None:
    from core.schema.pre026_restore_contract import check_contract
    check_contract(_contract)
    if _contract is not None:
        if _stage is None or _stage.contract is not _contract:raise service.BackupValidationError("Owned historical stage profile differs")
        bound=_stage.verify().url;requested=service.make_url(target_url)
        if (bound.host,bound.port or 5432,bound.username,bound.database)!=(requested.host,requested.port or 5432,requested.username,requested.database):raise service.BackupValidationError("Owned025 stage target differs")
    from modules.system.postgres_canonical_executables import remove_trusted_guards, install_trusted_guards
    restore_list = None
    engine = service.create_database_engine(target_url, role='restore', password_file=password_file)
    try:
        # Existing offline barrier spans these phases. This is not one transaction.
        with engine.begin() as connection:
            if _contract is not None:_stage.verify_connection(connection)
            service.validate_schema(connection,_contract=_contract)
            if set(service.inspect(connection).get_table_names(schema='public')) != set((service.canonical_tables() if _contract is None else service.canonical_tables(_contract=_contract))):
                raise service.BackupValidationError('Restore target schema has unsupported historical compatibility')
            restore_list = service._validated_restore_list(archive, target_url, password_file, directory,
                trusted_prevalidated=True, data_connection=connection,_contract=_contract)
            remove_trusted_guards(connection,_contract=_contract)
            names = ','.join('public."'+name+'"' for name in sorted((service.canonical_tables() if _contract is None else service.canonical_tables(_contract=_contract))))
            connection.exec_driver_sql('TRUNCATE '+names+' RESTART IDENTITY')
        service._run_pg_tool(['pg_restore', '--exit-on-error', '--single-transaction', '--data-only',
            '--no-owner', '--no-privileges', '--use-list', str(restore_list), '--dbname',
            service._credentialless_cli_url(target_url, 'odin-restore'), str(archive)], target_url, password_file, directory)
        with engine.begin() as connection:
            install_trusted_guards(connection,_contract=_contract)
        # Migration adoption/data associations are verified after guard installation.
        if _contract is None:service.bootstrap_database(engine)
    finally:
        engine.dispose()
        if restore_list is not None:restore_list.unlink(missing_ok=True)



# Bind after definitions so either the service or this module can be imported first.
from modules.system import postgres_backup_service as service
