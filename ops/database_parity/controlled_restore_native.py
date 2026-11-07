"""Real PostgreSQL controlled-data backup/restore, run by the CV05 offline drill.

Uses the drill's owned database, application/maintenance credentials and real
pg_dump/pg_restore through the production backup service. No client doubles.
"""
# Raw job INSERT exemption: this PostgreSQL-only restore proof uses generated
# sequence IDs to verify that deleted, post-stage IDs are never issued again.
import os
from pathlib import Path
import tempfile
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database_config import create_database_engine
from core.schema import bootstrap_database
from modules.system import postgres_backup_service as backup
from modules.system.postgres_restore_identity import job_identity_floor

TABLES = (
    'controlled_audiences', 'controlled_audience_members', 'controlled_files',
    'controlled_jobs', 'controlled_job_tombstones', 'controlled_retention_clocks',
    'job_id_high_water',
)


def execute(connection, statement, **parameters):
    # All callers below supply fixture SQL or a TABLES allowlisted snapshot;
    # generated IDs and values are always bound separately.
    return connection.execute(text(statement), parameters)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- only local fixture SQL and TABLES allowlisted identifiers; values use bound parameters


def snapshot(engine):
    with engine.connect() as connection:
        return {
            name: sorted((tuple(row) for row in execute(connection, 'SELECT * FROM ' + name)), key=repr)
            for name in TABLES
        }


def seed(engine, tag):
    """Seed valid policies and create a tombstone via the production deletion path."""
    with engine.begin() as db:
        user = execute(db, "INSERT INTO users(username,password_hash,role,is_active) "
                       "VALUES(:name,'unused','operator',TRUE) RETURNING id", name=tag).scalar_one()
        audience = execute(db, 'INSERT INTO controlled_audiences(name) VALUES(:name) RETURNING id',
                           name=tag).scalar_one()
        execute(db, 'INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(:a,:u)', a=audience, u=user)
        file_id = execute(db, 'INSERT INTO print_files(filename) VALUES(:name) RETURNING id', name=tag+'.3mf').scalar_one()
        live_job = execute(db, "INSERT INTO jobs(item_name,status) VALUES(:name,'pending') RETURNING id", name=tag).scalar_one()
        deleted_job = execute(db, "INSERT INTO jobs(item_name,print_file_id,status) "
                              "VALUES(:name,:file,'completed') RETURNING id", name=tag+'-deleted', file=file_id).scalar_one()
        execute(db, "INSERT INTO controlled_files(file_id,label,audience_id,printer_tag,retention_days,reason,changed_by,marked_at) "
                "VALUES(:file,'CUI',:a,'Secure',7,'native restore fixture',:u,'2026-01-01T00:00:00+00:00')",
                file=file_id, a=audience, u=user)
        execute(db, "INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,changed_by,marked_at) "
                "VALUES(:job,'CUI',:a,'Secure',14,'native restore fixture',:u,'2026-01-01T00:00:00+00:00')",
                job=live_job, a=audience, u=user)
        execute(db, "INSERT INTO controlled_retention_clocks(resource_type,resource_id,marked_at,initial_days,changes) "
                "VALUES('files',:file,'2026-01-01T00:00:00+00:00',7,'[]')", file=file_id)
        # PostgreSQL allocates with its sequence; the cross-provider table is
        # still archive data and must survive byte-for-byte as well.
        execute(db, 'INSERT INTO job_id_high_water(singleton,high_water) VALUES(1,:floor) '
                'ON CONFLICT(singleton) DO UPDATE SET high_water=EXCLUDED.high_water', floor=deleted_job)
    from core.controlled_deletion import redact_deleted_job_history
    with Session(engine) as db:
        redact_deleted_job_history(db, deleted_job)
        execute(db, 'DELETE FROM jobs WHERE id=:job', job=deleted_job)
        db.commit()
    return audience, file_id, live_job, deleted_job


