"""Claim one summary intent inside the physical terminal transition transaction."""
import json
from sqlalchemy import text
from core.dispatch_observation_executor import TransactionView


_DEFAULT_JOB = object()


def claim_terminal(executor, print_job_id, *, deliver_alerts=True,
                   alert_job_id=_DEFAULT_JOB, notify_cancelled=False):
    db = TransactionView(executor)
    row = db.execute(text("SELECT p.economic_uid,p.economic_birth_nonce,p.scheduled_job_id,p.printer_id,p.status,j.charged_to_org_id FROM print_jobs p LEFT JOIN jobs j ON j.id=p.scheduled_job_id WHERE p.id=:id"), {'id':print_job_id}).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
    if row is None or row[4] not in ('completed','failed','cancelled'):
        return False
    uid, birth, job, printer, outcome, org = row
    # Historical/unlinked attempts have no economic identity. Never invent one
    # from a reusable integer ID, or attach their outcome to a newer attempt.
    if uid is None or birth is None:
        return False
    claimed = db.execute(text("INSERT INTO job_summary_outbox(print_attempt_uid,attempt_birth,job_id,printer_id,org_id,outcome) VALUES(:uid,:birth,:job,:printer,:org,:outcome) ON CONFLICT(print_attempt_uid) DO NOTHING"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
        {'uid':uid,'birth':birth,'job':job,'printer':printer,'org':org,'outcome':outcome}).rowcount == 1
    if claimed and deliver_alerts and job is not None and (outcome in ('completed', 'failed') or notify_cancelled):
        # 034 has no kind CHECK; keep its historical migration unchanged.
        identifier = db.execute(text("INSERT INTO alert_outbox(job_id,alert_kind) VALUES(:job,:kind) RETURNING id"),
            {'job':job,'kind':'terminal:'+str(print_job_id)}).scalar_one()
        db.execute(text('UPDATE job_summary_outbox SET delivery_state=:state WHERE print_attempt_uid=:uid'),
                   {'uid':uid,'state':json.dumps({'alert_outbox_id':identifier,
                                              'alert_job_id':job if alert_job_id is _DEFAULT_JOB else alert_job_id})})
    return claimed



def terminal_event(executor, print_job_id):
    """Capture identity in the terminal transaction; enqueue only after its commit."""
    db=TransactionView(executor)
    row=db.execute(text('SELECT p.scheduled_job_id,p.economic_uid,p.printer_id,p.status,j.charged_to_org_id FROM print_jobs p LEFT JOIN jobs j ON j.id=p.scheduled_job_id WHERE p.id=:id'),{'id':print_job_id}).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
    if row is None or row[0] is None or row[3] not in ('completed','failed','cancelled'):return None
    from core.job_timeline import event
    return event(row[0],row[3],printer_id=row[2],print_attempt_uid=row[1],org_id=row[4])


def dispatch_terminal_alert(print_job_id, fallback, **payload):
    """Use the claimed durable intent, or preserve legacy/unidentified delivery."""
    from core.db import current_engine
    from modules.notifications.alert_outbox import dispatch
    engine = current_engine()
    with engine.begin() as connection:
        row = connection.execute(text("SELECT s.print_attempt_uid,s.delivery_state FROM job_summary_outbox s JOIN print_jobs p ON p.economic_uid=s.print_attempt_uid AND p.economic_birth_nonce=s.attempt_birth WHERE p.id=:id"), {'id':print_job_id}).first()
        if row is not None:
            state = json.loads(row[1])
            identifier = state.get('alert_outbox_id')
            if identifier is not None:
                # Preserve the monitor's existing text, resource scope and metadata.
                payload['job_id'] = state.get('alert_job_id',payload.get('job_id'))
                state['alert_payload'] = payload
                connection.execute(text('UPDATE job_summary_outbox SET delivery_state=:state WHERE print_attempt_uid=:uid'),
                                   {'uid':row[0],'state':json.dumps(state)})
    if row is None:
        return fallback(**payload)
    if identifier is not None:
        return dispatch(engine, identifier)
    # Education claims deliberately have no generic alert delivery intent.
