"""Controlled cameras use an authenticated relay with per-chunk audience checks."""
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.controlled_parts import policies, assert_access


def frame_check(engine, printer_id, viewer):
    from core.controlled_parts import any_marks
    with engine.connect() as connection:
        if not any_marks(connection):
            return
    with Session(engine) as db:
        assert_access(db, 'printer', printer_id,
                      {'id': viewer['user_id'], '_auth_kind': viewer.get('auth_kind')})


def prepare_camera(engine, job_id, printer_id):
    """After acquiring the dispatch claim, retire pre-existing WebRTC sessions.

    The durable claim already blocks new signaling while the relay reloads.
    Failure refuses dispatch before any upload/start. Unmarked work is a no-op.
    """
    with Session(engine) as db:
        rules = policies(db, 'job', job_id)
        camera = db.execute(text('SELECT camera_enabled FROM printers WHERE id=:id'), {'id': printer_id}).scalar()
    if not rules or not camera:
        return None
    from modules.printers.route_utils import _go2rtc_reload
    if not _go2rtc_reload():
        from core.audit import audit_download
        with Session(engine) as db:
            audit_download(db, 'controlled_part.refused', 'job', job_id, {'reason': 'controlled_part_camera_sessions_unconfirmed'})
        return 'controlled_part_camera_sessions_unconfirmed'
    return None


def frame_guard(engine, printer_id):
    """One existence read on the first chunk, after upstream admission.

    An unmarked stream needs no subsequent controlled-policy connections.
    Marking active work is refused; entering controlled dispatch resets the
    upstream camera sessions before sending a print. A stream opened after
    that reset sees the marks on its first chunk. Marked streams continue
    live per-frame membership checks.
    """
    import time
    enabled = None
    checked_at = 0
    def check(viewer):
        nonlocal enabled, checked_at
        if enabled is None or time.monotonic() - checked_at >= 30:
            from core.controlled_parts import any_marks
            with engine.connect() as connection:
                enabled = any_marks(connection)
                checked_at = time.monotonic()
        if enabled:
            frame_check(engine, printer_id, viewer)
    return check
