"""Camera-stack review LOW 5: one redaction rule set on write and read.

go2rtc.log, supervisord.log and captured stdout never pass through ODIN's redacting log
handlers, so the readers (log viewer, support bundle, troubleshooting report) must apply
the same rules: sanitize_log_line runs core.log_redaction.redact_text first. Also covers
UniFi rtspx:// tokens and URL-encoded login parameters.
"""
import base64
import io
import json
import secrets
import string
import zipfile

import pytest

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

# Synthetic values generated per run (never committed literals).
RTSP_PASSWORD = secrets.token_hex(32)
UNIFI_TOKEN = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
RELAY_TOKEN = secrets.token_hex(32)
IDP_CODE = "IdPCode-" + secrets.token_urlsafe(12)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


JWT = ".".join((_b64(b'{"alg":"HS256"}'), _b64(json.dumps({"sub": secrets.token_hex(4)}).encode()),
                _b64(secrets.token_bytes(32))))
SMTP_PASSWORD = "synthetic-" + secrets.token_hex(6)
SECRETS = (RTSP_PASSWORD, UNIFI_TOKEN, RELAY_TOKEN, IDP_CODE, JWT, SMTP_PASSWORD)

GO2RTC_LOG = [
    f"16:01:02.123 INF [streams] new consumer url=rtsp://odin:{RTSP_PASSWORD}@127.0.0.1:8554/printer_2",
    f"16:01:03.456 WRN [rtsp] source=rtspx://10.0.0.9:7441/{UNIFI_TOKEN} error=EOF",
    f"16:01:04.789 ERR [exec] ffmpeg http://127.0.0.1:8000/internal/camera-source/3/{RELAY_TOKEN} 404",
]
SUPERVISORD_LOG = [
    "2026-10-16 16:02:00,001 WARN exited: backend (exit status 1; not expected)",
    f"2026-10-16 16:02:00,002 INFO stdout: smtp login failed password={SMTP_PASSWORD}",
    f"2026-10-16 16:02:00,003 INFO stdout: GET /login?next=%2Fapi%2Fauth%2Foidc%2Fcallback%3Fcode%3D{IDP_CODE}%26state%3Dx",
]
STDOUT_BACKEND = [
    f"ERROR uncaught: Authorization: Bearer {JWT}",
    f"WARNING printed by a library: rtspx://cam.local:7441/{UNIFI_TOKEN}?enableSrtp",
]


def _clean(text):
    return not any(secret in text for secret in SECRETS)


@pytest.mark.parametrize("line", GO2RTC_LOG + SUPERVISORD_LOG + STDOUT_BACKEND)
def test_sanitize_log_line_applies_the_write_side_rules(line):
    from modules.system.sanitize import sanitize_log_line
    assert _clean(sanitize_log_line(line)), sanitize_log_line(line)


def test_parameter_names_survive_and_prose_is_untouched():
    from modules.system.sanitize import sanitize_log_line
    out = sanitize_log_line(SUPERVISORD_LOG[2])
    assert "%3Fcode%3D[REDACTED]" in out and "%26state%3D[REDACTED]" in out
    assert sanitize_log_line("zip code=12345 in prose") == "zip code=12345 in prose"


def test_log_viewer_serves_go2rtc_and_supervisord_logs_redacted(tmp_path, monkeypatch):
    from modules.system import routes_admin
    files = {"go2rtc": GO2RTC_LOG, "supervisord": SUPERVISORD_LOG, "backend": STDOUT_BACKEND}
    paths = {}
    for source, lines in files.items():
        path = tmp_path / f"{source}.log"
        path.write_text("\n".join(lines) + "\n")
        paths[source] = str(path)
    monkeypatch.setattr(routes_admin, "_LOG_FILES", paths)
    for source, lines in files.items():
        page = routes_admin.get_logs(source=source, lines=200, level=None, search=None, cursor=None,
                                     user={"id": 1, "role": "admin", "group_id": None})
        assert len(page["lines"]) == len(lines) and _clean(json.dumps(page)), page


def test_support_bundle_and_troubleshooting_report_redact_raw_log_text(review_db, monkeypatch):
    from sqlalchemy.orm import Session
    from modules.system import diagnostics, routes_admin, troubleshooting
    logs = {"/data/backend.log": STDOUT_BACKEND, "/data/supervisord.log": SUPERVISORD_LOG}
    monkeypatch.setattr(diagnostics, "_read_tail", lambda path, n: logs.get(path, []))
    with Session(review_db.get_bind()) as db:
        response = routes_admin.download_support_bundle(user={"id": 1, "role": "admin", "group_id": None}, db=db)
        import asyncio

        async def collect():
            return b"".join([chunk if isinstance(chunk, bytes) else chunk.encode() async for chunk in response.body_iterator])
        body = asyncio.run(collect())
        with zipfile.ZipFile(io.BytesIO(body)) as bundle:
            text = "\n".join(bundle.read(name).decode("utf-8", "replace") for name in bundle.namelist())
        assert "uncaught" in text and "printed by a library" in text and _clean(text)
        monkeypatch.setattr(troubleshooting, "server_checks", lambda db: {"checks": []})
        report = troubleshooting.support_report(
            troubleshooting.ReportRequest(problem=f"camera log shows rtspx://x:7441/{UNIFI_TOKEN}", include_logs=True),
            user={"id": 1, "role": "admin", "group_id": None}, db=db)
    assert report["recent_logs"]["supervisord"] and report["recent_logs"]["backend"]
    assert _clean(json.dumps(report))
