"""Actual scheduler eligibility must agree with charged-tenant dispatch authority."""
from datetime import datetime, timezone
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db


def _is_assignment(statement):
    """The scheduler's guarded assignment UPDATE (issued by modules.jobs.state_machine)."""
    sql = str(statement)
    return sql.startswith("UPDATE jobs SET status=:sm_to") and "printer_dispatch_claims dc" in sql

@pytest.mark.parametrize('tenant', [None, 2])
def test_private_printer_does_not_schedule_uncharged_or_foreign_tenant(review_db, monkeypatch, tenant):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,duration_hours,hold,priority) VALUES(901,'Synthetic tenant scheduling','pending',:tenant,1,1,FALSE,3)"), {'tenant':tenant})
    review_db.commit()
    result = Scheduler(SchedulerConfig(horizon_days=1)).run(review_db, start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.scheduled_count == 0
    assert review_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=901')).one() == ('pending', None)

@pytest.mark.parametrize('scenario', ['own', 'global', 'shared'])
def test_authorized_printer_remains_schedulable(review_db, monkeypatch, scenario):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    tenant = 1 if scenario == 'own' else 2
    if scenario == 'global':
        review_db.execute(text('DELETE FROM education_cost_center_printers'))
        review_db.execute(text('UPDATE printers SET org_id=NULL'))
    elif scenario == 'shared':
        review_db.execute(text('UPDATE printers SET shared=TRUE'))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,duration_hours,hold,priority) VALUES(901,'Synthetic authorized scheduling','pending',:tenant,1,1,FALSE,3)"), {'tenant':tenant})
    review_db.commit()
    result = Scheduler(SchedulerConfig(horizon_days=1)).run(review_db, start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.scheduled_count == 1
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'scheduled'

@pytest.mark.parametrize('column,value', [('shared',2), ('shared','false'), ('is_active',2), ('is_active','true')])
def test_malformed_permission_flags_are_not_schedulable(review_db, monkeypatch, column, value):
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    review_db.execute(text(f'UPDATE printers SET {column}=:value'), {'value':value})
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,duration_hours,hold,priority) VALUES(901,'Synthetic invalid authority','pending',1,1,1,FALSE,3)"))
    review_db.commit()
    result = Scheduler(SchedulerConfig(horizon_days=1)).run(review_db, start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.scheduled_count == 0
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'pending'

@pytest.mark.parametrize('change', ['owner', 'sharing', 'tenant', 'inactive'])
def test_assignment_rechecks_current_authority_after_candidate_selection(review_db, monkeypatch, change):
    from sqlalchemy.orm import Session
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    review_db.execute(text('DELETE FROM education_cost_center_printers'))
    review_db.execute(text('UPDATE printers SET shared=TRUE' if change == 'sharing' else 'UPDATE printers SET shared=FALSE'))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,duration_hours,hold,priority) VALUES(901,'Synthetic authority drift','pending',:tenant,1,1,FALSE,3)"), {'tenant':2 if change == 'sharing' else 1})
    review_db.commit()
    original = Session.execute
    observed = []
    def drift(session, statement, *args, **kwargs):
        if not observed and _is_assignment(statement):
            observed.append(True)
            command = {'owner':'UPDATE printers SET org_id=2', 'sharing':'UPDATE printers SET shared=FALSE',
                       'tenant':'UPDATE jobs SET charged_to_org_id=2 WHERE id=901', 'inactive':'UPDATE printers SET is_active=FALSE'}[change]
            original(session, text(command))
        return original(session, statement, *args, **kwargs)
    monkeypatch.setattr(Session, 'execute', drift)
    result = Scheduler(SchedulerConfig(horizon_days=1)).run(review_db, start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert observed == [True]
    assert result.scheduled_count == 0
    assert review_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=901')).one() == ('pending', None)

@pytest.mark.parametrize('state', ['preparing','adapter_entered','accepted','uncertain'])
def test_unresolved_dispatch_claim_excludes_printer_capacity(review_db, monkeypatch, state):
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    review_db.execute(text('UPDATE printers SET is_active=FALSE WHERE id!=9'))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,duration_hours,hold,priority) VALUES(901,'Synthetic pending print','pending',1,1,1,FALSE,3)"))
    review_db.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,owner_org_id,state,token) VALUES(9,902,:physical,:config,:hash,1,:state,:token)"),
                      {'physical':'a'*64,'config':'b'*64,'hash':'c'*64,'state':state,'token':'d'*32})
    review_db.commit()
    result=Scheduler(SchedulerConfig(horizon_days=1)).run(review_db, start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.scheduled_count == 0
    assert review_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=901')).one() == ('pending',None)
    assert review_db.execute(text('SELECT state FROM printer_dispatch_claims WHERE printer_id=9')).scalar_one() == state

def _insert_claim(executor, printer=9):
    executor.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,owner_org_id,state,token) VALUES(:printer,902,:physical,:config,:hash,1,'uncertain',:token)"),
                     {'printer':printer,'physical':'a'*64,'config':'b'*64,'hash':'c'*64,'token':'d'*32})

