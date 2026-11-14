"""Under ITAR mode ODIN does not connect to a PostgreSQL server outside the local network.

Review 2026-11-13 finding 4: create_database_engine handed DATABASE_URL straight to
SQLAlchemy/psycopg and the backup service started pg_dump / pg_restore with no destination
check, so with ITAR mode on a DATABASE_URL naming a public server still carried every row
there.

Now, under ITAR mode, no engine is built and no PostgreSQL client is started unless the
server (URL host, `host` / `hostaddr` in the query, PGHOST, PGHOSTADDR) is on the local
network; a libpq service file is refused. Unix sockets and localhost pass.

Not proven here, and not done by ODIN: the connection is not pinned inside libpq (it looks
the name up again when it connects). No PostgreSQL server, psycopg or pg_dump runs in these
tests: SQLAlchemy's create_engine and subprocess.run are replaced and record their calls.
"""
import socket
import subprocess

import pytest
from sqlalchemy.engine import make_url

from core import database_config, itar_database, webhook_utils
from core.database_config import DatabaseConfigurationError, create_database_engine
from core.itar import ItarOutboundBlocked

PRIVATE = "172.18.0.3"
PUBLIC = "8.8.8.8"


class Resolver:
    def __init__(self, monkeypatch, table):
        self.table, self.calls = table, []
        monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", self)

    def __call__(self, host, *args, **kwargs):
        self.calls.append(host)
        if host not in self.table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
                for a in self.table[host]]


NAMES = {"postgres": (PRIVATE,), "db.example.com": (PUBLIC,), "mixed.example.com": (PRIVATE, PUBLIC),
         "localhost": ("127.0.0.1", "::1")}


@pytest.fixture
def engines(monkeypatch, tmp_path):
    """Engines SQLAlchemy was asked to build (none is built)."""
    built = []
    secret = tmp_path / "db-password"
    secret.write_text("secret\n")
    secret.chmod(0o600)
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))
    for name in ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGPORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(database_config, "assert_postgres_restore_ready", lambda *a, **k: None)

    class Engine:
        pass
    monkeypatch.setattr(database_config, "create_engine", lambda url, **kw: built.append(url) or Engine())
    monkeypatch.setattr(database_config.event, "listens_for", lambda *a, **k: (lambda function: function))
    return built


