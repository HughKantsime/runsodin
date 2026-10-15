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


@pytest.mark.parametrize("param", ["oidc_code", "code", "state", "nonce", "password", "refresh_token", "id_token", "client_secret"])
def test_auth_flow_query_parameters_are_redacted(param):
    from core.log_redaction import redact_text
    value = "S3cr" + secrets.token_hex(8)
    line = f'GET /api/auth/oidc/callback?x=1&{param}={value}&next=/ HTTP/1.1'
    assert value not in redact_text(line) and f"{param}=[REDACTED]" in redact_text(line)


@pytest.mark.parametrize("header", ["Authorization: Bearer {v}", "authorization: Basic {v}", "Authorization: Token {v}",
                                    "'Authorization': 'Digest {v}'", "X-API-Key: {v}", "x-api-key={v}", '"X-API-Key": "{v}"'])
def test_authorization_and_api_key_header_values_are_redacted(header):
    from core.log_redaction import redact_text
    value = "Zq" + secrets.token_hex(12)
    assert value not in redact_text("request headers " + header.format(v=value) + " end")


def test_handlers_added_after_startup_and_basic_config_are_redacted():
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    token = _token()
    stream = io.StringIO()
    late = logging.StreamHandler(stream)  # e.g. a module's own basicConfig/handler added later
    logger = logging.getLogger("odin.late.module")
    logger.propagate = False
    logger.addHandler(late)
    try:
        logger.warning("proxy %s", f"/overlay/1/camera?token={token}")
        try:
            raise RuntimeError(f"failed /api/timelapses/1/video?token={token}")
        except RuntimeError:
            logger.exception("upstream error")
        logger.error({"url": f"/overlay/1/camera?token={token}"})  # non-str msg
    finally:
        logger.removeHandler(late)
    assert token not in stream.getvalue() and stream.getvalue().count("[REDACTED]") >= 3


def test_jwt_pattern_is_linear_on_adversarial_input():
    import time
    from core.log_redaction import redact_text
    hostile = ("eyJ" + "a" * 50 + ".") * 20000 + "x"
    started = time.perf_counter()
    redact_text(hostile)
    assert time.perf_counter() - started < 1.0


def test_odin_matching_targets_real_tokens_only():
    from core.log_redaction import redact_text
    token = "odin_" + secrets.token_urlsafe(32)
    assert redact_text(f"key {token} ok") == "key [REDACTED] ok"
    for identifier in ("odin_api", "odin_contract-batch-ac", "odin.api", "odin_timelapse_42.mp4", "odin_schema_migrations"):
        assert redact_text(f"see {identifier} here") == f"see {identifier} here"


def _emit(logger_name, *log_args, exc=False, **log_kw):
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)  # added after install
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger(logger_name)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.warning(*log_args, **log_kw)
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def test_placeholders_survive_redaction():
    out = _emit("odin.t.placeholders", "printer %s camera %d of %s", "token=abc", 3, "x")
    assert "printer token=abc camera 3 of x" in out  # nothing secret here: formatting intact
    out = _emit("odin.t.placeholders2", "%(name)s=%(value)s", {"name": "token", "value": "plain"})
    assert "token=plain" in out


def test_non_str_args_are_redacted_after_formatting():
    import httpx
    token = _token()

    class Lazy:
        def __str__(self):
            return f"/overlay/1/camera?token={token}"
    out = _emit("odin.t.nonstr", "url=%s list=%s dict=%s lazy=%s",
                httpx.URL(f"http://h/overlay/1/camera?token={token}"), [f"?token={token}"], {"next": f"?token={token}"}, Lazy())
    assert token not in out and out.count("[REDACTED]") >= 4


def test_header_dicts_and_userinfo_are_redacted():
    secret = "Hv" + secrets.token_hex(10)
    out = _emit("odin.t.headers", "headers %s", {"Authorization": f"Bearer {secret}", "X-API-Key": secret, "Accept": "*/*"})
    assert secret not in out and "*/*" in out
    out = _emit("odin.t.userinfo", "camera rtsp://admin:%s@192.0.2.1:554/s and http://u:%s@h/", secret, secret)
    assert secret not in out and "rtsp://[REDACTED]@192.0.2.1:554/s" in out
    for param in ("pwd", "passwd", "access_code"):
        assert secret not in _emit("odin.t.params", "GET /x?%s=%s", param, secret)


def test_structured_dict_messages_keep_their_shape():
    from core.log_redaction import redact_record
    token = _token()
    record = logging.LogRecord("odin.t", logging.INFO, __file__, 1, {"event": "copy", "url": f"/o?token={token}",
                               "headers": {"Authorization": "Basic Zm9vOmJhcg=="}}, None, None)
    redact_record(record)
    assert isinstance(record.msg, dict) and record.msg["event"] == "copy"
    assert token not in json.dumps(record.msg) and record.msg["headers"]["Authorization"] == "[REDACTED]"


def test_handle_error_never_prints_raw_arguments(capsys):
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    token = _token()

    class Broken(logging.Handler):
        def emit(self, record):  # the stdlib pattern: emit failures go to handleError
            try:
                raise RuntimeError("emit failed")
            except Exception:
                self.handleError(record)
    logger = logging.getLogger("odin.t.broken")
    logger.propagate = False
    broken = Broken()
    logger.addHandler(broken)
    previous = logging.raiseExceptions
    logging.raiseExceptions = True
    try:
        logger.error("bad %s", f"/overlay/1/camera?token={token}")
    finally:
        logging.raiseExceptions = previous
        logger.removeHandler(broken)
    err = capsys.readouterr().err
    assert "Logging error" in err and token not in err


DAEMONS = ["modules/archives/timelapse_capture.py", "modules/printers/monitors/elegoo_monitor.py",
           "modules/printers/monitors/moonraker_monitor.py", "modules/printers/monitors/mqtt_monitor.py",
           "modules/printers/monitors/prusalink_monitor.py", "modules/reporting/report_runner.py", "modules/vision/monitor.py"]


def test_every_supervised_process_installs_redaction():
    """Each process supervisord starts (docker/supervisord*.conf) installs redaction in its __main__ block."""
    import ast, re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    commands = set()
    for conf in (root / "docker").glob("supervisord*.conf"):
        commands |= set(re.findall(r"python3 -m (modules\.[\w.]+)", conf.read_text()))
    assert {c.replace(".", "/") + ".py" for c in commands} == set(DAEMONS)
    for daemon in DAEMONS:
        tree = ast.parse((root / "backend" / daemon).read_text())
        main = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)]
        assert main, daemon
        first = "".join(ast.unparse(statement) for statement in main[0].body[:2])
        assert "install_log_redaction()" in first, f"{daemon}: install redaction before anything logs"
