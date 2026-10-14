"""Short final dispatch admission transaction, with no device/network calls.

A committed admission is required before adapter entry. Commit uncertainty
never authorizes transport. Unresolved claims have no automatic expiration.
"""
from dataclasses import dataclass
import hmac
import re
from sqlalchemy import text,update,select
from sqlalchemy.sql import func
from core.dispatch_claim_schema import PrinterDispatchClaim
from core.dispatch_identity import build_identity,ownership_refusal


@dataclass(frozen=True)
class Admission:
    admitted: bool
    reason: str


class _Refused(RuntimeError):
    pass


def _lock(conn,table,identifier,column):
    # Only callers' constant table/column names reach this private function.
    statement=(f'SELECT 1 FROM {table} WHERE id=:id FOR UPDATE NOWAIT'
               if conn.dialect.name=='postgresql' else
               f'UPDATE {table} SET {column}={column} WHERE id=:id RETURNING 1')
    if conn.execute(text(statement),{'id':identifier}).first() is None:
        raise _Refused('Dispatch resource is no longer available')


def admit(engine, **inputs):
    """Confirm current authority and durably enter the adapter exactly once."""
    return _check_attempt(engine, already_admitted=False, **inputs)


def recheck_admitted(engine, **inputs):
    """Recheck the same admitted attempt without advancing or renewing its claim."""
    return _check_attempt(engine, already_admitted=True, **inputs)


