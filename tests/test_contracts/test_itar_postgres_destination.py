"""Under ITAR mode ODIN connects to PostgreSQL only on the local network, at the address it
checked, and refuses every way of naming the server that it cannot pin with certainty.

History (reviews of 2026-11-13):
  finding 4      create_database_engine and the backup tools had no destination check.
  recheck 1 (A)  the check threw the address away; libpq looked the name up again.
  recheck 2      mirroring libpq's host lists and precedence left gaps (a partly empty
                 `hostaddr` list, `?host=` overriding the authority, empty host positions).
Round 3 stops mirroring libpq. Under ITAR mode exactly one form is accepted: one host in
the URL authority (or no host: the local socket). More than one host, `host` / `hostaddr`
/ `port` / `service` in the query, and PGHOST / PGHOSTADDR / PGPORT / PGSERVICE /
PGSERVICEFILE in the environment are refused at start. The pin is built from the arguments
SQLAlchemy hands the driver: one `hostaddr` for the one `host`.

No PostgreSQL server, psycopg or pg_dump runs here: SQLAlchemy's create_engine and
subprocess.run are replaced and record their calls, and the engine's connect listener is
called with the driver arguments. One test builds a real SQLAlchemy engine and replaces
only psycopg.connect; it is skipped where psycopg is not installed. That libpq connects to
`hostaddr` without a lookup and verifies TLS against `host` is libpq's documented
behaviour and is not exercised here.
"""
import ast
import socket
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from core import database_config, itar_database, webhook_utils
from core.database_config import DatabaseConfigurationError, create_database_engine
from core.itar import ItarOutboundBlocked

PRIVATE = "172.18.0.3"
PUBLIC = "8.8.8.8"
PG_ENVIRONMENT = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE")
NAMES = {"postgres": (PRIVATE,), "db.example.com": (PUBLIC,), "mixed.example.com": (PRIVATE, PUBLIC),
         "localhost": ("::1", "127.0.0.1"), "two.plant.example": ("10.0.0.2", "10.0.0.1")}


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


@pytest.fixture
def engines(monkeypatch, tmp_path):
    """Engines SQLAlchemy was asked to build (none is built), and the listeners put on them."""
    secret = tmp_path / "db-password"
    secret.write_text("secret\n")
    secret.chmod(0o600)
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))
    for name in PG_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(database_config, "assert_postgres_restore_ready", lambda *a, **k: None)

    class Engine:
        def __init__(self):
            received = self.received = []

            class Dialect:
                @staticmethod
                def connect(*cargs, **arguments):
                    received.append(arguments)
                    return arguments
            self.dialect = Dialect()
    listeners = {}

    class Built(list):
        engines = []

        def connect(self, **driver_arguments):
            """Hand the arguments the dialect built to the newest engine's dialect.connect,
            the one call that reaches the driver, and return what the driver receives.
            `driver_arguments` must come back unchanged: SQLAlchemy shares that one
            dictionary between all connections."""
            before = dict(driver_arguments)
            received = self.engines[-1].dialect.connect(**driver_arguments)
            assert driver_arguments == before, "the shared connect arguments were changed"
            return received

        def listener_names(self):
            return sorted(listeners)
    built = Built()
    def create(url, **kw):
        built.append(url)
        built.engines.append(Engine())
        return built.engines[-1]
    monkeypatch.setattr(database_config, "create_engine", create)

    def listens_for(_target, name, *a, **k):
        return lambda function: listeners.setdefault(name, []).append(function) or function
    monkeypatch.setattr(database_config.event, "listens_for", listens_for)
    return built


