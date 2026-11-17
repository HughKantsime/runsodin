"""Plate-clear confirmation that does not need a waiting job.

`POST /printers/{id}/plate-cleared` used to write nothing unless a job was in
waiting_plate_clear, so `plate_clear_confirmed_at` stayed empty on farms that clear plates
by hand. Confirmations use the job timeline when linked to a job, or one guarded
printer-owned system_config value for a print started at the printer.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import bindparam, text

from core.audit import log_audit
from core.printer_display_facts import plate_clear_key, stored_plate_clear_times
from core.printer_utils import is_printing_state
from core.rbac import check_org_access

_ENDED = "('completed','failed','cancelled')"


def as_utc(value) -> Optional[datetime]:
    """Stored times are naive UTC or ISO strings (SQLite); None when unreadable."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def select_plate_clear(db, printer_ids):
    """One selection shared by the read and writer, batched across printers.

    Select before applying reader visibility: hiding the first waiting job must
    never make the control advertise a different job or promise nothing starts.
    """
    ids = list(printer_ids)
    if not ids:
        return {}, {}, set()
    waiting = {}
    for row in db.execute(text(
            "SELECT id,printer_id FROM jobs WHERE printer_id IN :ids AND CAST(status AS TEXT)='waiting_plate_clear' "
            "ORDER BY priority ASC, (queue_position IS NULL) ASC, queue_position ASC, created_at ASC, id ASC")
            .bindparams(bindparam("ids", expanding=True)), {"ids": ids}):
        waiting.setdefault(row.printer_id, row.id)
    busy = set(db.execute(text(
        "SELECT printer_id FROM jobs WHERE printer_id IN :ids AND status IN ('printing','paused') "
        "UNION SELECT printer_id FROM print_jobs WHERE printer_id IN :ids AND status='running'")
        .bindparams(bindparam("ids", expanding=True)), {"ids": ids}).scalars())
    # Monitor end times decide linked prints; retain operator-finished jobs when
    # the monitor closed their print with an unknown end (the B11 selection).
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- static status list; printer ids are bound
    recent = db.execute(text(
        f"SELECT p.printer_id,p.id,p.ended_at,p.id,p.scheduled_job_id FROM print_jobs p WHERE p.printer_id IN :ids AND p.status IN {_ENDED} "
        "AND p.ended_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM education_submissions s "
        "WHERE s.job_id=p.scheduled_job_id AND s.archived_at IS NOT NULL) "
        "UNION ALL SELECT j.printer_id,j.id,j.actual_end,NULL,j.id FROM jobs j "
        f"WHERE j.printer_id IN :ids AND j.status IN {_ENDED} AND j.actual_end IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM print_jobs p WHERE p.scheduled_job_id=j.id AND p.ended_at IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.job_id=j.id AND s.archived_at IS NOT NULL)").bindparams(bindparam("ids", expanding=True)),
        {"ids": ids}).all()
    latest = {}
    for pid, identifier, raw, print_id, job_id in recent:
        ended = as_utc(raw)
        if ended is None:
            continue
        candidate = (ended, identifier, print_id is not None, print_id, job_id)
        if pid not in latest or candidate[:3] > latest[pid][:3]:
            latest[pid] = candidate
    return waiting, latest, busy


def current_plate_confirmations(db, printer_ids, actor):
    """Current confirmations for the writer and physical state, without exposing job details."""
    from core.controlled_parts import any_read_restrictions
    from core.rbac import get_org_scope
    from modules.printers.integration_floor import visible_clauses

    ids = list(printer_ids)
    if not ids:
        return {}
    clauses, params = visible_clauses(actor, get_org_scope(actor), any_read_restrictions(db), 'j')
    known = {pid: as_utc(at) for pid, at in stored_plate_clear_times(db, ids, actor).items()}
    # Scope hides names, not the physical bed state.
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- source-owned visibility clauses; every value is bound
    rows = db.execute(text(
        "SELECT e.printer_id,e.occurred_at FROM job_timeline_events e JOIN jobs j ON j.id=e.job_id "
        "WHERE e.printer_id IN :ids AND e.kind='bed_clear_confirmed' AND " + ' AND '.join(clauses))
        .bindparams(bindparam('ids', expanding=True)), {**params, 'ids': ids})
    for pid, raw in rows:
        at = as_utc(raw)
        if at is not None and (known.get(pid) is None or at > known[pid]):
            known[pid] = at
    starts = db.execute(text(
        "SELECT printer_id,actual_start FROM jobs WHERE printer_id IN :ids AND actual_start IS NOT NULL "
        "UNION ALL SELECT printer_id,started_at FROM print_jobs WHERE printer_id IN :ids AND started_at IS NOT NULL")
        .bindparams(bindparam('ids', expanding=True)), {'ids': ids})
    for pid, raw in starts:
        begun = as_utc(raw)
        if begun is not None and known.get(pid) is not None and begun > known[pid]:
            del known[pid]
    return known


