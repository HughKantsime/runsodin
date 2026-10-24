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
PRESERVED034=object()
PRESERVED035=object()
PRESERVED036=object()
PRESERVED037=object()
STAGE_SECONDS=3600
_LEDGER_COPY='COPY public.odin_schema_migrations (migration_id, checksum, dialect, applied_at) FROM stdin;'
_LEDGER_ROW=re.compile(r'([A-Za-z0-9_:./-]{1,255})\t([0-9a-f]{64})\t(sqlite|postgresql)\t[0-9: .+-]{10,40}')


def post027_migration_ids():
    from core.schema.bootstrap import POST027_MIGRATIONS
    return tuple(migration_id for migration_id,_module in POST027_MIGRATIONS)


def current_schema_version():
    ids=post027_migration_ids()
    return int(ids[-1].split(':')[1][:3]) if ids else 27


LEDGER_BYTE_CAP=4*1024*1024
LEDGER_SECONDS=300


def _pg_restore_lines(arguments,url,password_file,directory,*,limit):
    """Stream one pg_restore's stdout line by line; kill it past ``limit`` bytes or LEDGER_SECONDS."""
    import shutil,subprocess,threading
    from modules.system import postgres_backup_service as b
    executable=shutil.which('pg_restore')
    if not executable:raise b.RestoreEnvironmentError('Required PostgreSQL client is unavailable: pg_restore')
    environment=os.environ.copy();environment.pop('PGPASSFILE',None)
    environment['PGPASSWORD']=b._postgres_password(password_file)
    process=subprocess.Popen([executable,*arguments],env=environment,cwd=directory,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    expired=threading.Event()
    def expire():
        expired.set()
        try:process.kill()
        except OSError:pass
    timer=threading.Timer(LEDGER_SECONDS,expire);timer.daemon=True;timer.start()
    total=0;complete=False
    try:
        for raw in process.stdout:
            total+=len(raw)
            if total>limit:raise b.BackupValidationError('PostgreSQL archive migration ledger exceeds the supported size')
            try:yield raw.decode('utf-8','strict').rstrip('\n')
            except UnicodeDecodeError:raise b.BackupValidationError('PostgreSQL archive migration ledger is missing or malformed') from None
        complete=True
    finally:
        timer.cancel()
        if not complete and process.poll() is None:process.kill()
        process.stdout.close()
        try:returncode=process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill();returncode=process.wait()
    # A timeout is not a judgement of the archive: classify it as retryable.
    if expired.is_set():raise b.PostgreSQLClientFailed('PostgreSQL client operation timed out: pg_restore',retryable=True)
    if returncode:raise b.PostgreSQLClientFailed('PostgreSQL client operation failed: pg_restore',retryable=False)


def archive_ledger_ids(archive,url,password_file,directory):
    """Recorded migration ids from the archive's ledger data; strict streamed COPY parse, never executed."""
    from modules.system import postgres_backup_service as b
    malformed=b.BackupValidationError('PostgreSQL archive migration ledger is missing or malformed')
    ids=set();state='before'
    from contextlib import closing
    # closing(): an early refusal stops (kills) pg_restore at once, not when the generator is collected.
    with closing(_pg_restore_lines(['--data-only','--table=odin_schema_migrations','--file','-',str(archive)],url,password_file,directory,limit=LEDGER_BYTE_CAP)) as lines:
        for line in lines:
            if state=='before':
                if line.startswith('COPY '):
                    if line!=_LEDGER_COPY:raise malformed
                    state='rows'
            elif state=='rows':
                if line=='\\.':state='after';continue
                match=_LEDGER_ROW.fullmatch(line)
                if not match or match.group(1) in ids:raise malformed
                ids.add(match.group(1))
            elif line.startswith('COPY '):raise malformed
    if state!='after':raise malformed
    return frozenset(ids)


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
    from core.schema import preserved034, preserved035, preserved036, preserved037
    if ids == preserved034.ledger_ids():return PRESERVED034
    if ids == preserved035.ledger_ids():return PRESERVED035
    if ids == preserved036.ledger_ids():return PRESERVED036
    if ids == preserved037.ledger_ids():return PRESERVED037
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
    from core.schema import preserved029, preserved034, preserved035, preserved036, preserved037
    contract=identify_contract(archive,target_url,password_file,directory)
    if contract is None:
        yield archive
        return
    preserved = preserved037 if contract is PRESERVED037 else preserved036 if contract is PRESERVED036 else preserved035 if contract is PRESERVED035 else preserved034 if contract is PRESERVED034 else preserved029
    maintenance_password_file=b._require_distinct_maintenance_secret(password_file,maintenance_password_file)
    original_sha=b._sha256(archive);original_size=archive.stat().st_size
    name='odin_validate_'+uuid.uuid4().hex[:20]
    _,cli=b._maintenance_urls(maintenance_url,target_url,name)
    from modules.system import restore_artifacts as artifacts
    artifacts.sweep_stale(directory,maintenance_url,maintenance_password_file)
    maintenance=create_database_engine(maintenance_url,role='restore',password_file=maintenance_password_file).execution_options(isolation_level='AUTOCOMMIT')
    engine=None;created=False;temporary=[]
    try:
        artifacts.record(directory,name)
        with maintenance.connect() as c:c.exec_driver_sql('CREATE DATABASE "'+name+'"');created=True
        url=make_url(maintenance_url).set(database=name).render_as_string(hide_password=True)
        engine=create_database_engine(url,role='restore',password_file=maintenance_password_file)
        with engine.connect() as c:identity=tuple(c.execute(text('SELECT oid,datdba FROM pg_database WHERE datname=current_database()')).one())
        try:
            era=preserved.restore_into_stage(engine,name,archive=archive,archive_sha256=original_sha,target_url=target_url,
                password_file=password_file,maintenance_url=maintenance_url,maintenance_password_file=maintenance_password_file,
                directory=directory,journal=artifacts.journal_dir(directory),
                drop=lambda names:artifacts.drop_databases(maintenance_url,maintenance_password_file,names),
                existing=lambda:artifacts.existing_databases(maintenance_url,maintenance_password_file),
                deadline=monotonic()+STAGE_SECONDS)
        except (preserved029.Preserved029Refused, preserved034.Preserved034Refused, preserved035.Preserved035Refused, preserved036.Preserved036Refused, preserved037.Preserved037Refused) as refusal:
            # The child's exit code says whether the preserved sources judged the archive.
            detail=(refusal.detail or '')[:300]
            if refusal.category=='archive':raise b.ArchiveRefused(detail or 'Historical PostgreSQL archive refused ('+refusal.kind+')') from None
            if refusal.category=='retry':raise b.PostgreSQLClientFailed('Historical PostgreSQL archive stage hit a retryable failure'+(': '+detail if detail else ''),retryable=True) from None
            raise b.RestoreEnvironmentError('Historical PostgreSQL archive stage could not run ('+refusal.kind+(': '+detail if detail else '')+')') from None
        except (preserved029.Preserved029Error, preserved034.Preserved034Error, preserved035.Preserved035Error, preserved036.Preserved036Error, preserved037.Preserved037Error) as error:
            # Parent-side refusals are source-authored text (never driver output).
            if str(error).startswith(('Preserved029 stage ledger differs','Preserved034 stage ledger differs','Preserved035 stage ledger differs','Preserved036 stage ledger differs','Preserved037 stage ledger differs')):raise b.ArchiveRefused(str(error)) from None
            raise b.RestoreEnvironmentError('Historical PostgreSQL archive stage did not validate: '+str(error)[:200]) from None
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
            if created:  # Only a database this code created.
                with maintenance.connect() as c:artifacts.drop_database(c,name)
            artifacts.forget(directory,name)
        finally:
            maintenance.dispose()
            for path in temporary:path.unlink(missing_ok=True)