@pytest.fixture
def itar_on(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    return Resolver(monkeypatch, NAMES)


# ---- the one accepted form ----

@pytest.mark.parametrize("url, host, port, expected", [
    ("postgresql://odin@postgres:5432/odin", "postgres", 5432, PRIVATE),          # the compose service name
    (f"postgresql://odin@{PRIVATE}/odin", PRIVATE, None, PRIVATE),
    ("postgresql://odin@10.1.2.3:6432/odin", "10.1.2.3", 6432, "10.1.2.3"),
    ("postgresql://odin@[fd00:12::5]:5432/odin", "fd00:12::5", 5432, "fd00:12::5"),
    ("postgresql://odin@127.0.0.1/odin", "127.0.0.1", None, "127.0.0.1"),
    ("postgresql://odin@postgres/odin?sslmode=verify-full&connect_timeout=5", "postgres", None, PRIVATE),
])
def test_one_host_is_accepted_and_each_connection_gets_its_checked_address(engines, itar_on, url, host, port, expected):
    create_database_engine(url)
    arguments = {"host": host, "dbname": "odin", **({"port": port} if port else {})}
    assert engines.connect(**arguments) == {**arguments, "hostaddr": expected}


def test_the_first_answer_in_the_resolvers_order_is_used(engines, itar_on):
    """Not "IPv4 first": localhost on a server that listens only on ::1 keeps working."""
    create_database_engine("postgresql://odin@localhost/odin")
    assert engines.connect(host="localhost")["hostaddr"] == "::1"
    create_database_engine("postgresql://odin@two.plant.example/odin")
    assert engines.connect(host="two.plant.example")["hostaddr"] == "10.0.0.2"


@pytest.mark.parametrize("url", ["postgresql://odin@/odin", "postgresql://odin@/odin?sslmode=disable"])
def test_no_host_is_the_local_socket_and_needs_no_pin(engines, itar_on, url):
    create_database_engine(url)
    assert engines.connect(dbname="odin") == {"dbname": "odin"} and itar_on.calls == []


def test_a_name_that_changes_its_answer_never_reaches_libpq(engines, monkeypatch):
    """Local when ODIN starts, public afterwards: libpq is never left to look the name up,
    and a later (pooled) connection whose lookup is public is refused."""
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    answers = iter([(PRIVATE,), (PRIVATE,), (PUBLIC,), ("10.0.0.9",)])
    lookups = []

    def resolver(host, *args, **kwargs):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in next(answers)]
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", resolver)
    create_database_engine("postgresql://odin@db.plant.example/odin")       # lookup 1: start-up check
    assert engines.connect(host="db.plant.example")["hostaddr"] == PRIVATE  # lookup 2: first connection
    with pytest.raises(ItarOutboundBlocked):
        engines.connect(host="db.plant.example")                            # lookup 3: public, no connection
    assert engines.connect(host="db.plant.example")["hostaddr"] == "10.0.0.9"   # the database moved, still local
    assert lookups == ["db.plant.example"] * 4, "one lookup per connection, and its answer is the one used"


# ---- a server outside the local network ----

@pytest.mark.parametrize("url", [
    "postgresql://odin@db.example.com:5432/odin",       # a name with a public address
    "postgresql://odin@mixed.example.com/odin",         # local and public: refused whole
    "postgresql://odin@unknown.example.com/odin",       # does not resolve
    f"postgresql://odin@{PUBLIC}/odin",
    "postgresql://odin@[2001:4860:4860::8888]:5432/odin",
])
def test_no_engine_is_built_for_a_server_outside_the_local_network(engines, itar_on, url):
    with pytest.raises(DatabaseConfigurationError, match="not on the local network"):
        create_database_engine(url)
    assert engines == []


def test_the_refusal_names_no_host_or_credential(engines, itar_on):
    with pytest.raises(DatabaseConfigurationError) as refused:
        create_database_engine("postgresql://odin@db.example.com/odin")
    assert "db.example.com" not in str(refused.value) and "secret" not in str(refused.value)
    assert refused.value.__cause__ is None and refused.value.__suppress_context__


# ---- every other form is refused at start, one test per form ----

def _refused_at_start(engines, url, names):
    with pytest.raises(DatabaseConfigurationError) as refused:
        create_database_engine(url)
    message = str(refused.value)
    assert engines == [] and "does not start" in message and "one host in the address part" in message
    assert names in message
    return message


