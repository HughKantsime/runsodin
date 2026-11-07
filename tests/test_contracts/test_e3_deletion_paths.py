"""Every job retirement path protects retained audit history after source unlink."""
import asyncio
import pytest
from sqlalchemy import text
from tests.test_contracts.test_controlled_parts import database, actor
from tests.test_contracts.test_controlled_guards import marked


@pytest.mark.parametrize('path', ['bulk', 'retention', 'organization'])
def test_deleted_file_controlled_job_history(marked, monkeypatch, path):
    from core.audit import log_audit
    from modules.jobs.routes import jobs_crud
    from modules.organizations.education_policy import assert_generic_job_mutation_allowed
    from modules.reporting.routes.exports import list_audit_logs
    monkeypatch.setattr(jobs_crud, 'assert_generic_job_mutation_allowed', assert_generic_job_mutation_allowed)
    log_audit(marked, 'job.updated', 'job', 1, {'name':'Retained secret'})
    marked.commit()
    if path == 'bulk':
        result = asyncio.run(jobs_crud.bulk_update_jobs({'job_ids':[1], 'action':'delete'}, actor(), marked))
        assert result['affected'] == 1
    elif path == 'retention':
        from modules.system.routes_config import run_retention_cleanup
        marked.execute(text("UPDATE jobs SET status='completed',updated_at='2000-01-01' WHERE id=1"))
        marked.execute(text("INSERT INTO system_config(key,value) VALUES('data_retention',:value)"), {'value':'{"completed_jobs_days":1,"audit_logs_days":0,"timelapses_days":0,"alert_history_days":0}'})
        marked.commit()
        asyncio.run(run_retention_cleanup(actor(2), marked))
    else:
        from modules.jobs.services import delete_org_jobs
        # A job can belong to another org while using the retained marked file.
        marked.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id=1')); marked.commit()
        delete_org_jobs(marked, 2); marked.commit()
    assert marked.execute(text('SELECT id FROM jobs WHERE id=1')).first() is None
    result = list_audit_logs(limit=50,offset=0,entity_type=None,action=None,date_from=None,date_to=None,q=None,db=marked,current_user=actor(2))
    rows = [r for r in result['logs'] if r['action']=='job.updated']
    assert rows and rows[0]['details']=='[controlled]'
