"""Host allowlist recovery against owned databases and the real audit chain."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from core import ip_allowlist_cache
from core.app_allowlist import _ip_allowlist_denial
from core.audit_integrity import verify
from core.models import AuditLog, SystemConfig
from core.schema import bootstrap_database
from modules.system.allowlist_recovery import main
from tests.test_contracts.test_post027_migration_033_postgres import pg_engine  # noqa: F401


BACKEND = Path(__file__).resolve().parents[2] / "backend"
ENABLED = {"enabled": True, "cidrs": ["192.0.2.4", "2001:db8::/32"], "mode": "api_only"}
DISABLED = {**ENABLED, "enabled": False}
EMPTY = {"enabled": False, "cidrs": [], "mode": "api_and_ui"}
ACTION = "ip_allowlist_recovered"


@pytest.fixture(scope="module", params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)])
def engine(request, tmp_path_factory):
    if request.param == "sqlite":
        path = tmp_path_factory.mktemp("allowlist") / "test.db"
        engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.1})
    else:
        engine = request.getfixturevalue("pg_engine")
    bootstrap_database(engine)
    yield engine
    if request.param == "sqlite":
        engine.dispose()


@pytest.fixture
def factory(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("ODIN_AUDIT_KEY_DIR", str(tmp_path / "audit-keys"))
    monkeypatch.setenv("ODIN_IP_ALLOWLIST_CACHE_SECONDS", "2.5")
    ip_allowlist_cache.invalidate()
    with engine.begin() as db:
        db.execute(text("DELETE FROM system_config WHERE key = 'ip_allowlist'"))
    yield sessionmaker(bind=engine)
    ip_allowlist_cache.invalidate()


def store(factory, value):
    with factory.begin() as db:
        db.add(SystemConfig(key="ip_allowlist", value=value))


def saved(factory):
    with factory() as db:
        return db.scalar(select(SystemConfig.value).where(SystemConfig.key == "ip_allowlist"))


def audits(factory):
    with factory() as db:
        return db.execute(select(AuditLog.id, AuditLog.details)
                          .where(AuditLog.action == ACTION).order_by(AuditLog.id)).all()


@pytest.mark.parametrize(("stored", "args", "expected"), [
    (None, [], "No allowlist is stored"),
    (ENABLED, [], "is enabled"),
    (DISABLED, [], "is disabled"),
    (["unreadable-marker"], [], "cannot be read"),
    (None, ["--disable"], "No allowlist is stored"),
    (DISABLED, ["--disable"], "already disabled"),
])
def test_no_writes(factory, engine, capsys, stored, args, expected):
    if stored is not None:
        store(factory, stored)
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert main(args, session_factory=factory) == 0
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert set(statements) <= {"SET", "BEGIN", "SELECT"}
    out, err = capsys.readouterr()
    assert expected in out and not err and "unreadable-marker" not in out
    if not args:
        assert "--disable" in out
        if isinstance(stored, dict):
            assert all(item in out for item in [stored["mode"], *stored["cidrs"]])


def test_disable_then_retry(factory, capsys):
    store(factory, ENABLED)
    before = audits(factory)
    outsider = "198.51.100.8"
    assert _ip_allowlist_denial(factory, outsider) == 403
    assert main(["--disable"], session_factory=factory) == 0
    assert "Running ODIN applies this within 2.5 seconds." in capsys.readouterr().out
    assert saved(factory) == DISABLED
    after = audits(factory)
    assert after[:-1] == before
    identifier, details = after[-1]
    assert details == {"previous_enabled": True, "network_count": 2, "source": "from the host command"}
    with factory() as db:
        assert db.execute(text("SELECT actor_user_id, actor_username, ip_address FROM audit_logs "
                               "WHERE id = :id"), {"id": identifier}).one() == (None, None, None)
        assert verify(db.connection())["ok"]
    ip_allowlist_cache.invalidate()
    assert _ip_allowlist_denial(factory, outsider) is None
    assert main(["--disable"], session_factory=factory) == 0
    assert "already disabled" in capsys.readouterr().out
    assert audits(factory) == after and saved(factory) == DISABLED


@pytest.mark.parametrize("value", [
    "unreadable-marker",
    {"enabled": "unreadable-marker", "cidrs": []},
    {"enabled": False, "cidrs": "unreadable-marker"},
    {"enabled": True, "cidrs": [42], "mode": "unreadable-marker"},
    {"enabled": True, "cidrs": ["unreadable-marker"]},
    {"enabled": True, "cidrs": [], "mode": ["unreadable-marker"]},
    json.dumps({**ENABLED, "mode": "unreadable-marker"}),
])
def test_unreadable_is_replaced_and_audited(factory, capsys, value):
    store(factory, value)
    before = audits(factory)
    assert main(["--disable"], session_factory=factory) == 0
    out, err = capsys.readouterr()
    assert "old list could not be kept" in out and "unreadable-marker" not in out and not err
    assert saved(factory) == EMPTY
    after = audits(factory)
    assert after[:-1] == before
    assert after[-1].details == {"stored_value_unreadable": True, "source": "from the host command"}


def test_legacy_mode_and_zero_ttl(factory, monkeypatch, capsys):
    monkeypatch.setenv("ODIN_IP_ALLOWLIST_CACHE_SECONDS", "0")
    store(factory, {"enabled": True, "cidrs": ENABLED["cidrs"]})
    assert main(["--disable"], session_factory=factory) == 0
    assert saved(factory) == {**DISABLED, "mode": "api_and_ui"}
    assert "Running ODIN applies this at once." in capsys.readouterr().out


def test_audit_failure_rolls_back(factory, engine, capsys):
    store(factory, ENABLED)
    before = audits(factory)

    def fail_insert(conn, cursor, statement, *args):
        if statement.startswith("INSERT INTO audit_logs"):
            raise RuntimeError("private error detail")

    event.listen(engine, "before_cursor_execute", fail_insert)
    try:
        assert main(["--disable"], session_factory=factory) == 1
    finally:
        event.remove(engine, "before_cursor_execute", fail_insert)
    out, err = capsys.readouterr()
    assert not out and err == "ODIN could not read or save the IP allowlist.\n"
    assert saved(factory) == ENABLED and audits(factory) == before


def test_locked_database(factory, engine, capsys):
    store(factory, ENABLED)
    before = audits(factory)
    with engine.connect() as blocker:
        blocker.execute(text("UPDATE system_config SET value = value WHERE key = 'ip_allowlist'"))
        assert main(["--disable"], session_factory=factory) == 1
    out, err = capsys.readouterr()
    assert not out and err == "ODIN could not read or save the IP allowlist.\n"
    assert saved(factory) == ENABLED and audits(factory) == before


@pytest.mark.parametrize("malformed", [
    "bad-json-marker",
    '{"enabled": true, "cidrs": [], "mode": "bad-json-marker", "extra": NaN}',
])
def test_invalid_json_with_real_entry_point(tmp_path, malformed):
    # PostgreSQL's JSON column rejects invalid JSON at insertion.
    path = tmp_path / "allowlist.db"
    engine = create_engine(f"sqlite:///{path}")
    bootstrap_database(engine)
    with engine.begin() as db:
        db.execute(text("INSERT INTO system_config (key, value) VALUES ('ip_allowlist', :value)"),
                   {"value": malformed})
    env = dict(os.environ, DATABASE_URL=str(engine.url), DEBUG="true", ODIN_AUDIT_KEY_DIR=str(tmp_path / "keys"))
    command = [sys.executable, "-m", "modules.system.allowlist_recovery"]
    for args, expected in [([], "cannot be read"), (["--disable"], "old list could not be kept")]:
        result = subprocess.run(command + args, cwd=BACKEND, env=env, capture_output=True, text=True)
        assert result.returncode == 0 and expected in result.stdout
        assert "bad-json-marker" not in result.stdout and "SELECT" not in result.stdout and not result.stderr
    factory = sessionmaker(bind=engine)
    assert saved(factory) == EMPTY
    [entry] = audits(factory)
    assert entry.details == {"stored_value_unreadable": True, "source": "from the host command"}
    engine.dispose()


def test_missing_database_exits_cleanly_without_creating_it(tmp_path):
    path = tmp_path / "missing.db"
    result = subprocess.run([sys.executable, "-m", "modules.system.allowlist_recovery"],
                            cwd=BACKEND, env=dict(os.environ, DATABASE_URL=f"sqlite:///{path}"),
                            capture_output=True, text=True)
    assert result.returncode == 1 and not path.exists()
    assert not result.stdout and result.stderr == "ODIN could not read or save the IP allowlist.\n"


def test_unknown_argument(capsys):
    assert main(["--dis"]) == 2
    out, err = capsys.readouterr()
    assert not out and "usage:" in err


def test_import_starts_no_thread_or_database(tmp_path):
    code = """
import sys, threading
def forbidden(*args, **kwargs):
    raise AssertionError('import started a thread')
threading.Thread.start = forbidden
import modules.system.allowlist_recovery
assert 'core.db' not in sys.modules
assert 'core.app' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, cwd=BACKEND,
                   env=dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path}/missing/db"))
