"""
Timelapse Capture Daemon

Periodically captures JPEG frames from go2rtc camera streams for printers
with timelapse_enabled=True while they are actively printing. When a print
completes, stitches frames into an MP4 video using ffmpeg.
"""

import os
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass

import httpx
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from core.database_config import create_database_engine
from core.db_compat import sql

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [timelapse] %(levelname)s %(message)s",
)
log = logging.getLogger("odin.timelapse")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/odin.db")
from modules.printers.go2rtc_access import go2rtc_api_url, go2rtc_client_kwargs

GO2RTC_BASE = go2rtc_api_url()  # loopback in the single container; ODIN_GO2RTC_API_URL otherwise
TIMELAPSE_DIR = Path("/data/timelapses")
CAPTURE_INTERVAL = 30  # seconds between frames
FFMPEG_FPS = 30  # output video framerate

engine = create_database_engine(
    DATABASE_URL,
    role=os.getenv("ODIN_DB_ROLE", "timelapse"),
    password_file=os.getenv("DATABASE_PASSWORD_FILE"),
)
SessionLocal = sessionmaker(bind=engine)


def get_active_printers(session) -> list:
    """Get printers that have timelapse enabled and are currently printing."""
    from core.printer_utils import PRINTING_STATES, state_sql_in
    # Protocol-neutral: Bambu reports RUNNING, Moonraker/PrusaLink/Elegoo PRINTING.
    printing, params = state_sql_in("p.gcode_state", PRINTING_STATES, "state")
    rows = session.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: state_sql_in() over the PRINTING_STATES/IDLE_STATES module constants; states are bound
        f"""
        SELECT p.id, p.name
        FROM printers p
        WHERE p.timelapse_enabled IS TRUE
          AND p.is_active IS TRUE
          AND {printing}
    """), params).fetchall()
    return [{"id": r[0], "name": r[1]} for r in rows]


def get_active_job_for_printer(session, printer_id: int) -> dict | None:
    """Get the currently-running job for a printer."""
    row = session.execute(text("""
        SELECT id, item_name FROM jobs
        WHERE printer_id = :pid AND status = 'printing'
        ORDER BY actual_start DESC LIMIT 1
    """), {"pid": printer_id}).fetchone()
    if row:
        return {"id": row[0], "item_name": row[1]}
    return None


@dataclass(frozen=True)
class CaptureTicket:
    timelapse_id: int
    nonce: str
    job_id: int


def get_or_create_timelapse(session, printer_id: int, job_id: int, *, return_ticket=False):
    """Create media and its capture-time ownership in one confirmed transaction."""
    import uuid
    from core.timelapse_ownership_schema import anchors
    from modules.archives.ownership import capture_content_owner
    try:
        # Lock the selected job before its printer and recheck stale selection.
        job_lock = session.execute(text("UPDATE jobs SET id=id WHERE id=:jid"), {"jid":job_id})
        job = session.execute(text("SELECT print_file_id,model_id,printer_id,status FROM jobs WHERE id=:jid"), {"jid":job_id}).first()
        if job_lock.rowcount != 1 or job is None or job[2] != printer_id or job[3] != 'printing':
            raise RuntimeError("Capture job unavailable")
        # A portable parent write lock serializes creation for this printer.
        locked = session.execute(text("UPDATE printers SET id=id WHERE id=:pid"), {"pid":printer_id})
        if locked.rowcount != 1:
            raise RuntimeError("Capture printer unavailable")
        row = session.execute(text("""
            SELECT id,ownership_operation_id FROM timelapses
            WHERE printer_id=:pid AND print_job_id=:jid AND status='capturing'
            ORDER BY id LIMIT 1
        """), {"pid":printer_id,"jid":job_id}).first()
        if row:
            # Never adopt a legacy capture from today's printer/source graph.
            session.commit()
            return CaptureTicket(row[0],row[1],job_id) if return_ticket else row[0]
        if job:
            if job[1] is not None:
                session.execute(text("UPDATE models SET id=id WHERE id=:mid"), {"mid":job[1]})
            if job[0] is not None:
                session.execute(text("UPDATE print_files SET id=id WHERE id=:fid"), {"fid":job[0]})
            else:
                session.execute(text("UPDATE print_files SET id=id WHERE job_id=:jid"), {"jid":job_id})
        source = capture_content_owner(session.connection(),job_id)
        printer = session.execute(text("SELECT org_id,shared FROM printers WHERE id=:pid"), {"pid":printer_id}).one()
        value = printer[1]
        if type(value) is bool:shared = value
        elif type(value) is int and value in (0,1):shared = value == 1
        else:raise RuntimeError("Capture visibility unavailable")
        # capture_printer_shared records the printer's install-wide audience at
        # capture time: shared, or org-less on an install without organizations
        # (single-tenant: the install owns it). Multi-org installs are unchanged.
        # One observation of the tenancy mode decides both the owner state and the
        # stored capture-time fact (migration 033), so an organization created
        # between them cannot split the decision.
        from core import tenancy_mode
        has_orgs = tenancy_mode.install_has_organizations(session.connection())
        shared = tenancy_mode.printer_visible_install_wide(session.connection(), printer[0], shared, has_organizations=has_orgs)
        org = source.get('org_id')
        state = 'unresolved'
        if source.get('state') == 'known':
            if org is not None and (org == printer[0] or shared):state = 'known'
            elif org is None and shared and source.get('education_submission_id') is None:state = 'shared'
        nonce = uuid.uuid4().hex
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"printer_{printer_id}/{ts}_{nonce}.mp4"
        session.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragments from core.db_compat sql.* dialect helpers called with literal arguments (closed per-dialect output); values are bound
            f"""
            INSERT INTO timelapses(printer_id,print_job_id,filename,frame_count,status,created_at,ownership_operation_id)
            VALUES(:pid,:jid,:filename,0,'capturing',{sql.now()},:nonce)
        """), {"pid":printer_id,"jid":job_id,"filename":filename,"nonce":nonce})
        tid = session.execute(text("SELECT id FROM timelapses WHERE ownership_operation_id=:nonce"), {"nonce":nonce}).scalar_one()
        session.execute(anchors.insert(),{
            'operation_id':nonce,'timelapse_id':tid,'printer_id':printer_id,
            'scheduled_job_id':job_id,'filename':filename,'owner_state':state,
            'owner_org_id':org if state == 'known' else None,
            'ownership_source':'capture_unresolved' if state == 'unresolved' else 'first_capture_job_file',
            'print_file_id':source.get('print_file_id'),
            'education_submission_id':source.get('education_submission_id'),
            'capture_printer_org_id':printer[0],'capture_printer_shared':shared,
        })
        # The capture-time tenancy fact the audience rule reads (migration 033).
        from core import timelapse_capture_tenancy_schema as capture_tenancy
        capture_tenancy.record(session.connection(), nonce, not has_orgs)
        session.commit()
        return CaptureTicket(tid,nonce,job_id) if return_ticket else tid
    except Exception:
        session.rollback()
        raise RuntimeError("Timelapse creation could not be confirmed") from None


