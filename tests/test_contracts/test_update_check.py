"""Register B88: opt-in release checks, with no network in any test."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.models import SystemConfig
from modules.system import update_check as checks, update_releases as releases
from tests.test_contracts.test_cert_G_harness import run_probe

START = datetime(2026, 11, 21, tzinfo=timezone.utc)


def release(tag, body="", **fields):
    return {"tag_name": tag, "body": body, "published_at": "2026-11-20T01:02:03Z",
            "draft": False, "prerelease": False, **fields}


@pytest.mark.parametrize("entries,newest,security", [
    ([release("v2.10.0")], "2.10.0", None),
    ([release("v2.12.0"), release("v2.10.0", "notes\r\n### Security\r\nfix"), release("v2.9.0")],
     "2.12.0", "2.10.0"),
    ([release("v2.9.0", "### Security")], "2.9.0", None),
    ([release("v2.8.1", "### Security")], "2.8.1", None),
    ([release("v3.0.0", prerelease=True), release("v4.0.0", draft=True)], None, None),
    ([release("v3.0"), release("v3.0.1-alpha"), release("v3.0.1?q"), release("3.0.1"), release(None)], None, None),
    ([], None, None),
    ([release("v3.0.0", " ### Security\n### Security fixes\n### Security ")], "3.0.0", None),
])
def test_release_selection(entries, newest, security):
    result = releases.parse_releases(entries, "2.9.0")
    assert result["newest"] == newest
    assert result["security_version"] == security
    assert result["security"] == bool(security)
    assert result["published_at"] == ("2026-11-20T01:02:03+00:00" if newest else None)


def test_captured_github_releases():
    path = Path(__file__).resolve().parents[1] / "fixtures" / "github-releases.json"
    if not path.exists():
        pytest.skip("Awaiting Dev's captured GitHub releases JSON, including v2.4.1")
    captured = path.read_bytes()
    payload = json.loads(captured)
    hotfix = next(entry for entry in payload if entry["tag_name"] == "v2.4.1")
    assert releases.parse_releases([hotfix], "2.4.0")["security"]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=captured))
    result = releases.fetch_releases("2.4.0", transport=transport)
    assert result["outcome"] == "ok" and result["security"]


@pytest.mark.parametrize("code,body,expected", [
    (200, b" " * (512 * 1024 - 2) + b"[]", "ok"),
    (200, b" " * (512 * 1024 - 1) + b"[]", "too_large"),
    (403, b"remote error", "http_error"), (302, b"redirect", "http_error"),
    (200, b"not json", "invalid_response"), (200, b"{}", "invalid_response"),
    (0, b"", "timeout"),
])
def test_transport(code, body, expected, monkeypatch):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    requests = []

    def respond(request):
        requests.append(request)
        if not code:
            raise httpx.ReadTimeout("private remote detail")
        return httpx.Response(code, content=body, headers={"location": "https://unused.invalid/"})

    result = releases.fetch_releases("2.5.0", transport=httpx.MockTransport(respond))
    assert result["outcome"] == expected
    if expected != "ok":
        assert result == {"outcome": expected}
    request, = requests
    assert str(request.url) == "https://api.github.com/repos/HughKantsime/runsodin/releases?per_page=20"
    assert request.method == "GET" and request.content == b""
    assert dict(request.headers) == {"host": "api.github.com", "accept": "application/vnd.github+json", "user-agent": "ODIN"}
    assert set(request.extensions["timeout"].values()) == {10}


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.delenv("ODIN_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'updates.db'}")
    SystemConfig.__table__.create(engine)
    try:
        yield sessionmaker(engine)
    finally:
        engine.dispose()


def seed(sessions, offset=14):
    with sessions.begin() as db:
        checks.write(db, checks.SETTING, {"enabled": True, "enabled_at": START.isoformat(), "offset_minutes": offset})


@pytest.mark.parametrize("enabled,failed", [(False, False), (True, False), (True, True)])
def test_supervised_runner_across_three_days(sessions, monkeypatch, enabled, failed):
    from modules.reporting import report_runner as runner
    offset = 14 if failed else 0
    if enabled:
        seed(sessions, offset)
    clock = [START]
    attempts = []
    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return clock[0]

    def reply(req):
        attempts.append(clock[0])
        return httpx.Response(503 if failed else 200, json=[])

    class Finished(Exception):
        pass

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        if clock[0] >= START + timedelta(days=3):
            raise Finished

    monkeypatch.setattr(checks, "now", lambda: clock[0])
    monkeypatch.setattr(checks, "fetch_releases", lambda v: releases.fetch_releases(v, transport=httpx.MockTransport(reply)))
    monkeypatch.setattr(runner, "datetime", Clock)
    monkeypatch.setattr(runner.time, "sleep", sleep)
    monkeypatch.setattr(runner, "SessionLocal", sessions)
    monkeypatch.setattr(runner, "process_due_reports", lambda db: None)
    monkeypatch.setattr(runner, "process_quiet_hours_digest", lambda db: None)
    monkeypatch.setattr(runner, "_check_database_guardian", lambda: None)
    with pytest.raises(Finished):
        runner.main_loop()
    assert attempts == ([START + timedelta(days=d, minutes=max(offset, 1)) for d in range(3)] if enabled else [])


@pytest.mark.parametrize("mode", ["itar", "off"])
def test_override_blocks_stored_consent(sessions, monkeypatch, mode):
    seed(sessions)
    monkeypatch.setenv("ODIN_ITAR_MODE" if mode == "itar" else "ODIN_UPDATE_CHECK", "1" if mode == "itar" else "off")
    monkeypatch.setattr(checks, "fetch_releases", lambda v: pytest.fail("Unexpected request"))
    with sessions() as db:
        assert checks.status(db)["enabled"] is False
        checks.run_check(db, started=START)
        assert checks.update_notices(db, 1) == []
        with pytest.raises(HTTPException) as locked:
            checks.configure(db, True, 1)
        assert locked.value.status_code == 409
        with pytest.raises(HTTPException) as refused:
            checks.run_check(db)
        assert refused.value.status_code == 409 and refused.value.detail == checks.status(db)["disabled_reason"]
    if mode == "itar":
        assert releases.fetch_releases("2.5.0", transport=httpx.MockTransport(lambda r: pytest.fail("ITAR request"))) == {"outcome": "blocked"}


def test_manual_reservation_survives_concurrent_request_and_preserves_known_fix(sessions, monkeypatch):
    seed(sessions)
    clock = [START]
    entered, resume = Event(), Event()
    calls = []
    def reply(req):
        calls.append(req)
        entered.set()
        assert resume.wait(5)
        if len(calls) == 2:
            raise httpx.ReadTimeout("private detail")
        if len(calls) == 3:
            return httpx.Response(200, json=[release("v9.1.0")])
        return httpx.Response(200, json=[release("v9.0.0"), release("v8.0.0", "### Security")])
    monkeypatch.setattr(checks, "fetch_releases", lambda v: releases.fetch_releases(v, transport=httpx.MockTransport(reply)))
    monkeypatch.setattr(checks, "now", lambda: clock[0])
    def manual():
        with sessions() as db:
            checks.run_check(db)
    worker = Thread(target=manual)
    worker.start()
    try:
        assert entered.wait(5)
        clock[0] += timedelta(seconds=59)
        with pytest.raises(HTTPException) as refused:
            manual()
        assert refused.value.status_code == 429 and len(calls) == 1
    finally:
        resume.set()
        worker.join(5)
    clock[0] += timedelta(seconds=1)
    manual()
    with sessions() as db:
        assert len(calls) == 2
        assert checks.status(db)["outcome"] == "timeout"
        assert checks.update_notices(db, 1)[0]["dismissible"] is False
        assert "private detail" not in str(checks.read(db, checks.RESULT))
        clock[0] += timedelta(minutes=1)
        assert checks.run_check(db)["security"] is True
        assert checks.update_notices(db, 1)[0]["version"] == "9.1.0"
        monkeypatch.setattr(checks, "backend_version", lambda: "8.0.0")
        assert checks.update_notices(db, 1)[0]["dismissible"] is True
        monkeypatch.setattr(checks, "backend_version", lambda: "9.1.0")
        assert checks.update_notices(db, 1) == []


def test_routes_permissions_consent_audit_and_version_bound_dismissal(tmp_path):
    result = run_probe("update-check", r"""
