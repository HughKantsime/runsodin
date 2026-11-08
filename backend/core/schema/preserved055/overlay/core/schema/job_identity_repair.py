"""Repair lost SQLite job identity guards without changing migration checksums."""
from importlib import import_module
import logging
import shlex

from sqlalchemy.orm import Session

from core.schema.migrator import MigrationError

logger = logging.getLogger(__name__)


def repair_job_identity(connection):
    if connection.dialect.name != 'sqlite':
        return
    history = import_module('core.schema.migrations.055_controlled_history')
    # Take the writer lock before inspecting or repairing, including a missing row.
    connection.exec_driver_sql('UPDATE job_id_high_water SET high_water=high_water WHERE singleton=1')
    expected = history.job_id_triggers()
    actual = dict(connection.exec_driver_sql(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'jobs_high_water_%'").all())
    normalize = lambda sql: ''.join(sql.lower().split())
    different = sorted(name for name, sql in actual.items()
                       if name not in expected or normalize(sql) != normalize(expected[name]))
    if different:
        drops = ' '.join('DROP TRIGGER "' + name.replace('"', '""') + '";' for name in different)
        command = 'sqlite3 ' + shlex.quote(connection.engine.url.database) + ' ' + shlex.quote(drops)
        raise MigrationError(
            'Job identity high-water trigger differs: ' + ', '.join(different)
            + '. After reviewing the altered definitions, run: ' + command
            + '; then restart ODIN to recreate the packaged guards and reseed job IDs.')
    before = connection.exec_driver_sql(
        'SELECT high_water FROM job_id_high_water WHERE singleton=1').scalar_one_or_none()
    # The migration owns the complete FK/legacy-link inventory and packaged DDL.
    # Its upsert never lowers an existing high-water value.
    history.initialize_job_ids(connection)
    after = connection.exec_driver_sql(
        'SELECT high_water FROM job_id_high_water WHERE singleton=1').scalar_one()
    missing = sorted(set(expected) - set(actual))
    if missing or before != after:
        from core.audit import log_audit
        with Session(bind=connection) as db:
            log_audit(db, 'job_identity.repaired', 'system', details={
                'triggers': missing, 'previous_high_water': before, 'high_water': after})
            db.flush()
        logger.warning('Repaired SQLite job identity guards: triggers=%s, high_water=%s -> %s',
                       missing, before, after)
