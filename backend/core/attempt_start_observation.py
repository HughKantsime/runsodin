"""Observed-start callback transaction work; never commits or emits events."""
from dataclasses import dataclass
from sqlalchemy import text
from core.attempt_start_transaction import start_transaction
from core.attempt_start_plan import prepare_start,create_start,finalize_start
from core.dispatch_observation_executor import TransactionView
from core.interfaces.education_policy import is_education_reserved_filename
from core.order_production import prepare_job_write


@dataclass(frozen=True)
class ObservedStart:
    id:int
    linked_job_id:int|None
    fresh:bool
    education_sensitive:bool
    reserved:bool
    candidate_ids:tuple=()
    production_orders:tuple=()


def observe_start(executor,*,printer_id,filename,job_name,physical_values,source,
                  protocol=None,prepare_operational=False,allow_physical_resume=False):
    """Prepare/create/capture/finalize in caller's existing outer transaction.

    Operational matching parents can be preheld before printer/attempt locks.
    They never supply a financially authorized scheduled_job_id to the factory.
    """
    from core import auth
    from modules.archives.services import capture_attempt_spools
    with start_transaction(executor) as scope:
        scope.fence_dispatch_membership(executor,education=True)
        view=TransactionView(executor)
        if protocol is None:
            protocol=view.execute(text('SELECT api_type FROM printers WHERE id=:id'),{'id':printer_id}).scalar_one()
        transport=job_name if protocol=='bambu' else filename
        education_sensitive=any(is_education_reserved_filename(value) for value in (filename,job_name,transport))
        reserved=view.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE printer_id=:id'),{'id':printer_id}).first() is not None or (isinstance(transport,str) and 'odin-dispatch-' in transport.lower())
        candidates=parents=()
        if prepare_operational and not reserved and not education_sensitive:
            candidates=tuple(row[0] for row in view.execute(text("SELECT id FROM jobs WHERE printer_id=:id AND status IN ('scheduled','pending') AND NOT EXISTS(SELECT 1 FROM education_submissions s WHERE s.job_id=jobs.id) ORDER BY id"),{'id':printer_id}).all())
            parents=tuple(prepare_job_write(executor,candidates))
        plan=prepare_start(executor,scope,printer_id=printer_id,observed_filename=transport,
            protocol=protocol,observation_fields={'filename':filename,'job_name':job_name,'dispatch_key':auth.SECRET_KEY.encode()},capture_required=True)
        reserved=reserved or plan.reserved
        if plan.branch in ('resume','legacy_resume'):
            identifier=finalize_start(executor,scope,plan)
            return ObservedStart(identifier,plan.job_id,False,education_sensitive,reserved,candidates,parents)
        if allow_physical_resume and not reserved and not education_sensitive:
            # Preserve preexisting unreserved monitor tracking only, without any
            # birth, snapshot, identity or valuation repair. Education rows never
            # enter this fallback even when their current name is noncanonical.
            row=view.execute(text("SELECT p.id,p.scheduled_job_id FROM print_jobs p WHERE p.printer_id=:id AND p.status='running' AND NOT EXISTS(SELECT 1 FROM education_submissions s WHERE s.job_id=p.scheduled_job_id) ORDER BY p.id DESC LIMIT 1"),{'id':printer_id}).first()
            if row is not None:
                from modules.printers.dispatch_admission import _lock
                _lock(view,'print_jobs',row[0],'status')
                current=view.execute(text("SELECT status,scheduled_job_id FROM print_jobs WHERE id=:id"),{'id':row[0]}).first()
                if current is not None and current[0]=='running' and current[1]==row[1]:
                    return ObservedStart(row[0],None,False,False,False,candidates,parents)
        identifier=create_start(executor,scope,plan,physical_values)
        if not capture_attempt_spools(executor,identifier,printer_id,source=source,start_scope=scope,start_plan=plan):
            raise RuntimeError('Fresh observed-start snapshot refused')
        finalize_start(executor,scope,plan)
        return ObservedStart(identifier,plan.job_id,True,education_sensitive,reserved,candidates,parents)
