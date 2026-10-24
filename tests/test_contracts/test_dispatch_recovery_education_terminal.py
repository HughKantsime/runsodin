"""Recovery against genuine Education admission/start/terminal lifecycle.
Only the existing synthetic dispatcher boundary is substituted; no hardware.
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.dispatch_recovery import capture, mutate
from modules.printers.routes_dispatch_recovery import HoldRequest, ReleaseRequest, configured_identity
from tests.test_contracts.test_dispatch_recovery_actions import ATTEST
from tests.test_contracts.test_education_dispatch_receipt_binding import admitted_education, actual_education_dispatch, review_db

ACTOR=dict(id=3,username='admin',role='admin',group_id=1,_auth_kind='bearer_jwt')

@pytest.mark.parametrize('terminal_path',['authorized','physical_only','lingering_claim'])
def test_education_release_requires_genuine_terminal_and_retired_reservation(admitted_education,terminal_path):
    from modules.organizations.education_policy import terminal_monitor_observation
    engine,uploaded,name,start=admitted_education
    observed_id=start()
    assert observed_id is not None
    identity=lambda conn,pid=9:configured_identity(pid,conn)
    with Session(engine) as db:
        snapshot=capture(db,9,ACTOR,identity)
        held=mutate(db,9,ACTOR,HoldRequest(attempt_handle=snapshot['attempt'],snapshot_handle=snapshot['snapshot'],reason_category='investigation'),'hold',identity)
        assert held['held']
        if terminal_path=='physical_only':
            db.execute(text('UPDATE education_submissions SET lifecycle_revision=lifecycle_revision+1 WHERE id=:id'),{'id':uploaded['id']})
        if terminal_path=='lingering_claim':
            # Physical-only/manual terminal edits cannot replace the authorized
            # lifecycle: reservation and its authority remain outstanding.
            db.execute(text("UPDATE jobs SET status='completed',actual_end=CURRENT_TIMESTAMP WHERE id=:id"),{'id':uploaded['job_id']})
            db.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=:id"),{'id':observed_id})
            db.execute(text("UPDATE education_submissions SET status='completed' WHERE id=:id"),{'id':uploaded['id']})
        else:
            terminal=terminal_monitor_observation(db,print_job_id=observed_id,printer_id=9,terminal_status='completed',observed_filename=name)
            assert terminal['transitioned'] is (terminal_path=='authorized')
        db.execute(text("UPDATE printers SET last_seen=CURRENT_TIMESTAMP,gcode_state='IDLE' WHERE id=9"))
        db.commit()
        snapshot=capture(db,9,ACTOR,identity)
        request=ReleaseRequest(attempt_handle=snapshot['attempt'],snapshot_handle=snapshot['snapshot'],reason_category='verified_terminal_reconciled',attestation=ATTEST)
        if terminal_path=='authorized':
            released=mutate(db,9,ACTOR,request,'release',identity)
            assert released['outcome']=='released' and released['held']
            assert db.execute(text('SELECT count(*) FROM dispatch_attempt_receipts')).scalar_one()==1
            assert db.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==1
        else:
            with pytest.raises(HTTPException) as failure:
                mutate(db,9,ACTOR,request,'release',identity)
            assert failure.value.status_code==409
            if terminal_path=='lingering_claim': assert failure.value.detail['code']=='education_authority_unresolved'
            assert db.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one()==1
        assert db.execute(text('SELECT hold FROM jobs WHERE id=:id'),{'id':uploaded['job_id']}).scalar_one()==1
