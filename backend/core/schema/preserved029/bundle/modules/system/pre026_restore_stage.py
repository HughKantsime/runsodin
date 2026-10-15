"""Private source-pinned025 data import followed by ordinary026 cutover."""
from contextlib import contextmanager
from pathlib import Path
import tempfile,os,uuid
from sqlalchemy import text
from sqlalchemy.engine import make_url
from core.schema.pre026_restore_contract import (PRE026_RESTORE025,PRE027_RESTORE026,inventory_for,check_sources,owned_stage_database,bootstrap_stage,validate_inventory,validate_postgres_history)


def identify_contract(archive,url,password_file,directory):
    from modules.system import postgres_backup_service as b
    if not archive.is_file() or not 100<=archive.stat().st_size<=b.MAX_BACKUP_BYTES:raise b.BackupValidationError('PostgreSQL backup size is outside the supported range')
    toc=b._run_pg_tool(['pg_restore','--list',str(archive)],url,password_file,directory)
    tables={v.split()[2] for v in b._toc_inventory(toc).structure if v.startswith('TABLE public ')}
    if tables==set(b.canonical_tables()):return None
    for contract in (PRE026_RESTORE025,PRE027_RESTORE026):
        if tables==set(inventory_for(contract)['tables']):
            check_sources(contract)
            b.validate_archive_toc(toc,allow_unresolved_structure=True,_contract=contract)
            return contract
    raise b.BackupValidationError('Unsupported PostgreSQL archive schema version or partial table inventory')


def missing_cutover_exclusions(c):
    return c.execute(text("SELECT identity_uid FROM cost_identities WHERE resource_kind='order' AND owner_scope='global' AND owner_key=0 EXCEPT SELECT order_uid FROM cost_global_origin_exclusions LIMIT 1")).first()


@contextmanager
def prepared_archive(archive,target_url,maintenance_url,password_file,maintenance_password_file,directory):
    from modules.system import postgres_backup_service as b
    from modules.system.postgres_canonical_executables import validate_schema_sql,validate_executables
    from core.database_config import create_database_engine
    from core.schema import bootstrap_database
    contract=identify_contract(archive,target_url,password_file,directory)
    if contract is None:
        yield archive
        return
    check_sources(contract)
    maintenance_password_file=b._require_distinct_maintenance_secret(password_file,maintenance_password_file)
    original_sha=b._sha256(archive);original_size=archive.stat().st_size
    name='odin_validate_'+uuid.uuid4().hex[:20]
    _,cli=b._maintenance_urls(maintenance_url,target_url,name)
    maintenance=create_database_engine(maintenance_url,role='restore',password_file=maintenance_password_file).execution_options(isolation_level='AUTOCOMMIT')
    engine=None;created=False;temporary=[]
    try:
        with maintenance.connect() as c:c.exec_driver_sql('CREATE DATABASE "'+name+'"');created=True
        url=make_url(maintenance_url).set(database=name).render_as_string(hide_password=True)
        engine=create_database_engine(url,role='restore',password_file=maintenance_password_file)
        stage=owned_stage_database(engine,name,contract)
        bootstrap_stage(stage)
        with engine.connect() as c:
            validate_inventory(c,contract)
            validate_postgres_history(c,contract)
        descriptor,path=tempfile.mkstemp(prefix='.odin-trusted025-',suffix='.dump',dir=directory);os.close(descriptor);canonical=Path(path);temporary.append(canonical)
        b._run_pg_tool(['pg_dump','--format=custom','--schema-only','--schema','public','--no-owner','--no-privileges','--no-comments','--file',str(canonical),'--dbname',cli],maintenance_url,maintenance_password_file,directory)
        toc=b._run_pg_tool(['pg_restore','--list',str(canonical)],maintenance_url,maintenance_password_file,directory)
        structure=b._toc_inventory(toc).structure
        b.inspect_postgres_archive(archive,target_url,password_file=password_file,work_dir=directory,allowed_structure=structure,_contract=contract)
        original_toc=b._run_pg_tool(['pg_restore','--list',str(archive)],target_url,password_file,directory)
        original_structure=b._toc_inventory(original_toc).structure
        original_schema=b._schema_sql_evidence(archive,target_url,password_file,directory,allowed_structure=structure,_contract=contract)
        canonical_schema=b._schema_sql_evidence(canonical,maintenance_url,maintenance_password_file,directory,schema_descriptors=original_structure,_contract=contract)
        validate_schema_sql(original_schema,canonical_schema)
        # Only allowlisted DATA enters this source-created25 schema; no adoption.
        b._restore_postgres_archive(archive,url,maintenance_password_file,directory,_contract=contract,_stage=stage)
        stage.verify()
        with engine.connect() as c:
            validate_inventory(c,contract)
            try:validate_postgres_history(c,contract)
            except Exception as e:
                from core.schema.migrator import MigrationError
                if not isinstance(e,MigrationError):raise
                raise b.BackupValidationError('Imported025 migration history differs from fixed packaged contract') from None
            validate_executables(c,_contract=contract)
        bootstrap_stage(stage) # All previously recorded source/data validators.
        bootstrap_database(engine) # Import precedes ordinary current27 upgrade.
        with engine.connect() as c:
            validate_executables(c)
            import importlib
            importlib.import_module('core.schema.migrations.026_global_order_origins').validate(c)
            if contract is PRE026_RESTORE025 and c.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one()!=0:raise b.BackupValidationError('Historical025 origin adoption refused')
            if contract is PRE026_RESTORE025 and missing_cutover_exclusions(c):raise b.BackupValidationError('Historical025 cutover exclusions differ')
        descriptor,path=tempfile.mkstemp(prefix='.odin-upgraded027-',suffix='.dump',dir=directory);os.close(descriptor);candidate=Path(path);temporary.append(candidate)
        b._run_pg_tool(['pg_dump','--format=custom','--schema','public','--no-owner','--no-privileges','--no-comments','--file',str(candidate),'--dbname',cli],maintenance_url,maintenance_password_file,directory)
        if b._sha256(archive)!=original_sha or archive.stat().st_size!=original_size:raise b.BackupValidationError('Original025 archive changed during preparation')
        check_sources(contract)
        yield candidate
    finally:
        if engine is not None:engine.dispose()
        try:
            if created:
                with maintenance.connect() as c:c.exec_driver_sql('DROP DATABASE IF EXISTS "'+name+'" WITH (FORCE)')
        finally:
            maintenance.dispose()
            for path in temporary:path.unlink(missing_ok=True)
