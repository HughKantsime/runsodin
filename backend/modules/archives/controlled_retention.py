"""Bounded periodic retirement through the existing sealed lifetime engine."""
import logging
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.controlled_retention import expired, preview_acknowledged

log = logging.getLogger('odin.controlled_retention')


def purge_due(engine, root, *, limit=25):
    from modules.archives.timelapse_action_authority import controlled_retention_evidence
    from modules.archives.timelapse_lifetime_prepare import prepare_request_lifetime_retirement
    from modules.archives.timelapse_lifetime_action import retire_sealed_lifetime
    from core.timelapse_request_budget import VerificationBudget
    with Session(engine) as db:
        rows = db.execute(text("SELECT id,ownership_operation_id FROM timelapses "
                               "WHERE status IN ('ready','failed','deleting') ORDER BY created_at,id")).all()
        due = [(r.id, r.ownership_operation_id) for r in rows if expired(db, r.id) and preview_acknowledged(db, r.id)][:limit]
    complete = 0
    for identifier, nonce in due:
        if nonce is None:
            from modules.archives.controlled_legacy_retention import purge_legacy
            try:
                complete += int(purge_legacy(engine, root, identifier))
            except Exception:
                log.warning('Controlled legacy timelapse %s retirement unavailable', identifier)
            continue
        evidence = controlled_retention_evidence(engine, identifier, nonce)
        try:
            budget = VerificationBudget()
            prepare_request_lifetime_retirement(engine, root, identifier, evidence,
                                                expected_nonce=nonce, budget=budget)
            retire_sealed_lifetime(engine, root, identifier, evidence, expected_nonce=nonce, budget=budget)
            complete += 1
        except Exception as error:
            # The existing journal retains interrupted plans for the next pass.
            from core.audit import log_audit
            with Session(engine) as db:
                log_audit(db, 'controlled_part.purge_refused', 'timelapse', identifier,
                          {'reason': getattr(error, 'reason', 'retirement_unavailable')})
                db.commit()
            log.warning('Controlled timelapse %s retirement unavailable', identifier)
        finally:
            evidence.scope.clear()
    return complete


def retention_loop(engine, root, stop):
    """Independent worker; failed passes still wait for the next bounded tick."""
    while not stop.is_set():
        try:
            purge_due(engine, root)
        except Exception:
            log.exception('Controlled retention pass failed')
        stop.wait(300)


def start_worker(engine, root):
    import threading
    stop = threading.Event()
    worker = threading.Thread(target=retention_loop, args=(engine, root, stop), daemon=True, name='controlled-retention')
    worker.start()
    return stop
