"""Two-phase request-bound explicit AMS approval; no printer commands."""
import asyncio
from sqlalchemy import text
from core.errors import ErrorCode,OdinError
from modules.organizations.education_ams_request_auth import reauthenticate_teacher_request
from modules.organizations.education_ams_payload_authority import capture_payload_graph,PreparedAMSApproval
from modules.organizations.education_review_service import approve_submission
from modules.printers.services import inspect_ams_payload


async def approve_with_explicit_ams(request,*,db,principal,submission_id,body):
    try:
        # Existing command handling validates audience/digest and returns its
        # committed snapshot. It must run before submitted-only byte staging.
        existing=db.execute(text('SELECT 1 FROM education_commands WHERE org_id=:org AND actor_kind=\'user\' '
            'AND actor_id=:actor AND action=\'submission.approve\' AND command_id=:command'),
            {'org':principal['group_id'],'actor':str(principal['id']),'command':str(body.command_id)}).first()
        if existing:
            return approve_submission(db,submission_id=submission_id,body=body,principal=principal)
        graph=capture_payload_graph(db,submission_id=submission_id,revision=body.revision,principal=principal)
        db.rollback();db.close()
        facts=await asyncio.to_thread(inspect_ams_payload,graph.stored_path,expected_sha256=graph.sha256,
                                     expected_bytes=graph.storage_bytes,plate_index=body.plate_index)
        current=await reauthenticate_teacher_request(request,expected_principal=principal,db=db)
        return approve_submission(db,submission_id=submission_id,body=body,principal=current,
                                  prepared_ams=PreparedAMSApproval(graph,facts))
    except OdinError:raise
    except Exception:
        db.rollback()
        raise OdinError(ErrorCode.resource_conflict,'AMS selection or payload changed. Refresh and try again.',status=409) from None
