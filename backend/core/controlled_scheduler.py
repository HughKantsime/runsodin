"""Hard printer constraints also apply to scheduler candidates detached from ORM."""

def refusal(db, job_id, printer_id):
    from core.controlled_parts import dispatch_refusal
    if db is not None:
        return dispatch_refusal(db, job_id, printer_id)
    from core.db import SessionLocal
    with SessionLocal() as session:
        return dispatch_refusal(session, job_id, printer_id)
