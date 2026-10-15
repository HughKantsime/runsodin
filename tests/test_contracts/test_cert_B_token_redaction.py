"""Cert B: overlay camera tokens (and every ?token= credential) never reach logs,
error buffers, support bundles or audit rows (F02-2.8-02 follow-up)."""
import io
import json
import logging
import secrets
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def _token():
    # token_urlsafe output: contains '-' and '_' often enough to defeat prefix-only patterns.
    return "odin_" + secrets.token_urlsafe(32) + "-_tail"


def _capture(logger_name, formatter):
    from core.log_redaction import install_log_redaction
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    install_log_redaction()
    return logger, handler, stream


def test_uvicorn_access_log_redacts_token_query(monkeypatch):
    from uvicorn.logging import AccessFormatter
    token = _token()
    logger, handler, stream = _capture("uvicorn.access", AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
    try:
        # The exact call uvicorn's httptools/h11 protocol makes for each request.
        logger.info('%s - "%s %s HTTP/%s" %d', "172.17.0.1:5000", "GET", f"/api/overlay/1/camera?token={token}&x=1", "1.1", 200)
    finally:
        logger.removeHandler(handler)
    line = stream.getvalue()
    assert token not in line and "odin_" not in line
    assert "/api/overlay/1/camera?token=[REDACTED]&x=1" in line


def test_uvicorn_websocket_and_app_error_logs_redact_tokens():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
    token = _token()
    ws_logger, ws_handler, ws_stream = _capture("uvicorn.error", logging.Formatter("%(message)s"))
    root_logger, root_handler, root_stream = _capture("", logging.Formatter("%(message)s"))
    try:
        ws_logger.info('%s - "WebSocket %s" [accepted]', ("172.17.0.1", 1), f"/ws?token={jwt}")
        logging.getLogger("odin.api").error("Upstream failed for %s", f"http://odin/api/overlay/1/camera?token={token}")
        logging.getLogger("odin.api").error(f"inline url /api/timelapses/3/video?token={jwt}")
    finally:
        ws_logger.removeHandler(ws_handler)
        root_logger.removeHandler(root_handler)
    combined = ws_stream.getvalue() + root_stream.getvalue()
    assert jwt not in combined and token not in combined and "eyJ" not in combined
    assert "/ws?token=[REDACTED]" in combined and "camera?token=[REDACTED]" in combined


def test_error_buffer_redacts_exception_text():
    from core.error_buffer import ErrorRingBuffer
    token = _token()
    buffer = ErrorRingBuffer()
    try:
        raise RuntimeError(f"could not open /api/overlay/9/camera?token={token}")
    except RuntimeError as exc:
        buffer.capture(exc)
    assert token not in json.dumps(buffer.entries())


def test_audit_details_never_store_tokens(review_db):
    from core.audit import log_audit
    from core.models import AuditLog
    token = _token()
    log_audit(review_db, "overlay.url_copied", "printer", 9,
              {"url": f"http://farm.local/overlay/9?token={token}", "nested": [{"raw": token}], "count": 1})
    review_db.commit()
    details = review_db.query(AuditLog).filter(AuditLog.action == "overlay.url_copied").one().details
    stored = json.dumps(details)
    assert token not in stored and "odin_" not in stored
    assert details["url"] == "http://farm.local/overlay/9?token=[REDACTED]" and details["count"] == 1


def test_support_bundle_redacts_tokens_in_recent_errors_and_settings(review_db, monkeypatch):
    from core.db import get_db
    from modules.system import diagnostics, routes_admin
    token = _token()
    lines = [f'2026-10-15 ERROR proxy failed GET /api/overlay/1/camera?token={token} HTTP/1.1',
             f'2026-10-15 WARNING ws /ws?token={token}']
    monkeypatch.setattr(diagnostics, "_read_tail", lambda path, n: lines)
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('overlay_note',:v)"), {"v": f"see /overlay/1?token={token}"})
    review_db.commit()
    app = FastAPI()
    app.include_router(routes_admin.router)
    app.dependency_overrides[get_db] = lambda: review_db
    from core.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {"id": 3, "role": "admin", "group_id": None, "is_active": True}
    response = TestClient(app).get("/admin/support-bundle")
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        everything = "".join(bundle.read(n).decode() for n in bundle.namelist())
    assert token not in everything and "_tail" not in everything
    assert "proxy failed" in everything  # the lines are kept, only the credential is removed


def test_log_viewer_sanitizer_removes_whole_urlsafe_token():
    from modules.system.sanitize import sanitize_log_line
    token = _token()
    assert token[5:] not in sanitize_log_line(f"GET /api/overlay/1/camera?token={token} 200")
    assert "_tail" not in sanitize_log_line(f"token {token}")