def test_more_than_one_host_is_refused(engines, itar_on):
    _refused_at_start(engines, "postgresql://odin@postgres,10.1.2.3/odin", "more than one host")
    assert itar_on.calls == [], "refused before anything is looked up"


def test_a_host_in_the_query_is_refused(engines, itar_on):
    _refused_at_start(engines, "postgresql://odin@postgres/odin?host=postgres", "`host=`")
    _refused_at_start(engines, "postgresql://odin@/odin?host=/var/run/postgresql", "`host=`")
    _refused_at_start(engines, "postgresql://odin@/odin?host=postgres:5432&host=10.1.2.3:5433", "`host=`")
    _refused_at_start(engines, "postgresql://odin@postgres/odin?host=,postgres", "`host=`")


def test_a_hostaddr_in_the_query_is_refused(engines, itar_on):
    _refused_at_start(engines, f"postgresql://odin@postgres/odin?hostaddr={PRIVATE}", "`hostaddr=`")
    _refused_at_start(engines, "postgresql://odin@postgres/odin?hostaddr=10.1.1.1,", "`hostaddr=`")


def test_a_port_in_the_query_is_refused(engines, itar_on):
    _refused_at_start(engines, "postgresql://odin@postgres:5432/odin?port=6432", "`port=`")


def test_a_service_in_the_query_is_refused(engines, itar_on):
    _refused_at_start(engines, "postgresql://odin@/odin?service=production", "`service=`")


@pytest.mark.parametrize("name, value", [("PGHOST", "postgres"), ("PGHOSTADDR", PRIVATE), ("PGPORT", "5432"),
                                         ("PGSERVICE", "production"), ("PGSERVICEFILE", "/etc/pg_service.conf"),
                                         ("PGHOST", "")])