@pytest.fixture
def itar_on(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    return Resolver(monkeypatch, NAMES)


REFUSED = [
    "postgresql://odin@db.example.com:5432/odin",            # a name with a public address
    "postgresql://odin@mixed.example.com/odin",              # local and public: refused whole
    "postgresql://odin@unknown.example.com/odin",            # does not resolve
    f"postgresql://odin@{PUBLIC}/odin",                      # a public address
    "postgresql://odin@[2001:4860:4860::8888]:5432/odin",
    f"postgresql://odin@postgres/odin?hostaddr={PUBLIC}",    # libpq connects to hostaddr, not host
    "postgresql://odin@postgres/odin?host=db.example.com",   # libpq host override
    "postgresql://odin@/odin?host=db.example.com:5432&host=postgres:5432",   # multi-host, one public
    f"postgresql://odin@postgres,{PUBLIC}/odin",
    "postgresql://odin@/odin?service=production",            # a service file names the host
]
ALLOWED = [
    "postgresql://odin@postgres:5432/odin",                  # the compose service name
    f"postgresql://odin@{PRIVATE}/odin",
    "postgresql://odin@10.1.2.3:6432/odin",
    "postgresql://odin@[fd00:12::5]:5432/odin",
    "postgresql://odin@localhost/odin",
    "postgresql://odin@127.0.0.1/odin",
    "postgresql://odin@/odin",                               # libpq's default Unix socket
    "postgresql://odin@/odin?host=/var/run/postgresql",
    f"postgresql://odin@postgres/odin?hostaddr={PRIVATE}",
]


@pytest.mark.parametrize("url", REFUSED)
def test_no_engine_is_built_for_a_server_outside_the_local_network(engines, itar_on, url):
    with pytest.raises(DatabaseConfigurationError, match="ITAR mode is on"):
        create_database_engine(url)
    assert engines == []


@pytest.mark.parametrize("url", ALLOWED)
def test_a_local_server_a_socket_and_localhost_pass(engines, itar_on, url):
    create_database_engine(url)
    assert len(engines) == 1


@pytest.mark.parametrize("name, value", [("PGHOSTADDR", PUBLIC), ("PGHOST", "db.example.com"),
                                         ("PGHOST", f"postgres,{PUBLIC}"), ("PGSERVICE", "production")])
def test_a_libpq_environment_override_is_checked_too(engines, itar_on, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(DatabaseConfigurationError, match="ITAR mode is on"):
        create_database_engine("postgresql://odin@postgres/odin")
    assert engines == []
    monkeypatch.setenv(name, "postgres" if name == "PGHOST" else PRIVATE if name == "PGHOSTADDR" else "")
    create_database_engine("postgresql://odin@postgres/odin")
    assert len(engines) == 1


def test_an_already_resolved_url_object_is_checked(engines, itar_on):
    with pytest.raises(DatabaseConfigurationError):
        create_database_engine(make_url("postgresql+psycopg://odin:pw@db.example.com/odin"))
    assert engines == []


def test_the_refusal_names_no_host_or_credential(engines, itar_on):
    with pytest.raises(DatabaseConfigurationError) as refused:
        create_database_engine("postgresql://odin@db.example.com/odin")
    assert "db.example.com" not in str(refused.value) and "secret" not in str(refused.value)
    assert refused.value.__cause__ is None and refused.value.__suppress_context__


@pytest.mark.parametrize("url", REFUSED[:9])
def test_with_itar_off_nothing_is_checked(engines, monkeypatch, url):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    resolver = Resolver(monkeypatch, NAMES)
    create_database_engine(url)
    assert len(engines) == 1 and resolver.calls == []


def test_sqlite_is_never_checked(itar_on, tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'odin.db'}")
    engine.dispose()
    assert itar_on.calls == []
    itar_database.enforce_postgres_destination("sqlite:////data/odin.db")


# ---- the PostgreSQL client programs (pg_dump, pg_restore, psql) ----

@pytest.fixture
def clients(monkeypatch, tmp_path):
    from modules.system import postgres_backup_service as service
    started = []
    secret = tmp_path / "db-password"
    secret.write_text("secret\n")
    secret.chmod(0o600)
    for name in ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGPORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(command, **kwargs):
        started.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")
    monkeypatch.setattr(service.subprocess, "run", run)
    return service, started, str(secret), tmp_path


def _dump(url):
    return ["pg_dump", "--format=custom", "--file", "/tmp/x.dump", "--dbname", url]


@pytest.mark.parametrize("command, target", [
    (_dump("postgresql://odin@db.example.com/odin?application_name=odin-backup"), "postgresql://odin@postgres/odin"),
    (["pg_restore", f"--dbname=postgresql://odin_maintenance@{PUBLIC}/validation", "/tmp/x.dump"],
     "postgresql://odin@postgres/odin"),
    (["psql", "-d", "postgresql://odin@mixed.example.com/odin", "-c", "select 1"], "postgresql://odin@postgres/odin"),
    (["pg_restore", "--list", "/tmp/x.dump"], "postgresql://odin@db.example.com/odin"),
])
def test_no_client_is_started_against_a_server_outside_the_local_network(clients, itar_on, command, target):
    service, started, secret, directory = clients
    with pytest.raises(service.RestoreEnvironmentError, match="ITAR mode is on"):
        service._run_pg_tool(command, target, secret, directory)
    assert started == []


def test_a_client_environment_override_is_checked(clients, itar_on, monkeypatch):
    service, started, secret, directory = clients
    monkeypatch.setenv("PGHOSTADDR", PUBLIC)
    with pytest.raises(service.RestoreEnvironmentError):
        service._run_pg_tool(_dump("postgresql://odin@postgres/odin"), "postgresql://odin@postgres/odin", secret, directory)
    assert started == []


def test_a_client_against_a_local_server_runs(clients, itar_on):
    service, started, secret, directory = clients
    url = "postgresql://odin@postgres:5432/odin"
    assert service._run_pg_tool(_dump(url), url, secret, directory) == "ok"
    assert [command[0] for command in started] == ["/usr/bin/pg_dump"]


def test_with_itar_off_the_clients_are_unchanged(clients, monkeypatch):
    service, started, secret, directory = clients
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    resolver = Resolver(monkeypatch, NAMES)
    url = "postgresql://odin@db.example.com/odin"
    assert service._run_pg_tool(_dump(url), url, secret, directory) == "ok"
    assert len(started) == 1 and resolver.calls == []


def test_every_engine_and_database_client_goes_through_the_two_checked_functions():
    """create_engine for PostgreSQL is called in one place, and a client program is given a
    server only by _run_pg_tool. The two other pg_restore calls read an archive file and
    are given no server (no --dbname), so they connect to nothing."""
    import ast
    from pathlib import Path
    backend = Path(database_config.__file__).resolve().parents[1]
    offenders = []
    for path in backend.rglob("*.py"):
        relative = path.relative_to(backend).as_posix()
        if relative.startswith("core/schema/") or relative == "core/database_config.py":
            continue
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source, filename=relative)):
            if not isinstance(node, ast.Call):
                continue
            called = ast.unparse(node.func)
            text = ast.unparse(node)
            if called.endswith("create_engine") and "sqlite" not in text:
                offenders.append(f"{relative}: {called}")
            if called.startswith("subprocess.") and any(tool in text for tool in ("pg_dump", "psql", "pg_restore")) \
                    and "dbname" in text:
                offenders.append(f"{relative}: {called} with --dbname")
    assert offenders == []
    stage = (backend / "modules/system/pre026_restore_stage.py").read_text(encoding="utf-8")
    export = (backend / "modules/system/backup_api_export.py").read_text(encoding="utf-8")
    assert "--dbname" not in export
    assert stage.count("--dbname") == stage.count("b._run_pg_tool(['pg_dump'")


def test_destinations_are_read_the_way_libpq_reads_them():
    read = itar_database.postgres_destinations
    assert read(make_url("postgresql://u@h1:6000/d?hostaddr=10.0.0.1"), {}) == [("h1", 6000), ("10.0.0.1", 6000)]
    assert read(make_url("postgresql://u@/d?host=h1:5433&host=h2"), {}) == [("h1", 5433), ("h2", 5432)]
    assert read(make_url("postgresql://u@/d?host=/var/run/postgresql"), {}) == []
    assert read(make_url("postgresql://u@/d"), {"PGHOST": "a,b", "PGHOSTADDR": "10.0.0.2", "PGPORT": "7000"}) == \
        [("a", 7000), ("b", 7000), ("10.0.0.2", 7000)]
    assert read(make_url("postgresql://u@[fd00::5]:5433/d"), {}) == [("fd00::5", 5433)]
    with pytest.raises(ItarOutboundBlocked):
        read(make_url("postgresql://u@h/d"), {"PGSERVICE": "x"})
