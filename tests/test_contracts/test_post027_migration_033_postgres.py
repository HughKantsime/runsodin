"""Migration 033 validate() on an owned native PostgreSQL: reflected now()/CURRENT_TIMESTAMP
spellings of the created_at default are accepted, other defaults refused (round 6).

Starts a pinned PostgreSQL container bound to 127.0.0.1 only (name prefix certfixB_),
skips only when Docker or the PostgreSQL driver is unavailable, and always removes it.
"""
import importlib
import secrets
import shutil
import subprocess
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
M = importlib.import_module("core.schema.migrations.033_timelapse_capture_tenancy")
TABLE = M.TABLE


def _docker(*args, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def pg_engine():
    pytest.importorskip("psycopg2")
    if not shutil.which("docker") or _docker("info", timeout=20).returncode:
        pytest.skip("Docker unavailable")
    name = "certfixB_m033_" + uuid.uuid4().hex[:10]
    password = secrets.token_urlsafe(24)
    try:
        started = _docker("run", "-d", "--name", name, "-p", "127.0.0.1::5432", "--tmpfs",
                          "/var/lib/postgresql/data:rw,size=128m", "-e", "POSTGRES_PASSWORD=" + password, IMAGE, timeout=180)
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 90
        while _docker("exec", name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1", timeout=10).returncode:
            assert time.monotonic() < deadline, "owned PostgreSQL not ready"
            time.sleep(0.3)
        port = _docker("port", name, "5432/tcp").stdout.strip().rsplit(":", 1)[-1]
        engine = create_engine(f"postgresql+psycopg2://postgres:{password}@127.0.0.1:{port}/postgres")
        for _ in range(100):
            try:
                engine.connect().close()
                break
            except Exception:
                time.sleep(0.3)
        yield engine
        engine.dispose()
    finally:
        _docker("rm", "-f", name)


def _fresh(engine, ddl):
    with engine.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
        c.execute(text("DROP TABLE IF EXISTS timelapse_ownership_anchors"))
        c.execute(text("CREATE TABLE timelapse_ownership_anchors(operation_id VARCHAR(64) PRIMARY KEY)"))
        if ddl:
            c.execute(text(ddl))


def _ddl(default):
    return (f"CREATE TABLE {TABLE}(operation_id VARCHAR(64) NOT NULL PRIMARY KEY REFERENCES "
            "timelapse_ownership_anchors(operation_id) ON DELETE CASCADE, captured_single_tenant BOOLEAN NOT NULL, "
            f"created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT {default})")


def test_the_migration_s_own_table_validates_on_postgresql(pg_engine):
    _fresh(pg_engine, None)
    with pg_engine.begin() as c:
        M.apply(c)
        M.validate(c)


@pytest.mark.parametrize("default", ["now()", "CURRENT_TIMESTAMP", "(now())", "transaction_timestamp()",
                                     "(now())::timestamp with time zone"])
def test_now_spellings_are_accepted(pg_engine, default):
    _fresh(pg_engine, _ddl(default))
    with pg_engine.begin() as c:
        M.validate(c)


@pytest.mark.parametrize("default", ["'2020-01-01 00:00:00+00'", "LOCALTIMESTAMP", "(now() - interval '1 day')"])
def test_other_defaults_are_refused(pg_engine, default):
    from core.schema.migrator import MigrationError
    _fresh(pg_engine, _ddl(default))
    with pg_engine.begin() as c:
        with pytest.raises(MigrationError, match="default"):
            M.validate(c)


def test_timestamp_without_time_zone_is_refused(pg_engine):
    from core.schema.migrator import MigrationError
    _fresh(pg_engine, _ddl("now()").replace("TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITHOUT TIME ZONE"))
    with pg_engine.begin() as c:
        with pytest.raises(MigrationError, match="timezone"):
            M.validate(c)