@pytest.mark.parametrize('claimed_printer', [9,10])
def test_claim_arriving_after_candidate_selection_rechecks_selected_printer(review_db, monkeypatch, claimed_printer):
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    from sqlalchemy.orm import Session
    review_db.execute(text('UPDATE printers SET is_active=FALSE WHERE id!=9'))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,duration_hours,hold,priority) VALUES(901,'Synthetic late claim','pending',1,1,1,FALSE,3)"))
    review_db.commit()
    original=Session.execute
    observed=[]
    def execute(session,statement,*args,**kwargs):
        if not observed and _is_assignment(statement):
            observed.append(True)
            class Executor:
                def execute(self,*args,**kwargs):return original(session,*args,**kwargs)
            _insert_claim(Executor(),claimed_printer)
        return original(session,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',execute)
    result=Scheduler(SchedulerConfig(horizon_days=1)).run(review_db,start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert observed == [True]
    assert result.scheduled_count == int(claimed_printer != 9)
    assert review_db.execute(text('SELECT state FROM printer_dispatch_claims WHERE printer_id=:id'),{'id':claimed_printer}).scalar_one()=='uncertain'

@pytest.mark.parametrize('claimed_printer',[9,10])
def test_education_final_schedule_preserves_approval_when_selected_printer_claimed(review_db, claimed_printer):
    from modules.organizations.education_policy import advance_schedule
    review_db.execute(text("UPDATE jobs SET status='pending',printer_id=9 WHERE id=13"))
    review_db.execute(text("UPDATE education_submissions SET status='pending',approved_printer_id=9,approved_by=2 WHERE id=14"))
    _insert_claim(review_db,claimed_printer)
    review_db.commit()
    changed=advance_schedule(review_db,submission_id=14,job_id=13,printer_id=9,expected_revision=1)
    assert changed is (claimed_printer != 9)
    expected='scheduled' if changed else 'pending'
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=13')).scalar_one()==expected
    assert review_db.execute(text('SELECT status,approved_printer_id,lifecycle_revision FROM education_submissions WHERE id=14')).one()==(expected,9,2 if changed else 1)
    assert review_db.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one()=='uncertain'

@pytest.mark.parametrize('foreign_resource', ['model','primary_file','explicit_file'])
def test_private_payload_owner_must_match_charged_tenant_before_scheduling(review_db, foreign_resource):
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    review_db.execute(text('INSERT INTO models(id,name,org_id) VALUES(51,\'Synthetic private payload\',:owner)'), {'owner':2 if foreign_resource=='model' else 1})
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(52,51,'synthetic.gcode','/owned/synthetic.gcode',:owner)"), {'owner':2 if foreign_resource=='primary_file' else 1})
    review_db.execute(text('UPDATE models SET print_file_id=52 WHERE id=51'))
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(53,51,'alternate.gcode','/owned/alternate.gcode',2)"))
    review_db.execute(text("INSERT INTO jobs(id,model_id,print_file_id,item_name,status,charged_to_org_id,quantity,duration_hours,hold,priority) VALUES(901,51,:file,'Synthetic private payload job','pending',1,1,1,FALSE,3)"), {'file':53 if foreign_resource=='explicit_file' else None})
    review_db.commit()
    result=Scheduler(SchedulerConfig(horizon_days=1)).run(review_db,start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.scheduled_count == 0
    assert review_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=901')).one()==('pending',None)

@pytest.fixture
def payload_job(review_db):
    review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(51,'Synthetic owned payload',1)"))
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(52,51,'synthetic.gcode','/owned/synthetic.gcode',1)"))
    review_db.execute(text('UPDATE models SET print_file_id=52 WHERE id=51'))
    review_db.execute(text("INSERT INTO jobs(id,model_id,item_name,status,charged_to_org_id,quantity,duration_hours,hold,priority) VALUES(901,51,'Synthetic payload job','pending',1,1,1,FALSE,3)"))
    review_db.commit()
    return review_db

@pytest.mark.parametrize('scenario',['own','global','missing_model','missing_primary','missing_explicit','foreign_alias','operation_alias'])
def test_scheduler_payload_reference_and_alias_authority(payload_job,scenario):
    from modules.jobs.scheduler import Scheduler,SchedulerConfig
    db=payload_job
    if scenario=='global':
        db.execute(text('UPDATE models SET org_id=NULL WHERE id=51'))
        db.execute(text('UPDATE print_files SET org_id=NULL WHERE id=52'))
    elif scenario=='missing_model':
        # Deliberately represent a damaged legacy database on this owned fixture.
        # Restore enforcement before invoking any application code.
        db.rollback()
        with db.get_bind().connect() as connection:
            connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
            connection.commit()
            try:
                connection.execute(text('UPDATE jobs SET model_id=999 WHERE id=901'))
                connection.commit()
            finally:
                connection.rollback()
                connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                connection.commit()
            assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar_one()==1
    elif scenario=='missing_primary':db.execute(text('UPDATE models SET print_file_id=999 WHERE id=51'))
    elif scenario=='missing_explicit':db.execute(text('UPDATE jobs SET print_file_id=999 WHERE id=901'))
    elif scenario in ('foreign_alias','operation_alias'):
        db.execute(text("INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(55,51,'alias.gcode','/owned/alias.gcode',:owner)"),{'owner':2 if scenario=='foreign_alias' else 1})
        db.execute(text('UPDATE models SET print_file_id=55 WHERE id=51'))
        db.execute(text('UPDATE jobs SET print_file_id=52 WHERE id=901'))
        if scenario=='operation_alias':
            db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,final_path) VALUES('alias-operation',1,1,'committed','/owned/alias.gcode')"))
    db.commit()
    result=Scheduler(SchedulerConfig(horizon_days=1)).run(db,start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.scheduled_count==int(scenario in ('own','global'))

@pytest.mark.parametrize('change',['model_owner','file_owner','primary_pointer','explicit_pointer','path','new_fallback','operation_ownership'])
def test_scheduler_payload_final_comparison_refuses_current_drift(payload_job,monkeypatch,change):
    from modules.jobs.scheduler import Scheduler,SchedulerConfig
    from sqlalchemy.orm import Session
    db=payload_job
    if change=='new_fallback':
        db.execute(text('UPDATE models SET print_file_id=NULL WHERE id=51'))
        db.execute(text('DELETE FROM print_files WHERE id=52'))
        db.commit()
    original=Session.execute
    observed=[]
    def execute(session,statement,*args,**kwargs):
        if not observed and _is_assignment(statement):
            observed.append(True)
            command={
                'model_owner':'UPDATE models SET org_id=2 WHERE id=51',
                'file_owner':'UPDATE print_files SET org_id=2 WHERE id=52',
                'primary_pointer':'UPDATE models SET print_file_id=NULL WHERE id=51',
                'explicit_pointer':'UPDATE jobs SET print_file_id=52 WHERE id=901',
                'path':"UPDATE print_files SET stored_path='/owned/replaced.gcode' WHERE id=52",
                'new_fallback':"INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(55,51,'new.gcode','/owned/new.gcode',2)",
                'operation_ownership':"INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,final_path) VALUES('late-operation',1,1,'committed','/owned/synthetic.gcode')",
            }[change]
            original(session,text(command))
        return original(session,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',execute)
    result=Scheduler(SchedulerConfig(horizon_days=1)).run(db,start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert observed==[True] and result.scheduled_count==0
    assert db.execute(text('SELECT status,printer_id FROM jobs WHERE id=901')).one()==('pending',None)

@pytest.mark.parametrize('target,value', [('model',0),('model',-1),('model','invalid'),('file',0),('file',-1),('file','invalid'),('tenant',0),('tenant',-1),('tenant','invalid')])
def test_malformed_raw_payload_owners_refuse_scheduling(payload_job,target,value):
    from modules.jobs.scheduler import Scheduler,SchedulerConfig
    db=payload_job
    # This owned fixture deliberately represents legacy corrupt owner values.
    db.rollback()
    with db.get_bind().connect() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
        connection.commit()
        try:
            statement={'model':'UPDATE models SET org_id=:value WHERE id=51',
                       'file':'UPDATE print_files SET org_id=:value WHERE id=52',
                       'tenant':'UPDATE jobs SET charged_to_org_id=:value WHERE id=901'}[target]
            connection.execute(text(statement),{'value':value})
            connection.commit()
        finally:
            connection.rollback()
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            connection.commit()
        assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar_one()==1
    result=Scheduler(SchedulerConfig(horizon_days=1)).run(db,start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.scheduled_count==0
    assert db.execute(text('SELECT status,printer_id FROM jobs WHERE id=901')).one()==('pending',None)