def plate_clear_states(db, printers, actor):
    """Reader-owned job visibility and names; physical plate state needs no job name."""
    from core.controlled_parts import any_read_restrictions
    from core.display_names import display_names
    from core.printer_display_facts import may_read
    from core.rbac import get_org_scope
    from modules.printers.integration_floor import visible_clauses

    waiting, latest, busy = select_plate_clear(db, [p.id for p in printers])
    cleared = current_plate_confirmations(db, [p.id for p in printers], actor)
    visible = set()
    names = {}
    job_ids = set(waiting.values()) | {row[4] for row in latest.values() if row[4] is not None}
    if job_ids and may_read(actor, 'jobs'):
        clauses, params = visible_clauses(actor, get_org_scope(actor), any_read_restrictions(db), 'j')
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- source-owned visibility clauses; every value is bound
        visible = set(db.execute(text('SELECT j.id FROM jobs j WHERE j.id IN :ids AND ' + ' AND '.join(clauses))
            .bindparams(bindparam('ids', expanding=True)), {**params, 'ids': list(job_ids)}).scalars())
        names = display_names(db, 'job', [jid for jid in waiting.values() if jid in visible], actor,
                              visibility_checked=True)
    result = {}
    for printer in printers:
        last = latest.get(printer.id)
        ended = last[0] if last else None
        confirmed = as_utc(cleared.get(printer.id))
        job_id = waiting.get(printer.id)
        active = (printer.id in busy or is_printing_state(printer.gcode_state)
                  or str(printer.gcode_state or '').strip().upper() in ('PAUSE', 'PAUSED'))
        result[printer.id] = {
            'needed': not active and (job_id is not None or (ended is not None and (confirmed is None or confirmed < ended))),
            'has_waiting_job': job_id is not None,
            'releases_job': {'id': job_id, 'name': names[job_id]} if job_id in names else None,
            'last_print_ended_at': ended if last and (last[4] is None or last[4] in visible) else None,
        }
    return result


def record_plate_clear(db, printer, actor) -> Optional[datetime]:
    """Record the confirmation for `printer`; the caller commits.

    Returns its time, or None when there is nothing to confirm: the printer is printing or
    paused, it has no finished print, or that print belongs to another organization.
    """
    state = str(printer.gcode_state or "").strip().upper()
    if is_printing_state(printer.gcode_state) or state in ("PAUSE", "PAUSED"):
        return None
    if not check_org_access(actor, printer.org_id):
        return None
    _, latest, busy = select_plate_clear(db, [printer.id])
    if printer.id in busy or printer.id not in latest:
        return None
    ended, _, _, print_id, job_id = latest[printer.id]
    from core.controlled_parts import allowed
    org_id = printer.org_id
    if job_id is not None:
        job = db.execute(text("SELECT charged_to_org_id FROM jobs WHERE id=:id"), {"id": job_id}).first()
        if job is None or not check_org_access(actor, job[0]) or not allowed(db, "job", job_id, actor):
            return None
        org_id = job[0]
    known = current_plate_confirmations(db, [printer.id], actor).get(printer.id)
    if known is not None and known >= ended:
        return known
    now = datetime.now(timezone.utc)
    user_id = actor.get("id") if isinstance(actor, dict) and type(actor.get("id")) is int else None
    token_id = actor.get("_token_id") if isinstance(actor, dict) and type(actor.get("_token_id")) is int else None
    if job_id is not None:
        db.execute(text(
            "INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,actor_user_id,actor_token_id,occurred_at) "
            "VALUES(:id,:job,:printer,:org,'bed_clear_confirmed',:detail,:user,:token,:at)"),
            {"id": uuid.uuid4().hex, "job": job_id, "printer": printer.id, "org": org_id,
             "detail": json.dumps({"source": "plate_cleared", "waiting_job": False}),
             "user": user_id, "token": token_id, "at": now})
    else:
        # Use the stored creation value, as display facts do, to guard against reused ids.
        created_at = db.execute(text("SELECT created_at FROM printers WHERE id=:p"), {"p": printer.id}).scalar_one()
        db.execute(text("INSERT INTO system_config(key,value) VALUES(:key,:value) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"),
                   {"key": plate_clear_key(printer.id), "value": json.dumps({"at": now.isoformat(),
                    "print_job_id": print_id, "org_id": org_id, "printer_created_at": str(created_at)})})
    log_audit(db, "printer.plate_clear_confirmed", "printer", printer.id,
              {"printer_id": printer.id, "print_job_id": print_id, "job_id": job_id,
               "actor_token_id": token_id})
    return now
