"""Request-bound teacher report acquisition. No print or approval command."""
import asyncio
from core.db import SessionLocal
from core.errors import ErrorCode,OdinError
from modules.organizations.education_ams_request_auth import reauthenticate_teacher_request
from modules.organizations.education_ams_teacher_authority import lock_teacher_ams_context
from modules.printers.services import begin_ams_read,acquire_ams_report,persist_ams_report,retire_ams_read


async def acquire_teacher_ams_report(request,*,principal,submission_id,body):
    operation=None;network_task=None;commit_attempted=False
    try:
        with SessionLocal() as db:
            current=await reauthenticate_teacher_request(request,expected_principal=principal,db=db)
            context=lock_teacher_ams_context(db,principal=current,submission_id=submission_id,
                printer_id=body.printer_id,revision=body.revision)
            operation=begin_ams_read(db,printer_id=body.printer_id,expected_org_id=context.org_id)
            commit_attempted=True
            db.commit()
            commit_attempted=False
        # No caller session/transaction survives DNS, MQTT or worker cleanup.
        network_task=asyncio.create_task(asyncio.to_thread(acquire_ams_report,operation,
                                                        selected_slots=body.selected_slots))
        result=await asyncio.shield(network_task)
        with SessionLocal() as db:
            current=await reauthenticate_teacher_request(request,expected_principal=principal,db=db)
            after=lock_teacher_ams_context(db,principal=current,submission_id=submission_id,
                printer_id=body.printer_id,revision=body.revision)
            if after!=context:raise ValueError()
            response=persist_ams_report(db,operation=operation,result=result,selected_slots=body.selected_slots)
            commit_attempted=True
            db.commit()
        return response
    except BaseException as error:
        # A cancelled request waits for the bounded owned read to finish/reap.
        # Its late result never enters persistence. Repeated cancellation must
        # not abandon the owned thread while it is cleaning up its worker.
        if network_task is not None:
            while not network_task.done():
                try:await asyncio.shield(network_task)
                except asyncio.CancelledError:continue
                except Exception:break
            try:network_task.result()
            except BaseException:pass
        if operation is not None:
            try:
                with SessionLocal() as cleanup:
                    retire_ams_read(cleanup,operation=operation)
                    cleanup.commit()
            except Exception:pass
        if not isinstance(error,Exception):raise
        if commit_attempted:
            raise OdinError(ErrorCode.resource_conflict,'AMS read result is unconfirmed. Refresh before using it.',
                status=503,extra={'reason':'ams_read_unconfirmed'}) from None
        raise OdinError(ErrorCode.resource_conflict,'Selected AMS trays could not be verified. Refresh and try again.',
                        status=409,extra={'reason':'ams_read_unavailable'}) from None
