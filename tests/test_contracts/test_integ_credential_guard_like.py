"""The credential write guard refuses a system_config LIKE only when it could reach a credential key.

Deleting or resetting a job drops its quota markers with
"DELETE FROM system_config WHERE key LIKE :a OR key = :b" (core/quota.py). The guard
failed closed on every LIKE, so the legacy global API key (a non-interactive platform
admin) could not delete a job. A LIKE now passes only when every pattern is a bound
string that can match no guarded key (smtp_config, base_url, setup_*, require_mfa) and no
guarded prefix (oidc*); literal, column or unresolved patterns, GLOB, ESCAPE and keys
built in SQL still fail closed.
"""
import pytest

QUOTA_DELETE = "DELETE FROM system_config WHERE key LIKE :a OR key = :b"


def _refused(statement, params):
    from core import credential_write_guard as guard
    return guard.guarded_write_target(statement, params) == "system_config"


def test_quota_marker_cleanup_is_not_a_credential_write():
    assert not _refused(QUOTA_DELETE, {"a": "quota_attempt:job:5:%", "b": "quota_attempt:seq:5"})
    assert not _refused("DELETE FROM system_config WHERE key LIKE ? OR key = ?", ("quota_attempt:job:5:%", "quota_attempt:seq:5"))


@pytest.mark.parametrize("pattern", ["%", "s%", "smtp%", "_mtp_config", "%config", "SMTP_CONFIG", "o%", "oidc_%",
                                     "OIDC%", "%oidc", "base_ur_", "setup%", "%mfa", "require\\_mfa", "quota%"])
def test_patterns_that_could_reach_a_credential_key_are_refused(pattern):
    statement = "DELETE FROM system_config WHERE key LIKE :a OR key = :b"
    reaches = pattern not in ("quota%",)  # backslash patterns fail closed (PostgreSQL LIKE escape)
    assert _refused(statement, {"a": pattern, "b": "quota_attempt:seq:5"}) is reaches, pattern


@pytest.mark.parametrize("statement,params", [
    ("DELETE FROM system_config WHERE key LIKE 'quota_attempt:%' OR key = :b", {"b": "quota_attempt:seq:5"}),
    ("DELETE FROM system_config WHERE key LIKE value OR key = :b", {"b": "x"}),
    ("DELETE FROM system_config WHERE key LIKE :a ESCAPE '!' OR key = :b", {"a": "quota_attempt:job:5:%", "b": "x"}),
    ("DELETE FROM system_config WHERE key GLOB :a OR key = :b", {"a": "quota_attempt:*", "b": "x"}),
    ("DELETE FROM system_config WHERE key = 'quota' || :a", {"a": "x"}),
    ("DELETE FROM system_config WHERE key LIKE :missing OR key = :b", {"b": "x"}),
    ("DELETE FROM system_config WHERE key LIKE :a", {"a": "quota_attempt:job:5:%"}),  # no key equality: key-less write
])
def test_unprovable_patterns_still_fail_closed(statement, params):
    assert _refused(statement, params)


def test_legacy_api_key_admin_can_delete_and_reset_a_job(tmp_path, monkeypatch):
    import os
    import secrets
    from pathlib import Path
    from cryptography.fernet import Fernet
    script = tmp_path / "probe.py"
    script.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from sqlalchemy import text\n"
        "from core.db import engine\n"
        "from core.schema import bootstrap_database\n"
        "bootstrap_database(engine, Path('backend').resolve())\n"
        "with engine.begin() as c:\n"
        "    c.execute(text(\"INSERT INTO users(id,username,email,password_hash,role,is_active) VALUES(1,'a','a@x','x','admin',1)\"))\n"
        "    c.execute(text(\"INSERT INTO jobs(id,item_name,status,priority,quantity) VALUES(5,'j','failed',1,1),(6,'k','pending',1,1)\"))\n"
        "    c.execute(text(\"INSERT INTO system_config(key,value) VALUES('quota_attempt:seq:6','1'),('smtp_config','{}')\"))\n"
        "from core.app import create_app\n"
        "from fastapi.testclient import TestClient\n"
        "c = TestClient(create_app(), raise_server_exceptions=False)\n"
        "h = {'X-API-Key': os.environ['API_KEY']}\n"
        "print('reset', c.post('/api/jobs/5/reset', headers=h).status_code)\n"
        "print('delete', c.delete('/api/jobs/6', headers=h).status_code)\n"
        "with engine.connect() as k:\n"
        "    print('markers', k.execute(text(\"SELECT count(*) FROM system_config WHERE key='quota_attempt:seq:6'\")).scalar())\n"
        "    print('smtp', k.execute(text(\"SELECT count(*) FROM system_config WHERE key='smtp_config'\")).scalar())\n")
    root = Path(__file__).resolve().parents[2]
    data = tmp_path.resolve()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{data / 'odin.db'}", "JWT_SECRET_KEY": secrets.token_urlsafe(48),
           "API_KEY": "k" + secrets.token_hex(12), "ENCRYPTION_KEY": Fernet.generate_key().decode(),
           "LICENSE_DIR": str(data), "ODIN_DATA_DIR": str(data), "PYTHONPATH": f"{root / 'backend'}:{root}"}
    import subprocess
    import sys
    result = subprocess.run([sys.executable, str(script)], cwd=root, env=env, capture_output=True, text=True, timeout=300)
    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines() if line.split(" ", 1)[0] in ("reset", "delete", "markers", "smtp"))
    assert lines.get("reset") == "200" and lines.get("delete") == "204", (result.stdout[-2000:], result.stderr[-2000:])
    assert lines.get("markers") == "0" and lines.get("smtp") == "1", lines