def test_a_libpq_environment_override_is_refused(engines, itar_on, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    _refused_at_start(engines, "postgresql://odin@postgres/odin", f"{name} is set in the environment")
    _refused_at_start(engines, "postgresql://odin@/odin", f"{name} is set in the environment")


def test_an_already_resolved_url_object_is_checked(engines, itar_on):
    with pytest.raises(DatabaseConfigurationError):
        create_database_engine(make_url("postgresql+psycopg://odin:pw@db.example.com/odin"))
    with pytest.raises(DatabaseConfigurationError):
        create_database_engine(make_url("postgresql+psycopg://odin:pw@postgres/odin?hostaddr=10.0.0.1"))
    assert engines == []


@pytest.mark.parametrize("driver_arguments, names", [
    ({"host": "postgres,10.1.2.3"}, "more than one host"),
    ({"host": "postgres", "port": "5432,5433"}, "more than one host"),
    ({"host": "postgres", "hostaddr": "10.1.1.1,"}, "`hostaddr`"),
    ({"service": "production"}, "`service`"),
])
def test_the_pin_reads_what_the_driver_is_given_and_refuses_what_it_cannot_pin(engines, itar_on, driver_arguments, names):
    """Belt and braces: whatever reached the driver arguments, only one host can be pinned."""
    create_database_engine("postgresql://odin@postgres/odin")
    with pytest.raises(ItarOutboundBlocked, match=names):
        engines.connect(**driver_arguments)


def test_an_environment_override_that_appears_later_stops_new_connections(engines, itar_on, monkeypatch):
    create_database_engine("postgresql://odin@postgres/odin")
    monkeypatch.setenv("PGHOSTADDR", PUBLIC)
    with pytest.raises(ItarOutboundBlocked, match="PGHOSTADDR"):
        engines.connect(host="postgres")


# ---- ITAR mode off, SQLite ----

@pytest.mark.parametrize("url", [
    "postgresql://odin@db.example.com:5432/odin", f"postgresql://odin@{PUBLIC}/odin",
    f"postgresql://odin@postgres/odin?hostaddr={PUBLIC}", "postgresql://odin@/odin?host=db.example.com:5432&host=postgres:5432",
    f"postgresql://odin@postgres,{PUBLIC}/odin", "postgresql://odin@postgres/odin?port=6432",
])
def test_with_itar_off_nothing_is_checked_refused_or_added(engines, monkeypatch, url):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    for name in PG_ENVIRONMENT:
        monkeypatch.setenv(name, "anything")
    resolver = Resolver(monkeypatch, NAMES)
    create_database_engine(url)
    assert len(engines) == 1 and resolver.calls == []
    assert engines.connect(host="from-the-url") == {"host": "from-the-url"}
    assert engines.listener_names() == ["checkout", "connect"], "the engine has the listeners it had in 2.2.0"
    create_database_engine(url, role="backup")
    assert engines.listener_names() == ["checkout", "connect"], "a backup engine still has none of its own"


def test_under_itar_every_role_gets_the_connect_listener(engines, itar_on):
    for role in ("api", "backup", "restore", "reports"):
        create_database_engine("postgresql://odin@postgres/odin", role=role)
        assert engines.connect(host="postgres")["hostaddr"] == PRIVATE, role


def test_sqlite_is_never_checked(itar_on, tmp_path, monkeypatch):
    monkeypatch.setenv("PGHOST", "db.example.com")
    engine = create_database_engine(f"sqlite:///{tmp_path / 'odin.db'}")
    engine.dispose()
    assert itar_on.calls == []
    assert itar_database.enforce_postgres_destination("sqlite:////data/odin.db") is None


# ---- real SQLAlchemy connection dispatch (recheck 3, finding 1) ----
#
# SQLAlchemy builds ONE connect-arguments dictionary per engine and hands the same object to
# every physical connection. Round 3 wrote `hostaddr` into it: the first connection worked,
# and every later one (pool growth, reconnect, recovery after a failed first attempt) found
# that `hostaddr` and was refused as a form ODIN cannot pin.

class _Driver:
    """A DBAPI module for a real SQLAlchemy engine: sqlite3 in everything but connect(),
    which records the arguments it is given (as psycopg.connect would receive them) and
    returns a working in-memory connection, or fails the way a driver does."""

    def __init__(self):
        import sqlite3
        import types
        self.module = types.ModuleType("recording_dbapi")
        for name in dir(sqlite3):
            if not name.startswith("__"):
                setattr(self.module, name, getattr(sqlite3, name))
        self.module.connect = self.connect
        self.sqlite3, self.received, self.fail_next = sqlite3, [], 0

    def connect(self, *args, **kwargs):
        self.received.append(dict(kwargs))
        if self.fail_next:
            self.fail_next -= 1
            raise self.sqlite3.OperationalError("connection to server failed")
        return self.sqlite3.connect(":memory:")


def _real_engine(monkeypatch, poolclass=None, **connect_args):
    """A real SQLAlchemy engine with ODIN's connect listener on it. The dialect is SQLite's,
    because no PostgreSQL driver is installed here; the part under test (SQLAlchemy's
    do_connect dispatch and its shared arguments dictionary) is the same for every dialect."""
    import sqlalchemy
    from sqlalchemy.pool import NullPool
    driver = _Driver()
    engine = sqlalchemy.create_engine("sqlite://", module=driver.module, poolclass=poolclass or NullPool,
                                      connect_args={"host": "db.plant.example", "port": 5432, **connect_args})
    database_config._pin_postgres_connections_under_itar(engine)
    return engine, driver


def _pins(driver):
    return [(arguments.get("host"), arguments.get("hostaddr")) for arguments in driver.received]


def test_three_physical_connections_in_a_row_each_reach_the_driver_with_a_pin(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    for name in PG_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    resolver = Resolver(monkeypatch, {"db.plant.example": (PRIVATE,)})
    engine, driver = _real_engine(monkeypatch)
    for _ in range(3):
        with engine.connect() as connection:
            assert connection.exec_driver_sql("select 1").scalar() == 1
    assert _pins(driver) == [("db.plant.example", PRIVATE)] * 3
    assert resolver.calls == ["db.plant.example"] * 3, "one fresh lookup for each connection"


def test_each_connection_gets_the_address_of_its_own_lookup(monkeypatch):
    """The database moves to another local address between connections: the second
    connection is pinned to the new answer, not to a pin left over from the first."""
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    answers = iter([(PRIVATE,), ("10.0.0.9",), (PUBLIC,), (PRIVATE,)])
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in next(answers)])
    engine, driver = _real_engine(monkeypatch)
    engine.connect().close()
    engine.connect().close()
    with pytest.raises(ItarOutboundBlocked):
        engine.connect()                       # a public answer: the driver is not called
    engine.connect().close()
    assert _pins(driver) == [("db.plant.example", PRIVATE), ("db.plant.example", "10.0.0.9"),
                             ("db.plant.example", PRIVATE)]