def run():
    target = os.environ['DATABASE_URL']
    maintenance = os.environ['DATABASE_MAINTENANCE_URL']
    password = os.environ['DATABASE_PASSWORD_FILE']
    maintenance_password = os.environ['DATABASE_MAINTENANCE_PASSWORD_FILE']
    engine = create_database_engine(target, role='restore', password_file=password)
    assert engine.dialect.name == 'postgresql', 'This proof requires real PostgreSQL'
    tag = 'controlled-restore-' + uuid.uuid4().hex
    try:
        bootstrap_database(engine)
        audience, file_id, live_job, deleted_job = seed(engine, tag)
        before = snapshot(engine)
        assert before['controlled_job_tombstones'], 'Deletion must leave a real controlled tombstone'
        with engine.connect() as db:
            archived_floor = job_identity_floor(db)
        with tempfile.TemporaryDirectory(prefix='controlled-restore-') as directory:
            root = Path(directory); root.chmod(0o700)
            options = dict(password_file=password, maintenance_password_file=maintenance_password, base_dir=root)
            engine.dispose()  # Offline restore rejects idle pooled sessions too.
            candidate, metadata = backup.create_postgres_backup(target, maintenance_url=maintenance, **options)
            assert metadata['size_bytes'] > 100
            staged = backup.stage_postgres_restore(candidate, target, maintenance, **options)
            with engine.begin() as db:
                issued = [execute(db, "INSERT INTO jobs(item_name) VALUES(:name) RETURNING id", name=tag+'-later').scalar_one()
                          for _ in range(5)]
                execute(db, 'DELETE FROM jobs WHERE id BETWEEN :first AND :last', first=issued[0], last=issued[-1])
                assert job_identity_floor(db) == (issued[-1], True)
                assert issued[-1] > archived_floor[0]
                # Make all controlled collections observably differ from the archive.
                execute(db, 'DELETE FROM controlled_job_tombstones WHERE job_id=:job', job=deleted_job)
                execute(db, 'DELETE FROM controlled_jobs WHERE job_id=:job', job=live_job)
                execute(db, 'DELETE FROM controlled_files WHERE file_id=:file', file=file_id)
                execute(db, "DELETE FROM controlled_retention_clocks WHERE resource_type='files' AND resource_id=:file", file=file_id)
                execute(db, 'DELETE FROM controlled_audience_members WHERE audience_id=:a', a=audience)
                execute(db, "UPDATE controlled_audiences SET name=:name WHERE id=:a", name=tag+'-changed', a=audience)
                execute(db, 'UPDATE job_id_high_water SET high_water=:floor WHERE singleton=1', floor=issued[-1])
            assert snapshot(engine) != before
            engine.dispose()
            result = backup.apply_pending_postgres_restore(target, maintenance,
                acknowledgement=staged['candidate_sha256'], **options)
            assert result['status'] == 'restored'
            assert snapshot(engine) == before, 'Controlled archive data changed during restore'
            with engine.begin() as db:
                assert job_identity_floor(db) == (issued[-1], True)
                next_id = execute(db, "INSERT INTO jobs(item_name) VALUES(:name) RETURNING id", name=tag+'-after').scalar_one()
                assert next_id == issued[-1] + 1
                assert execute(db, 'SELECT count(*) FROM jobs WHERE id=:job', job=deleted_job).scalar_one() == 0
            assert bootstrap_database(engine)['applied'] == []
            assert snapshot(engine) == before
            with engine.begin() as db:
                assert execute(db, "INSERT INTO jobs(item_name) VALUES(:name) RETURNING id", name=tag+'-reboot').scalar_one() == next_id + 1
            assert not backup.postgres_paths(root).pending.exists()
            assert not backup.postgres_paths(root).manifest.exists()
        print('controlled-restore-native-pass marks audiences tombstones high-water sequence-floor reboot', flush=True)
    finally:
        engine.dispose()


if __name__ == '__main__':
    run()