from modules.system import update_check as checks
from modules.system.update_releases import parse_releases
seed_user("owner", "admin")
admin = login("owner")
path = "/api/system/updates"
notices = "/api/system/notices"
def dismiss(client, version):
    return client.post(notices + "/update_available:" + version + "/dismiss")
for name, role, group in [("orgadmin", "admin", 7), ("op", "operator", None), ("reader", "viewer", None)]:
    seed_user(name, role, group)
    user = login(name)
    for answer in [user.get(path), user.put(path, json={"enabled": True}), user.post(path+"/check"),
                   user.get(notices), dismiss(user, "8.0.0")]:
        assert answer.status_code == 403, (name, answer.status_code)
os.environ["ODIN_UPDATE_CHECK"] = "on"
assert not admin.get(path).json()["enabled"]
assert admin.post(path+"/check").status_code == 409
def toggle(value):
    r = admin.put(path, json={"enabled": value})
    assert r.status_code == 200, r.text
    return r.json()
assert toggle(True)["enabled"]
pref = lambda: json.loads(sql("SELECT value FROM system_config WHERE key='update_check'")[0][0])
first = pref()
assert first["enabled_by"] == sql("SELECT id FROM users WHERE username='owner'")[0][0]
assert first["enabled_at"] and 0 <= first["offset_minutes"] < 1440
checks.fetch_releases = lambda v: {"outcome": "ok", **parse_releases(
    [{"tag_name":"v8.0.0","draft":False,"prerelease":False,"body":"ordinary"}], v)}
