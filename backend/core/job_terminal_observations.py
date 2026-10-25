"""Claim one summary intent inside the physical terminal transition transaction."""
from sqlalchemy import text
from core.dispatch_observation_executor import TransactionView


def claim_terminal(executor, print_job_id):
    db = TransactionView(executor)
    row = db.execute(text("SELECT p.economic_uid,p.economic_birth_nonce,p.scheduled_job_id,p.printer_id,p.status,j.charged_to_org_id FROM print_jobs p LEFT JOIN jobs j ON j.id=p.scheduled_job_id WHERE p.id=:id"), {'id':print_job_id}).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
    if row is None or row[4] not in ('completed','failed','cancelled'):
        return False
    uid, birth, job, printer, outcome, org = row
    # Historical/unlinked attempts have no economic identity. Never invent one
    # from a reusable integer ID, or attach their outcome to a newer attempt.
    if uid is None or birth is None:
        return False
    return db.execute(text("INSERT INTO job_summary_outbox(print_attempt_uid,attempt_birth,job_id,printer_id,org_id,outcome) VALUES(:uid,:birth,:job,:printer,:org,:outcome) ON CONFLICT(print_attempt_uid) DO NOTHING"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
        {'uid':uid,'birth':birth,'job':job,'printer':printer,'org':org,'outcome':outcome}).rowcount == 1



def terminal_event(executor, print_job_id):
    """Capture identity in the terminal transaction; enqueue only after its commit."""
    db=TransactionView(executor)
    row=db.execute(text('SELECT p.scheduled_job_id,p.economic_uid,p.printer_id,p.status,j.charged_to_org_id FROM print_jobs p LEFT JOIN jobs j ON j.id=p.scheduled_job_id WHERE p.id=:id'),{'id':print_job_id}).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
    if row is None or row[0] is None or row[3] not in ('completed','failed','cancelled'):return None
    from core.job_timeline import event
    return event(row[0],row[3],printer_id=row[2],print_attempt_uid=row[1],org_id=row[4])
