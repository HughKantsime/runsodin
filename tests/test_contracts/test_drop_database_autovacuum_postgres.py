"""Dropping a disposable restore database survives an autovacuum worker (PostgreSQL 16).

Restore validation and staging create odin_validate_* databases as the non-superuser
maintenance role, load an archive into them and drop them with DROP DATABASE ... WITH
(FORCE). On PostgreSQL 16 a non-superuser's FORCE refuses outright ("permission denied
to terminate process") while an autovacuum worker -- which a fresh bulk load invites --
is processing that database; a plain DROP DATABASE cancels autovacuum itself. This is
the intermittent CV05 "offline restore scenarios" failure (run 14; 2 of 3 standalone
drills). restore_artifacts.drop_database falls back accordingly.

Starts a pinned PostgreSQL container on 127.0.0.1 with a deliberately slow autovacuum,
skips only when Docker is unavailable, and always removes it.
"""
import secrets
import shutil
import subprocess
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

IMAGE = "postgres@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685"


def _docker(*args, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def maintenance_url():
    import psycopg  # noqa: F401 - the product driver; a missing driver fails loud
    if not shutil.which("docker") or _docker("info", timeout=20).returncode:
        pytest.skip("Docker unavailable")
    name = "certfixB_dropav_" + uuid.uuid4().hex[:10]
    password = secrets.token_urlsafe(24)
    try:
        started = _docker("run", "-d", "--name", name, "-p", "127.0.0.1::5432", "--tmpfs",
                          "/var/lib/postgresql/data:rw,size=256m", "-e", "POSTGRES_PASSWORD=" + password, IMAGE,
                          "-c", "autovacuum_naptime=1", "-c", "autovacuum_vacuum_cost_delay=100",
                          "-c", "autovacuum_vacuum_cost_limit=1", "-c", "autovacuum_analyze_threshold=0",
                          "-c", "autovacuum_vacuum_insert_threshold=1", timeout=180)
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 90
        while _docker("exec", name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1", timeout=10).returncode:
            assert time.monotonic() < deadline, "owned PostgreSQL not ready"
            time.sleep(0.3)
        port = _docker("port", name, "5432/tcp").stdout.strip().rsplit(":", 1)[-1]
        admin = create_engine(f"postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/postgres",
                              isolation_level="AUTOCOMMIT")
        for _ in range(100):
            try:
                with admin.connect() as c:
                    c.execute(text("CREATE ROLE odin_maintenance LOGIN CREATEDB PASSWORD 'maintenance-test-only'"))
                break
            except Exception:
                time.sleep(0.3)
        admin.dispose()
        yield f"postgresql+psycopg://odin_maintenance:maintenance-test-only@127.0.0.1:{port}/postgres", name
    finally:
        _docker("rm", "-f", name)


def _database_under_autovacuum(url, container, name):
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}"')
    loaded = create_engine(url.rsplit("/", 1)[0] + "/" + name)
    with loaded.begin() as c:
        c.execute(text("CREATE TABLE t AS SELECT g, md5(g::text) m FROM generate_series(1,300000) g"))
    loaded.dispose()
    deadline = time.monotonic() + 60
    while True:
        workers = _docker("exec", container, "psql", "-U", "postgres", "-tAc",
                          f"SELECT count(*) FROM pg_stat_activity WHERE datname='{name}' AND backend_type='autovacuum worker'")
        if workers.stdout.strip() not in ("", "0"):
            return engine
        assert time.monotonic() < deadline, "autovacuum never visited the loaded database"
        time.sleep(0.2)


def test_forced_drop_alone_is_refused_while_autovacuum_runs(maintenance_url):
    """Control: the raw statement the restore paths used fails under this condition."""
    url, container = maintenance_url
    engine = _database_under_autovacuum(url, container, "odin_validate_" + uuid.uuid4().hex[:20])
    try:
        with engine.connect() as c:
            current = c.execute(text("SELECT datname FROM pg_database WHERE datname LIKE 'odin_validate_%'")).scalar_one()
            with pytest.raises(DBAPIError, match="permission denied to terminate process"):
                c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{current}" WITH (FORCE)')
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{current}"')
    finally:
        engine.dispose()


def test_drop_database_survives_autovacuum(maintenance_url):
    from modules.system.restore_artifacts import drop_database
    url, container = maintenance_url
    name = "odin_validate_" + uuid.uuid4().hex[:20]
    engine = _database_under_autovacuum(url, container, name)
    try:
        with engine.connect() as c:
            drop_database(c, name)
            assert c.execute(text("SELECT count(*) FROM pg_database WHERE datname=:n"), {"n": name}).scalar_one() == 0
    finally:
        engine.dispose()


def test_drop_database_still_terminates_the_maintenance_role_s_own_sessions(maintenance_url):
    """FORCE stays the first choice: a lingering session of the maintenance role is ended."""
    from modules.system.restore_artifacts import drop_database
    url, _container = maintenance_url
    name = "odin_validate_" + uuid.uuid4().hex[:20]
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}"')
    lingering = create_engine(url.rsplit("/", 1)[0] + "/" + name).connect()
    try:
        with engine.connect() as c:
            drop_database(c, name)
            assert c.execute(text("SELECT count(*) FROM pg_database WHERE datname=:n"), {"n": name}).scalar_one() == 0
    finally:
        try:
            lingering.close()
        except Exception:
            pass
        engine.dispose()


def test_every_restore_drop_goes_through_the_helper():
    from pathlib import Path
    backend = Path(__file__).resolve().parents[2] / "backend" / "modules" / "system"
    for path in ("postgres_backup_service.py", "pre026_restore_stage.py"):
        source = (backend / path).read_text()
        assert "WITH (FORCE)" not in source and "drop_database(" in source, path
    helper = (backend / "restore_artifacts.py").read_text()
    assert helper.count('" WITH (FORCE)"') == 1 and helper.count("exec_driver_sql(") == 1
