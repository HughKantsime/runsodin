"""Late deleted-job audits and final boot-time job identity guard validation."""
import asyncio
import importlib

import pytest
from sqlalchemy import text

from core.audit import log_audit
from core.controlled_audit import audit_allowed
from core.controlled_parts import (
    any_marks, any_read_restrictions, audit_visibility, tombstone_job_ids,
)
from core.controlled_redaction import redact_export
from core.schema import bootstrap_database
from core.schema.bootstrap import validate_schema
from core.schema.migrator import MigrationError
from tests.test_contracts.test_controlled_parts import actor, database
from tests.test_contracts.test_controlled_round8 import (
    clock_db, native_pg, add_job, add_rule, delete_job,
)
from tests.test_contracts.test_controlled_round12 import members


def test_terminal_audit_after_controlled_job_deletion(clock_db, pro_license):
    from modules.reporting.routes.exports import list_audit_logs, export_audit_logs_csv
    db = clock_db
    members(db)
    add_job(db, 5)
    add_rule(db, 'jobs', 5)
    db.execute(text("UPDATE jobs SET status='printing',printer_id=1 WHERE id=5"))
    db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) "
                    "VALUES(50,1,5,'Secret terminal part','running',CURRENT_TIMESTAMP)"))
    delete_job(db, 5)
    bound = db.execute(text('SELECT audit_max_id FROM controlled_job_tombstones')).scalar_one()
    db.commit()
    # The physical print finishes after its scheduled job has been deleted.
    db.execute(text("UPDATE print_jobs SET status='completed' WHERE id=50"))
    entry = log_audit(db, 'job.completed', 'job', 5, {'name': 'Secret terminal part'})
    db.flush()
    identifier = entry.id
    assert identifier > bound
    db.commit()
    for who, entitled in ((actor(), True), (actor(2), False)):
        assert audit_allowed(db, who, 'job', 5, identifier) is entitled
        clause, values = audit_visibility(who, db)
        visible = db.execute(text('SELECT id FROM audit_logs WHERE id=:late AND ' + clause),
                             dict(values, late=identifier)).scalars().all()
        assert visible == ([identifier] if entitled else [])
        args = dict(entity_type='job', action='job.completed', date_from=None,
                    date_to=None, q=None, db=db, current_user=who)
        result = list_audit_logs(limit=50, offset=0, **args)
        assert len(result['logs']) == 1
        assert (result['logs'][0]['details'] == '[controlled]') is not entitled
        assert len(list_audit_logs(limit=50, offset=0, **dict(args, q='Secret terminal'))['logs']) == int(entitled)
        response = export_audit_logs_csv(**args)
        async def body():
            return ''.join([part async for part in response.body_iterator])
        assert ('Secret terminal part' in asyncio.run(body())) is entitled
        exported = redact_export(db, who, [{'id': identifier, 'entity_type': 'job',
                                           'entity_id': 5, 'details': 'Secret terminal part'}])
        assert exported[0]['details'] == ('Secret terminal part' if entitled else '[controlled]')
    # A late audit alone must keep historical redaction and job identity relevant.
    db.execute(text('DELETE FROM print_jobs WHERE id=50'))
    db.execute(text('DELETE FROM audit_logs WHERE id<=:bound'), {'bound': bound})
    db.commit()
    assert not any_marks(db)
    assert any_read_restrictions(db)
    assert tombstone_job_ids(db) == {5}


@pytest.mark.parametrize('trigger', ['jobs_high_water_insert', 'jobs_high_water_delete'])
def test_final_schema_contract_rejects_altered_job_id_guards(database, trigger):
    db = database
    db.execute(text('DROP TRIGGER ' + trigger))
    migration = importlib.import_module('core.schema.migrations.055_controlled_history')
    db.execute(text(migration.job_id_triggers()[trigger].replace('max(high_water,', 'min(high_water,')))
    db.commit()
    with pytest.raises(MigrationError, match='Job identity high-water trigger differs'):
        validate_schema(db.connection())
    db.rollback()
    with pytest.raises(MigrationError, match='Job identity high-water trigger differs'):
        bootstrap_database(db.get_bind())