def test_a_listener_added_after_odins_still_runs_and_can_refuse(monkeypatch):
    """ODIN's listener must not end the do_connect dispatch: a listener registered later
    (another guard) still runs, sees the pin, and its refusal still stops the driver."""
    from sqlalchemy import event
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    Resolver(monkeypatch, {"db.plant.example": (PRIVATE,)})
    engine, driver = _real_engine(monkeypatch)
    seen = []

    @event.listens_for(engine, "do_connect")
    def _later_guard(_dialect, _record, _cargs, cparams):
        seen.append(cparams.get("hostaddr"))
        if len(seen) == 2:
            raise RuntimeError("refused by the later guard")

    engine.connect().close()
    with pytest.raises(RuntimeError, match="later guard"):
        engine.connect()
    engine.connect().close()
    assert seen == [None] * 3, "the shared arguments never carry a pin"
    assert _pins(driver) == [("db.plant.example", PRIVATE)] * 2, "the refused connection never reached the driver"


@pytest.mark.parametrize("change", [lambda c: c.__setitem__("hostaddr", PUBLIC),
                                    lambda c: c.__setitem__("host", "elsewhere.example,db.plant.example")],
                         ids=["adds-a-public-hostaddr", "names-two-hosts"])
def test_what_a_later_listener_changes_is_still_checked_before_the_driver(monkeypatch, change):
    from sqlalchemy import event
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    Resolver(monkeypatch, {"db.plant.example": (PRIVATE,)})
    engine, driver = _real_engine(monkeypatch)
    event.listen(engine, "do_connect", lambda _d, _r, _a, cparams: change(cparams))
    with pytest.raises(ItarOutboundBlocked):
        engine.connect()
    assert driver.received == []