def _check_attempt(engine,*,token,job,creds,key,payload_sha256,transport_filename,power_accepted,broker_connection=None,education_provider=None,education_reservation=None,already_admitted):
    """Revalidate captured facts and persist adapter entry on the same connection.

    Payload hash describes the owned adapter snapshot. The orchestrator must
    independently verify current source bytes, power and fresh idle acquisition.
    No database locks may be held during any of those operations.
    """
    from modules.printers import dispatch
    from core.order_production import prepare_job_write
    from core.registry import registry
    identity=build_identity(creds,key)
    if (identity is None or not isinstance(job,dict)
            or type(job.get('id')) is not int or type(job.get('printer_id')) is not int
            or not isinstance(token,str) or not re.fullmatch('[0-9a-f]{32}',token)
            or not isinstance(payload_sha256,str) or not re.fullmatch('[0-9a-f]{64}',payload_sha256)):
        return Admission(False,'Dispatch identity or payload could not be verified')
    try:
        with engine.begin() as conn:
            if job.get('education_submission_id') is not None:
                if (education_provider is None or education_reservation is None
                        or not education_provider.lock_dispatch_reservation(conn,reservation=education_reservation)):
                    raise _Refused('Education reservation or authority changed')
            else:
                if education_reservation is not None:
                    raise _Refused('Dispatch ownership changed')
                prepare_job_write(conn,[job['id']])
                _lock(conn,'jobs',job['id'],'status')
            _lock(conn,'printers',job['printer_id'],'is_active')
            _lock(conn,'models',job['model_id'],'name')
            _lock(conn,'print_files',job['resolved_print_file_id'],'stored_path')
            primary=job.get('model_print_file_id')
            if primary is not None and primary!=job['resolved_print_file_id']:
                _lock(conn,'print_files',primary,'stored_path')
            # Existing slot rows are protected from edits; the printer FOR UPDATE
            # lock also blocks new FK-referencing slots on PostgreSQL.
            if conn.dialect.name=='postgresql':
                conn.execute(text('SELECT id FROM filament_slots WHERE printer_id=:id ORDER BY id FOR UPDATE NOWAIT'),{'id':job['printer_id']}).all()
            current_job=dispatch._load_job(job['id'],connection=conn)
            current_creds=dispatch._get_printer_info(job['printer_id'],connection=conn)
            if current_job!=job:
                raise _Refused('Job, file, options or ownership changed before dispatch')
            current_identity=build_identity(current_creds,key)
            if current_identity is None or current_identity!=identity or current_creds.get('is_active') is not True:
                raise _Refused('Printer configuration, capability or ownership changed')
            ownership_error=ownership_refusal(current_job,current_creds)
            if ownership_error:raise _Refused(ownership_error)
            if job['status'] not in ('pending','scheduled') or job.get('hold') not in (None,False,0):
                raise _Refused('Job is held or no longer ready for dispatch')
            active=conn.execute(text("SELECT 1 FROM jobs WHERE printer_id=:id AND status IN ('printing','paused') LIMIT 1"),{'id':job['printer_id']}).first()
            physical=conn.execute(text("SELECT 1 FROM print_jobs WHERE printer_id=:id AND status IN ('running','printing','paused') LIMIT 1"),{'id':job['printer_id']}).first()
            state=conn.execute(text('SELECT gcode_state FROM printers WHERE id=:id'),{'id':job['printer_id']}).scalar_one()
            if active or physical or str(state or '').upper() in ('RUNNING','PRINTING','PAUSE','PAUSED','BUSY'):
                raise _Refused('Printer has an active physical or queued print')
            policy=education_provider or registry.get_provider('EducationPolicyProvider')
            if policy is None:raise _Refused('Education dispatch policy is unavailable')
            if job.get('education_submission_id') is not None:
                context=policy.authorize_dispatch(conn,job_id=job['id'],printer_id=job['printer_id'],expected_revision=job['education_lifecycle_revision'])
                if not context or not context.get('authorized'):raise _Refused('Education approval or entitlement changed')
                from modules.printers.education_compatibility import evaluate_education_compatibility
                import json
                facts=json.loads(job.get('compatibility_facts_json') or '{}')
                if not evaluate_education_compatibility(facts,current_creds)['compatible']:
                    raise _Refused('Education printer compatibility changed')
            else:
                policy.assert_generic_job_mutation_allowed(conn,job['id'])
                policy.assert_generic_model_reference_allowed(conn,job['model_id'])
                from core.model_preview_access import is_education_owned_file
                for identifier in set((job['resolved_print_file_id'],primary))-{None}:
                    path=conn.execute(text('SELECT stored_path FROM print_files WHERE id=:id'),{'id':identifier}).scalar_one()
                    if is_education_owned_file(conn,identifier,path):
                        raise _Refused('Education file requires its approval workflow')
            if current_creds.get('plug_configuration',{}).get('plug_type')=='mqtt' and current_creds['plug_configuration'].get('plug_auto_on'):
                # Settings writers update these same rows. Missing/new keys are
                # represented in the resulting snapshot and fail equality.
                if conn.dialect.name=='postgresql':
                    conn.execute(text("SELECT key FROM system_config WHERE key LIKE 'mqtt_republish_%' ORDER BY key FOR UPDATE NOWAIT")).all()
                from modules.notifications.mqtt_republish import get_power_connection_snapshot
                if broker_connection is None or get_power_connection_snapshot(connection=conn)!=broker_connection:
                    raise _Refused('Broker configuration changed before dispatch')
            table=PrinterDispatchClaim.__table__
            predicates = (table.c.token==token,
                table.c.state==('adapter_entered' if already_admitted else 'preparing'),
                table.c.printer_id==job['printer_id'],table.c.job_id==job['id'],table.c.physical_key==identity.target_key,
                table.c.configuration_key==identity.configuration_key,table.c.payload_sha256==payload_sha256,
                table.c.owner_org_id==creds.get('org_id'))
            if already_admitted:
                statement = select(table).where(*predicates)
                if conn.dialect.name == 'postgresql':
                    statement = statement.with_for_update(nowait=True)
                if conn.execute(statement).mappings().one_or_none() is None:
                    raise _Refused('Admitted dispatch claim changed; reconciliation is required')
                from core.dispatch_receipt_schema import DispatchAttemptReceipt
                from core.dispatch_receipts import filename_fingerprint, power_fingerprint
                receipt_table = DispatchAttemptReceipt.__table__
                receipt_statement = select(receipt_table).where(receipt_table.c.claim_token==token)
                if conn.dialect.name == 'postgresql':
                    receipt_statement = receipt_statement.with_for_update(nowait=True)
                receipt = conn.execute(receipt_statement).mappings().one_or_none()
                if (receipt is None or power_accepted is not True
                        or receipt['printer_id'] != job['printer_id'] or receipt['job_id'] != job['id']
                        or receipt['configuration_key'] != identity.configuration_key
                        or receipt['observed_print_job_id'] is not None
                        or not hmac.compare_digest(receipt['observed_filename_key'], filename_fingerprint(key,token,transport_filename))
                        or not hmac.compare_digest(receipt['prepared_power_key'], power_fingerprint(key,token,current_creds['plug_configuration'],broker_connection))):
                    raise _Refused('Original admitted receipt changed or printing was already observed')
            else:
                result=conn.execute(update(table).where(*predicates).values(state='adapter_entered',updated_at=func.now()))
                if result.rowcount!=1:raise _Refused('Dispatch claim changed; reconciliation may be required')
            if education_reservation is not None and transport_filename!=education_reservation['remote_filename']:
                raise _Refused('Education reservation filename differs')
            from core.dispatch_receipts import store_admission_receipt
            if not already_admitted:
                store_admission_receipt(conn,token=token,job=current_job,creds=current_creds,key=key,
                    configuration_key=current_identity.configuration_key,filename=transport_filename,
                    broker_connection=broker_connection,power_accepted=power_accepted)
        return Admission(True,'Admitted dispatch authority rechecked' if already_admitted else 'Dispatch admitted')
    except _Refused as exc:
        return Admission(False,str(exc))
    except Exception:
        return Admission(False,'Admitted dispatch authority could not be confirmed' if already_admitted else 'Final dispatch admission could not be confirmed; no command was sent')