assert admin.post(path+"/check").json()["newest"] == "8.0.0"
assert admin.post(path+"/check").status_code == 429
def listing(client):
    return [n for n in client.get(notices).json()["notices"] if n["kind"] == "update_available"]
assert listing(admin)[0]["link"] == "https://github.com/HughKantsime/runsodin/releases/tag/v8.0.0"
assert dismiss(admin, "7.0.0").status_code == 409
assert dismiss(admin, "8.0.0").status_code == 200 and not listing(admin)
seed_user("owner2", "admin")
assert listing(login("owner2"))
from datetime import timedelta
real_now = checks.now
checks.now = lambda: real_now() + timedelta(minutes=2)
checks.fetch_releases = lambda v: {"outcome":"ok", **parse_releases(
    [{"tag_name":"v9.0.0","draft":False,"prerelease":False,"body":"### Security"}], v)}
assert admin.post(path+"/check").json()["security"]
assert not listing(admin)[0]["dismissible"]
assert dismiss(admin, "9.0.0").status_code == 409
os.environ["ODIN_ITAR_MODE"] = "1"
assert admin.post(path+"/check").json()["detail"] == checks.ITAR_REASON
assert admin.put(path,json={"enabled":True}).status_code == 409
assert not listing(admin)
os.environ.pop("ODIN_ITAR_MODE")
assert not toggle(False)["enabled"] and not listing(admin)
toggle(False)
toggle(True)
assert first["offset_minutes"] == pref()["offset_minutes"]
rows = sql("SELECT details, actor_username FROM audit_logs WHERE action='update_check.changed' ORDER BY id")
assert [(json.loads(d)["enabled"], actor) for d,actor in rows] == [(True,"owner"),(False,"owner"),(True,"owner")]
sql("CREATE TRIGGER refuse_update_audit BEFORE INSERT ON audit_logs WHEN NEW.action='update_check.changed' "
    "BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
assert admin.put(path, json={"enabled":False}).status_code >= 500
assert pref()["enabled"] is True
""", tmp_path)
    assert result["_net_attempts"] == []