def test_two_connections_opened_at_once_each_use_their_own_checked_address(monkeypatch):
    """Thread A's lookup answers 10.0.0.1 and thread B's 10.0.0.2; each driver call must
    carry the address of its own lookup, whatever the interleaving."""
    import threading
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    local = threading.local()
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (local.address, 0))])
    engine, driver = _real_engine(monkeypatch)
    barrier = threading.Barrier(2)
    seen = {}
    original = driver.module.connect

    def connect(*cargs, **arguments):
        seen[threading.current_thread().name] = arguments.get("hostaddr")
        return original(*cargs, **arguments)
    monkeypatch.setattr(driver.module, "connect", connect)

    def open_one(address):
        local.address = address
        barrier.wait()
        engine.connect().close()
    threads = [threading.Thread(target=open_one, args=(address,), name=address) for address in ("10.0.0.1", "10.0.0.2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert seen == {"10.0.0.1": "10.0.0.1", "10.0.0.2": "10.0.0.2"}


def test_a_hostaddr_odin_did_not_write_is_refused_on_any_connection(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    Resolver(monkeypatch, {"db.plant.example": (PRIVATE,)})
    engine, driver = _real_engine(monkeypatch, hostaddr=PUBLIC)
    with pytest.raises(ItarOutboundBlocked):
        engine.connect()
    assert driver.received == []


def test_a_reconnect_after_a_failed_first_attempt_works(monkeypatch):
    """The database is not up when ODIN first connects. The retry must not be refused."""
    import sqlalchemy.exc
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    Resolver(monkeypatch, {"db.plant.example": (PRIVATE,)})
    from sqlalchemy.pool import QueuePool
    engine, driver = _real_engine(monkeypatch, poolclass=QueuePool)
    driver.fail_next = 1
    with pytest.raises(sqlalchemy.exc.OperationalError):
        engine.connect()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("select 1").scalar() == 1
    assert _pins(driver) == [("db.plant.example", PRIVATE)] * 2


def test_a_pool_that_grows_and_reconnects_keeps_working(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    Resolver(monkeypatch, {"db.plant.example": (PRIVATE,)})
    from sqlalchemy.pool import QueuePool
    engine, driver = _real_engine(monkeypatch, poolclass=QueuePool)
    first, second = engine.connect(), engine.connect()        # the pool opens a second connection
    first.invalidate()                                        # a dropped connection is replaced
    assert first.exec_driver_sql("select 1").scalar() == 1
    first.close()
    second.close()
    engine.dispose()
    engine.connect().close()                                  # and after the pool is emptied
    assert _pins(driver) == [("db.plant.example", PRIVATE)] * 4


def test_a_hostaddr_the_caller_supplied_is_still_refused_on_every_connection(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    Resolver(monkeypatch, {"db.plant.example": (PRIVATE,)})
    engine, driver = _real_engine(monkeypatch, hostaddr="10.1.1.1,")
    for _ in range(2):
        with pytest.raises(ItarOutboundBlocked, match="`hostaddr`"):
            engine.connect()
    assert driver.received == []


def test_with_itar_off_a_real_engine_gets_no_listener_and_no_pin(monkeypatch):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    resolver = Resolver(monkeypatch, {})
    engine, driver = _real_engine(monkeypatch)
    for _ in range(2):
        engine.connect().close()
    assert _pins(driver) == [("db.plant.example", None)] * 2 and resolver.calls == []


def test_the_real_driver_is_called_with_the_checked_address(monkeypatch, tmp_path):
    """The real PostgreSQL dialect and a replaced psycopg.connect, three connections in a
    row. Skipped where psycopg is not installed (it is not on the development Mac; CI has
    it); the tests above cover the same dispatch with SQLite's dialect."""
    psycopg = pytest.importorskip("psycopg")
    secret = tmp_path / "pw"
    secret.write_text("x\n")
    secret.chmod(0o600)
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    for name in PG_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    Resolver(monkeypatch, NAMES)
    monkeypatch.setattr(database_config, "assert_postgres_restore_ready", lambda *a, **k: None)
    given = []

    class Stop(Exception):
        pass

    def connect(*args, **kwargs):
        given.append(kwargs)
        raise Stop()
    monkeypatch.setattr(psycopg, "connect", connect)
    engine = create_database_engine("postgresql://odin@postgres:5432/odin", role="backup", password_file=str(secret))
    for _ in range(3):
        with pytest.raises(Stop):
            engine.connect()
    assert [(kwargs["host"], kwargs["hostaddr"]) for kwargs in given] == [("postgres", PRIVATE)] * 3


# ---- the PostgreSQL client programs (pg_dump, pg_restore, psql) ----

@pytest.fixture
def clients(monkeypatch, tmp_path):
    from modules.system import postgres_backup_service as service
    started = []
    secret = tmp_path / "db-password"
    secret.write_text("secret\n")
    secret.chmod(0o600)
    for name in PG_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(command, **kwargs):
        started.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")
    monkeypatch.setattr(service.subprocess, "run", run)
    return service, started, str(secret), tmp_path


def _dump(url):
    return ["pg_dump", "--format=custom", "--file", "/tmp/x.dump", "--dbname", url]


TARGET = "postgresql://odin@postgres/odin"


@pytest.mark.parametrize("command, target", [
    (_dump("postgresql://odin@db.example.com/odin?application_name=odin-backup"), TARGET),
    (["pg_restore", f"--dbname=postgresql://odin_maintenance@{PUBLIC}/validation", "/tmp/x.dump"], TARGET),
    (["psql", "-d", "postgresql://odin@mixed.example.com/odin", "-c", "select 1"], TARGET),
    (["pg_restore", "--list", "/tmp/x.dump"], "postgresql://odin@db.example.com/odin"),
    (_dump("postgresql://odin@postgres,10.1.2.3/odin"), TARGET),                        # more than one host
    (_dump(f"postgresql://odin@postgres/odin?hostaddr={PRIVATE}"), TARGET),             # hostaddr in the query
    (_dump("postgresql://odin@postgres/odin?port=6432"), TARGET),
    (_dump("postgresql://odin@/odin?host=postgres"), TARGET),
    (["pg_dump", "--dbname", TARGET, "-d", "postgresql://odin@10.1.2.3/other"], TARGET),  # two servers
])
def test_no_client_is_started_against_a_server_odin_cannot_pin(clients, itar_on, command, target):
    service, started, secret, directory = clients
    with pytest.raises(service.RestoreEnvironmentError, match="ITAR mode is on"):
        service._run_pg_tool(command, target, secret, directory)
    assert started == []


@pytest.mark.parametrize("name", PG_ENVIRONMENT)
def test_a_client_is_not_started_with_a_libpq_environment_override(clients, itar_on, monkeypatch, name):
    service, started, secret, directory = clients
    monkeypatch.setenv(name, PRIVATE)
    with pytest.raises(service.RestoreEnvironmentError, match=name):
        service._run_pg_tool(_dump(TARGET), TARGET, secret, directory)
    assert started == []


def test_a_client_program_is_given_the_one_checked_address(clients, monkeypatch):
    service, started, secret, directory = clients
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    answers = iter([(PRIVATE,), (PRIVATE, "10.0.0.4"), (PUBLIC,)])
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in next(answers)])
    url = "postgresql://odin_maintenance@db.plant.example:5432/postgres?application_name=odin-restore"
    assert service._run_pg_tool(_dump(url), "postgresql://odin@db.plant.example/odin", secret, directory) == "ok"
    command, environment = started[0]
    assert environment["PGHOSTADDR"] == PRIVATE, "pg_dump connects to this address and looks nothing up"
    assert "db.plant.example" in " ".join(command), "the name stays in the URL for TLS verification"


def test_a_client_that_reads_an_archive_or_uses_the_socket_gets_nothing_added(clients, itar_on):
    service, started, secret, directory = clients
    service._run_pg_tool(["pg_restore", "--list", "/tmp/x.dump"], TARGET, secret, directory)
    service._run_pg_tool(_dump("postgresql://odin@/odin"), TARGET, secret, directory)
    assert ["PGHOSTADDR" in environment for _command, environment in started] == [False, False]


def test_with_itar_off_the_clients_are_unchanged(clients, monkeypatch):
    service, started, secret, directory = clients
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    monkeypatch.setenv("PGPORT", "6000")
    resolver = Resolver(monkeypatch, NAMES)
    url = "postgresql://odin@db.example.com,other/odin?hostaddr=8.8.8.8,"
    assert service._run_pg_tool(_dump(url), url, secret, directory) == "ok"
    assert len(started) == 1 and resolver.calls == [] and "PGHOSTADDR" not in started[0][1]


# ---- structure ----

def test_every_engine_and_database_client_goes_through_the_two_checked_functions():
    """create_engine for PostgreSQL is called in one place, and a client program is given a
    server only by _run_pg_tool. The two other pg_restore calls read an archive file and
    are given no server (no --dbname), so they connect to nothing."""
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


def test_the_list_handling_code_is_gone():
    """Round 3 deleted the code that mirrored libpq's host lists and precedence."""
    source = Path(itar_database.__file__).read_text(encoding="utf-8")
    for name in ("postgres_destinations", "pinned_hostaddr", "_split_entry", "_values", "enforce_postgres_command"):
        assert not hasattr(itar_database, name) and f"def {name}" not in source
    assert itar_database.REFUSED_QUERY_KEYS == ("host", "hostaddr", "port", "service")
    assert itar_database.REFUSED_ENVIRONMENT == ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE")
