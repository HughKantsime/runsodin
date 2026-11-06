"""Disposable-database restore validation and pending archive staging."""
from __future__ import annotations

def _validate_current_postgres_restore(
    archive: service.Path,
    target_url: str,
    maintenance_url: str,
    *,
    password_file: str | None = None,
    maintenance_password_file: str | None = None,
    base_dir: service.Path | None = None,
) -> dict[str, object]:
    """Restore an allowlisted archive into a disposable database and inspect it."""
    maintenance_password_file = service._require_distinct_maintenance_secret(
        password_file, maintenance_password_file
    )
    paths = service.postgres_paths(base_dir)
    metadata = service.inspect_postgres_archive(
        archive,
        target_url,
        password_file=password_file,
        work_dir=paths.backups,
        trusted_prevalidated=True,
    )
    validation_database = "odin_validate_" + service.uuid.uuid4().hex[:20]
    if not service._SAFE_DATABASE_NAME.fullmatch(validation_database):
        raise AssertionError("Generated unsafe validation database name")
    _maintenance_cli, validation_cli = service._maintenance_urls(
        maintenance_url, target_url, validation_database
    )
    maintenance_engine = service.create_database_engine(
        maintenance_url,
        role="restore",
        password_file=maintenance_password_file,
    ).execution_options(isolation_level="AUTOCOMMIT")
    created = False
    canonical_archive: service.Path | None = None
    from modules.system import restore_artifacts
    restore_artifacts.record(paths.backups, validation_database)
    try:
        with maintenance_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{validation_database}"')
            created = True
        validation_url = (
            service.make_url(maintenance_url)
            .set(database=validation_database)
            .render_as_string(hide_password=True)
        )
        validation_engine = service.create_database_engine(
            validation_url,
            role="restore",
            password_file=maintenance_password_file,
        )
        try:
            canonical_result = service.bootstrap_database(validation_engine)
            canonical_fingerprint = str(canonical_result["schema_fingerprint"])
            with validation_engine.connect() as connection:
                canonical_manifest = service.schema_manifest(connection)
        finally:
            validation_engine.dispose()

        descriptor, canonical_name = service.tempfile.mkstemp(
            prefix=".odin-canonical-schema-", suffix=".dump", dir=paths.backups
        )
        service.os.close(descriptor)
        canonical_archive = service.Path(canonical_name)
        canonical_command = [
            "pg_dump",
            "--format=custom",
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "--no-comments",
            "--file",
            str(canonical_archive),
            "--dbname",
            validation_cli,
        ]
        canonical_command.extend(['--schema', 'public'])
        service._run_pg_tool(
            canonical_command,
            maintenance_url,
            maintenance_password_file,
            paths.backups,
        )
        canonical_toc = service._run_pg_tool(
            ["pg_restore", "--list", str(canonical_archive)],
            maintenance_url,
            maintenance_password_file,
            paths.backups,
        )
        service.validate_archive_toc(canonical_toc, allow_unresolved_structure=True)
        allowed_structure = service._toc_inventory(canonical_toc).structure
        metadata = service.inspect_postgres_archive(
            archive,
            target_url,
            password_file=password_file,
            work_dir=paths.backups,
            allowed_structure=allowed_structure,
        )
        from modules.system.postgres_canonical_executables import validate_schema_sql
        original_toc = service._run_pg_tool(['pg_restore', '--list', str(archive)], target_url, password_file, paths.backups)
        original_structure = service._toc_inventory(original_toc).structure
        original_schema = service._schema_sql_evidence(archive, target_url, password_file, paths.backups,
            allowed_structure=allowed_structure)
        canonical_schema = service._schema_sql_evidence(canonical_archive, maintenance_url, maintenance_password_file,
            paths.backups, schema_descriptors=original_structure)
        validate_schema_sql(original_schema, canonical_schema)
        # Only data enters a canonical source-created schema. Archive DDL is never executed.
        service._restore_postgres_archive(archive, validation_url, maintenance_password_file, paths.backups)
        validation_engine = service.create_database_engine(
            validation_url,
            role="restore",
            password_file=maintenance_password_file,
        )
        try:
            with validation_engine.connect() as connection:
                service.validate_schema(connection)
                restored_manifest = service.schema_manifest(connection)
                if service.schema_fingerprint(connection) != canonical_fingerprint:
                    differing_tables = sorted(
                        table_name
                        for table_name in set(canonical_manifest)
                        | set(restored_manifest)
                        if canonical_manifest.get(table_name)
                        != restored_manifest.get(table_name)
                    )[:12]
                    differing_components: dict[str, list[str]] = {}
                    for table_name in differing_tables:
                        canonical_table = canonical_manifest.get(table_name, {})
                        restored_table = restored_manifest.get(table_name, {})
                        if not isinstance(canonical_table, dict) or not isinstance(
                            restored_table, dict
                        ):
                            differing_components[table_name] = ["table"]
                            continue
                        differing_components[table_name] = sorted(
                            key
                            for key in set(canonical_table) | set(restored_table)
                            if canonical_table.get(key) != restored_table.get(key)
                        )
                    raise service.BackupValidationError(
                        "Restored archive schema differs from the canonical ODIN schema; "
                        f"components={differing_components}"
                    )
                inspector = service.inspect(connection)
                actual_tables = set(inspector.get_table_names(schema="public"))
                if actual_tables != set(service.canonical_tables()):
                    raise service.BackupValidationError("Restored archive has noncanonical public tables")
                if inspector.get_view_names(schema="public"):
                    raise service.BackupValidationError("Restored archive contains public views")
                service._validate_canonical_public_executables(connection)
        finally:
            validation_engine.dispose()
    finally:
        if canonical_archive is not None:
            canonical_archive.unlink(missing_ok=True)
        if created:  # Only a database this code created.
            with maintenance_engine.connect() as connection:
                restore_artifacts.drop_database(connection, validation_database)
        restore_artifacts.forget(paths.backups, validation_database)
        maintenance_engine.dispose()
    return {
        **metadata,
        "schema_fingerprint": canonical_fingerprint,
        "restore_validated": True,
    }


