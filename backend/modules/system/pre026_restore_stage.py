"""Historical PostgreSQL archives: preserved029 stage, then the ordinary current upgrade.

An archive is *current* when its ledger records every post-027 migration; the
ordinary path then validates it exactly. Any archive that records none of them (025, 026 or
027-era) is validated and imported by the preserved029 sources in an isolated child,
into a fresh stage database this process owns. Live sources then upgrade the stage
to the current schema and the resulting dump takes the ordinary current restore path.
Archives from an intermediate post-027 build are refused.
"""
from contextlib import contextmanager
from pathlib import Path
import os,re,tempfile,uuid
from time import monotonic
from sqlalchemy import inspect,text
from sqlalchemy.engine import make_url

PRESERVED029=object()
STAGE_SECONDS=3600
_LEDGER_COPY='COPY public.odin_schema_migrations (migration_id, checksum, dialect, applied_at) FROM stdin;'
_LEDGER_ROW=re.compile(r'([A-Za-z0-9_:./-]{1,255})\t([0-9a-f]{64})\t(sqlite|postgresql)\t[0-9: .+-]{10,40}')


def post027_migration_ids():
    from core.schema.bootstrap import POST027_MIGRATIONS
    return tuple(migration_id for migration_id,_module in POST027_MIGRATIONS)


def current_schema_version():
    ids=post027_migration_ids()
    return int(ids[-1].split(':')[1][:3]) if ids else 27


def archive_ledger_ids(archive,url,password_file,directory):
    """Recorded migration ids from the archive's ledger data; strict COPY parse, never executed."""
    from modules.system import postgres_backup_service as b
    sql=b._run_pg_tool(['pg_restore','--data-only','--table=odin_schema_migrations','--file','-',str(archive)],url,password_file,directory)
    lines=sql.splitlines()
    starts=[i for i,line in enumerate(lines) if line.startswith('COPY ')]
    if len(starts)!=1 or lines[starts[0]]!=_LEDGER_COPY:raise b.BackupValidationError('PostgreSQL archive migration ledger is missing or malformed')
    ids=set()
    for line in lines[starts[0]+1:]:
        if line=='\\.':return frozenset(ids)
        match=_LEDGER_ROW.fullmatch(line)
        if not match or match.group(1) in ids:raise b.BackupValidationError('PostgreSQL archive migration ledger is missing or malformed')
        ids.add(match.group(1))
    raise b.BackupValidationError('PostgreSQL archive migration ledger is missing or malformed')


def identify_contract(archive,url,password_file,directory):
    """None for a current archive, PRESERVED029 for a 025/026/027-era archive; else refuse."""
    from modules.system import postgres_backup_service as b
    from core.schema.preserved029 import ledger_ids
    if not archive.is_file() or not 100<=archive.stat().st_size<=b.MAX_BACKUP_BYTES:raise b.BackupValidationError('PostgreSQL backup size is outside the supported range')
    toc=b._run_pg_tool(['pg_restore','--list',str(archive)],url,password_file,directory)
    b._toc_inventory(toc)
    ids=archive_ledger_ids(archive,url,password_file,directory)
    post=set(post027_migration_ids())
    if post<=ids:return None # Current path validates tables, structure and schema exactly.
    if ids&post:
        raise b.BackupValidationError('PostgreSQL archive is from an intermediate post-027 build; restore it on that build or a matching release')
    if not ids<=ledger_ids():raise b.BackupValidationError('Unsupported PostgreSQL archive schema version or partial table inventory')
    return PRESERVED029


def archive_metadata(archive):
    from modules.system import postgres_backup_service as b
    return {'size_bytes':archive.stat().st_size,'sha256':b._sha256(archive)}


@contextmanager
def prepared_archive(archive,target_url,maintenance_url,password_file,maintenance_password_file,directory,*,_era=None):
    """Yield a current-format archive: the original, or a live-upgraded preserved029 stage dump."""
    from modules.system import postgres_backup_service as b
    from modules.system.postgres_canonical_executables import validate_executables
    from core.database_config import create_database_engine
    from core.schema import bootstrap_database
    from core.schema import preserved029
    contract=identify_contract(archive,target_url,password_file,directory)
    if contract is None:
        yield archive
        return
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
        with engine.connect() as c:identity=tuple(c.execute(text('SELECT oid,datdba FROM pg_database WHERE datname=current_database()')).one())
        try:
            era=preserved029.restore_into_stage(engine,name,archive=archive,archive_sha256=original_sha,target_url=target_url,
                password_file=password_file,maintenance_url=maintenance_url,maintenance_password_file=maintenance_password_file,
                directory=directory,deadline=monotonic()+STAGE_SECONDS)
        except preserved029.Preserved029Refused as refusal:
            if refusal.kind in ('BackupValidationError','MigrationError') and refusal.detail:raise b.BackupValidationError(refusal.detail) from None
            raise b.BackupValidationError('Historical PostgreSQL archive refused by the preserved029 stage ('+refusal.kind+')') from None
        except preserved029.Preserved029Error:
            raise b.BackupValidationError('Historical PostgreSQL archive stage did not validate') from None
        with engine.connect() as c:
            if tuple(c.execute(text('SELECT oid,datdba FROM pg_database WHERE datname=current_database()')).one())!=identity:raise b.BackupValidationError('Historical stage identity differs')
        bootstrap_database(engine) # Live sources: 027-era stage -> current schema.
        with engine.connect() as c:
            validate_executables(c)
            if set(inspect(c).get_table_names(schema='public'))!=set(b.canonical_tables()):raise b.BackupValidationError('Upgraded historical stage tables differ')
            ids={row[0] for row in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}
            if not set(post027_migration_ids())<=ids:raise b.BackupValidationError('Upgraded historical stage ledger differs')
        if _era is not None:_era.append(era)
        descriptor,path=tempfile.mkstemp(prefix='.odin-upgraded029-',suffix='.dump',dir=directory);os.close(descriptor);candidate=Path(path);temporary.append(candidate)
        b._run_pg_tool(['pg_dump','--format=custom','--schema','public','--no-owner','--no-privileges','--no-comments','--file',str(candidate),'--dbname',cli],maintenance_url,maintenance_password_file,directory)
        if b._sha256(archive)!=original_sha or archive.stat().st_size!=original_size:raise b.BackupValidationError('Original historical archive changed during preparation')
        yield candidate
    finally:
        if engine is not None:engine.dispose()
        try:
            if created:
                with maintenance.connect() as c:c.exec_driver_sql('DROP DATABASE IF EXISTS "'+name+'" WITH (FORCE)')
        finally:
            maintenance.dispose()
            for path in temporary:path.unlink(missing_ok=True)