def capture_frame(printer_id: int, timelapse_id: int, *, nonce=None, job_id=None) -> bool:
    """Never infer a capture lifetime from a reusable numeric ID."""
    from modules.archives.timelapse_frame_capture import capture_owned_frame
    return capture_owned_frame(sessions=SessionLocal,root=TIMELAPSE_DIR,http=httpx,base=GO2RTC_BASE,
        printer_id=printer_id,timelapse_id=timelapse_id,nonce=nonce,job_id=job_id,
        auth=go2rtc_client_kwargs().get('auth'))


def encode_timelapse(timelapse_id: int, *, printer_id=None, nonce=None, job_id=None) -> bool:
    """Publish only the exact ticket's completed admitted frame manifest."""
    from modules.archives.timelapse_encode import encode_owned_timelapse
    return encode_owned_timelapse(sessions=SessionLocal,root=TIMELAPSE_DIR,
        timelapse_id=timelapse_id,printer_id=printer_id,nonce=nonce,job_id=job_id)


def finalize_stale_timelapses():
    """Copy expected tickets, then end the reader before encode/reconciliation."""
    from core.base import JobStatus
    try:
        with SessionLocal() as session:
            rows=session.execute(text("SELECT t.id,t.printer_id,t.print_job_id,t.ownership_operation_id "
                "FROM timelapses t JOIN jobs j ON j.id=t.print_job_id "
                "WHERE t.status IN ('capturing','encoding') AND j.status IN (:completed,:failed,:cancelled) "
                "AND t.retirement_operation_id IS NULL"),{'completed':JobStatus.COMPLETED.value,'failed':JobStatus.FAILED.value,'cancelled':JobStatus.CANCELLED.value}).fetchall()
            tickets=[]
            for tid,pid,jid,nonce in rows:
                if type(nonce) is str:tickets.append((pid,CaptureTicket(tid,nonce,jid)))
        for pid,ticket in tickets:
            encode_timelapse(ticket.timelapse_id,printer_id=pid,nonce=ticket.nonce,job_id=ticket.job_id)
    except Exception:
        log.error('Stale timelapse finalization could not be confirmed')


def main_loop():
    """Main capture loop."""
    TIMELAPSE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Timelapse capture daemon started")

    # Track active captures: {(printer_id, job_id): timelapse_id}
    active = {}
    tick = 0

    while True:
        session = None
        try:
            session = SessionLocal()
            printers = get_active_printers(session)

            current_keys = set()
            for printer in printers:
                job = get_active_job_for_printer(session, printer["id"])
                if not job:
                    continue

                key = (printer["id"], job["id"])
                current_keys.add(key)

                ticket = active.get(key)
                if not ticket:
                    ticket = get_or_create_timelapse(session, printer["id"], job["id"], return_ticket=True)
                    active[key] = ticket
                    tid = ticket.timelapse_id
                    log.info(f"Capturing timelapse {tid} for {printer['name']} (job {job['id']})")

                session.rollback()  # No daemon reader transaction spans camera I/O.
                capture_frame(printer['id'],ticket.timelapse_id,nonce=ticket.nonce,job_id=ticket.job_id)

            # Check for prints that just ended
            ended = set(active.keys()) - current_keys
            for key in ended:
                ticket = active.pop(key)
                session.rollback()
                log.info("Print ended for timelapse %s, starting encode",ticket.timelapse_id)
                encode_timelapse(ticket.timelapse_id,printer_id=key[0],nonce=ticket.nonce,job_id=ticket.job_id)

            # Every 5 minutes, check for stale timelapses (e.g. daemon was restarted)
            if tick % 10 == 0:
                finalize_stale_timelapses()

            tick += 1

        except Exception as e:
            log.error(f"Main loop error: {e}")
        finally:
            if session is not None:
                session.close()

        time.sleep(CAPTURE_INTERVAL)


if __name__ == "__main__":
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    main_loop()