def _inspect_compatible_archive(archive,target_url,password_file,directory):
    from modules.system.pre026_restore_stage import identify_contract,archive_metadata
    if identify_contract(archive,target_url,password_file,directory) is None:
        return service.inspect_postgres_archive(archive,target_url,password_file=password_file,work_dir=directory,trusted_prevalidated=True)
    # Historical archives are validated in full by the preserved029 stage in prepared_archive.
    return archive_metadata(archive)


def validate_postgres_restore(archive,target_url,maintenance_url,*,password_file=None,maintenance_password_file=None,base_dir=None):
    """Validate an archive; a migration refusal is an actionable validation error, not a 500."""
    from core.schema.migrator import MigrationError
    try:
        return service._validate_postgres_restore(archive,target_url,maintenance_url,password_file=password_file,maintenance_password_file=maintenance_password_file,base_dir=base_dir)
    except MigrationError as error:
        raise service.BackupValidationError('PostgreSQL archive cannot be upgraded by this ODIN version: '+str(error)[:300]) from error


def _validate_postgres_restore(archive,target_url,maintenance_url,*,password_file=None,maintenance_password_file=None,base_dir=None):
    from modules.system.pre026_restore_stage import identify_contract,prepared_archive,archive_metadata,current_schema_version
    from modules.system import restore_artifacts
    directory=service.postgres_paths(base_dir).backups
    restore_artifacts.sweep_stale(directory,maintenance_url,maintenance_password_file)
    contract=identify_contract(archive,target_url,password_file,directory)
    if contract is None:
        return service._validate_current_postgres_restore(archive,target_url,maintenance_url,password_file=password_file,maintenance_password_file=maintenance_password_file,base_dir=base_dir)
    original=archive_metadata(archive);era=[]
    with prepared_archive(archive,target_url,maintenance_url,password_file,maintenance_password_file,directory,_era=era) as prepared:
        current=service._validate_current_postgres_restore(prepared,target_url,maintenance_url,password_file=password_file,maintenance_password_file=maintenance_password_file,base_dir=base_dir)
        return {**original,'restore_validated':True,'schema_fingerprint':current['schema_fingerprint'],'source_schema_version':era[0],'prepared_schema_version':current_schema_version(),'upgrade_required':True,'prepared_sha256':service._sha256(prepared)}


def stage_postgres_restore(
    candidate: service.Path,
    target_url: str,
    maintenance_url: str,
    *,
    actor_id: int | None = None,
    password_file: str | None = None,
    maintenance_password_file: str | None = None,
    base_dir: service.Path | None = None,
) -> dict[str, object]:
    paths = service.postgres_paths(base_dir)
    paths.backups.mkdir(parents=True, exist_ok=True)
    metadata = service.validate_postgres_restore(
        candidate,
        target_url,
        maintenance_url,
        password_file=password_file,
        maintenance_password_file=maintenance_password_file,
        base_dir=base_dir,
    )
    audit_engine = service.create_database_engine(
        target_url,
        role="backup",
        password_file=password_file,
    )
    try:
        with audit_engine.begin() as connection:
            connection.execute(
                service.text(
                    "INSERT INTO audit_logs (action, entity_type, details) "
                    "VALUES ('restore_staged', 'system', :details)"
                ),
                {
                    "details": service.json.dumps(
                        {
                            "actor_id": actor_id,
                            "candidate_sha256": metadata["sha256"],
                            "restart_required": True,
                        },
                        sort_keys=True,
                    )
                },
            )
    finally:
        audit_engine.dispose()
    with service.restore_lock(paths.lock):
        descriptor, filename = service.tempfile.mkstemp(
            prefix="restore-pending-", suffix=".dump", dir=paths.pending.parent
        )
        temporary = service.Path(filename)
        try:
            with service.os.fdopen(descriptor, "wb") as output, candidate.open("rb") as source:
                service.shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                service.os.fsync(output.fileno())
            service._secure(temporary)
            if service._sha256(temporary) != metadata["sha256"]:
                raise service.BackupValidationError("Staged PostgreSQL backup checksum changed")
            service.os.replace(temporary, paths.pending)
            manifest = {
                "schema_version": 1,
                "dialect": "postgresql",
                "state": "staged",
                "sha256": metadata["sha256"],
                "size_bytes": metadata["size_bytes"],
                "actor_id": actor_id,
                "staged_at": service.datetime.now(service.timezone.utc).isoformat(),
            }
            manifest_tmp = paths.manifest.with_suffix(".json.tmp")
            manifest_tmp.write_text(service.json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
            service._secure(manifest_tmp)
            service.os.replace(manifest_tmp, paths.manifest)
            service._fsync_directory(paths.pending.parent)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "restart_required": True,
        "candidate_sha256": metadata["sha256"],
        "restore_validated": True,
    }



# Bind after definitions so either the service or this module can be imported first.
from modules.system import postgres_backup_service as service
