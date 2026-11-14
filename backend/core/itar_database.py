"""ITAR mode for the PostgreSQL database connection.

The database is ODIN's own store, but it is still a connection: with ITAR mode on, a
DATABASE_URL (or DATABASE_MAINTENANCE_URL) that names a public PostgreSQL server would send
every row to it. Under ITAR mode ODIN connects only to a PostgreSQL server on the local
network, and to the address it checked.

libpq can be told where to connect in many ways (several hosts, `host` / `hostaddr` / `port`
/ `service` in the URL query, PGHOST and friends in the environment), with its own rules
for which one wins. ODIN does not try to mirror those rules. Under ITAR mode it accepts
exactly one form, which it can pin with certainty, and REFUSES every other form at start:

  accepted   one host in the URL authority (a name or an IP address, with an optional port):
             postgresql://user@db.plant.example:5432/odin
             or no host at all (libpq's default Unix socket): postgresql://user@/odin
  refused    more than one host; `host`, `hostaddr`, `port` or `service` in the URL query;
             PGHOST, PGHOSTADDR, PGPORT, PGSERVICE or PGSERVICEFILE in the environment.

The pin: each time the engine opens a connection, the one host the driver is about to be
given is looked up once, every address it has must be on the local network, and the first
address (in the resolver's order) is added to the driver's arguments as `hostaddr`. libpq
then connects to that address without a lookup of its own and still verifies a TLS
certificate against `host`. A client program (pg_dump, pg_restore, psql) gets the same one
address in PGHOSTADDR. A Unix socket needs no pin. SQLite has no connection.

With ITAR mode off nothing here runs.
"""
from __future__ import annotations

import os

from sqlalchemy.engine import URL, make_url

from core.itar import ItarOutboundBlocked, is_itar_mode

DEFAULT_PORT = 5432
_POSTGRES_BACKENDS = frozenset({"postgresql", "postgres"})
REFUSED_QUERY_KEYS = ("host", "hostaddr", "port", "service")
REFUSED_ENVIRONMENT = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE")
ACCEPTED_FORM = ("With ITAR mode on, DATABASE_URL must name the PostgreSQL server as one host in the "
                 "address part, for example postgresql://user@db.example:5432/odin, or no host for the "
                 "local socket.")
REFUSAL = ("ITAR mode is on and the PostgreSQL server address is not on the local network, or its "
           "name could not be looked up. ODIN will not connect to it.")


def _refuse(what: str):
    return ItarOutboundBlocked(f"ITAR mode is on and {what}. ODIN cannot be certain which server that "
                               f"connects to, so it does not start. {ACCEPTED_FORM}")


def is_postgres(url: URL) -> bool:
    return url.get_backend_name() in _POSTGRES_BACKENDS


def refuse_unpinnable_forms(url: URL, environ=None) -> None:
    """Raise ItarOutboundBlocked for every way of naming the server other than the one
    accepted form (module docstring). Looks nothing up."""
    environ = os.environ if environ is None else environ
    for name in REFUSED_ENVIRONMENT:
        if name in environ:
            raise _refuse(f"{name} is set in the environment")
    for key in REFUSED_QUERY_KEYS:
        if key in url.query:
            raise _refuse(f"the database URL has `{key}=` after the `?`")
    if "," in (url.host or ""):
        raise _refuse("the database URL names more than one host")


def _is_socket(host) -> bool:
    return not host or str(host).startswith(("/", "@"))


def checked_address(host: str, port) -> str:
    """The address to connect to for `host`: one lookup, every answer on the local network,
    the first answer in the resolver's order. Raises ItarOutboundBlocked (REFUSAL)."""
    from core.itar_connect import local_network_addresses
    try:
        return local_network_addresses(str(host), int(port or DEFAULT_PORT), "postgresql")[0]
    except (ItarOutboundBlocked, ValueError) as exc:
        raise ItarOutboundBlocked(REFUSAL) from exc


def _parsed(database_url) -> URL:
    try:
        return database_url if isinstance(database_url, URL) else make_url(database_url)
    except Exception as exc:
        raise ItarOutboundBlocked("ITAR mode is on and the database URL could not be read") from exc


def enforce_postgres_destination(database_url, environ=None):
    """Start-up check. Under ITAR mode, raise ItarOutboundBlocked unless `database_url` is
    SQLite, or a PostgreSQL URL in the accepted form whose one host is on the local network.
    Returns the checked address (None for a socket, SQLite, or ITAR mode off)."""
    if not is_itar_mode():
        return None
    url = _parsed(database_url)
    if not is_postgres(url):
        return None
    refuse_unpinnable_forms(url, environ)
    return None if _is_socket(url.host) else checked_address(url.host, url.port)


def pinned_driver_arguments(cparams: dict, environ=None) -> dict:
    """Connection time. `cparams` is what SQLAlchemy's dialect built for the driver. Returns
    a NEW dictionary for this one connection: the same arguments plus, under ITAR mode,
    `hostaddr`, the freshly checked address of the one `host` in them.

    `cparams` is never changed. SQLAlchemy keeps that one dictionary for the life of the
    engine and hands it to every connection: a pin written into it would be found again by
    the next connection (and refused as a `hostaddr` ODIN did not put there), and would go
    stale. Refuses (ItarOutboundBlocked, so no connection is opened) anything but one host
    or a socket."""
    arguments = dict(cparams)
    if not is_itar_mode():
        return arguments
    environ = os.environ if environ is None else environ
    for name in REFUSED_ENVIRONMENT:
        if name in environ:
            raise _refuse(f"{name} is set in the environment")
    for key in ("hostaddr", "service"):
        if key in arguments:
            raise _refuse(f"the database connection carries `{key}`")
    host = arguments.get("host")
    if _is_socket(host):
        return arguments
    if "," in str(host) or "," in str(arguments.get("port") or ""):
        raise _refuse("the database connection names more than one host")
    arguments["hostaddr"] = checked_address(host, arguments.get("port"))
    return arguments


def _dbname_arguments(command) -> list:
    arguments = [str(item) for item in command]
    found = []
    for index, item in enumerate(arguments):
        if item in ("--dbname", "-d") and index + 1 < len(arguments):
            found.append(arguments[index + 1])
        elif item.startswith("--dbname="):
            found.append(item.split("=", 1)[1])
    return found


def pinned_client_environment(command, environment, database_url=None) -> dict:
    """`environment` for a PostgreSQL client program. Under ITAR mode the server the command
    names (its one --dbname / -d URL) must be in the accepted form and on the local network,
    and PGHOSTADDR is set to its checked address, so the program connects there without
    looking the name up. Returned unchanged with ITAR mode off and for a command that names
    no server (it reads an archive file). Raises ItarOutboundBlocked."""
    if not is_itar_mode():
        return environment
    if database_url:
        enforce_postgres_destination(database_url)
    targets = _dbname_arguments(command)
    if len(targets) > 1:
        raise _refuse("a PostgreSQL client was given more than one server")
    if not targets:
        return environment
    address = enforce_postgres_destination(targets[0])
    return {**environment, "PGHOSTADDR": address} if address else environment
