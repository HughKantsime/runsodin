"""ITAR mode for the PostgreSQL database connection.

The database is ODIN's own store, but it is still a connection: with ITAR mode on, a
DATABASE_URL (or DATABASE_MAINTENANCE_URL) that names a public PostgreSQL server would send
every row to it. Under ITAR mode ODIN refuses to build an engine for, or start a PostgreSQL
client (pg_dump, pg_restore, psql) against, a server whose host is not on the local network,
and it connects to the address it checked.

Checked: the host in the URL, the libpq overrides `host` and `hostaddr` in the URL query,
and the PGHOST and PGHOSTADDR environment variables (libpq reads those itself). A libpq
service (`service=` in the query, PGSERVICE) can name a host in a file ODIN does not read,
so it is refused. A Unix socket directory, `localhost` and an empty host (libpq's default
socket) pass. SQLite has no connection and is never checked.

Pinned, through libpq's own `hostaddr`: libpq connects to `hostaddr` without looking the
name up and keeps `host` for TLS certificate verification. Each host name is looked up
once, every address it has must be local, and one of those addresses is handed to libpq:
  - the SQLAlchemy engine gets it as the `hostaddr` connection parameter each time it opens
    a connection (a new lookup, a new check, and the connection goes to that answer), so
    pooled and later connections are covered and a database that moves to another local
    address is still found;
  - a client program gets it in PGHOSTADDR.
With several hosts there is one address per host, in the same order (a socket entry gets an
empty one). A `hostaddr` already in the URL, or PGHOSTADDR in the environment, must be local
addresses and is used as given.

With ITAR mode off nothing here runs.
"""
from __future__ import annotations

import os

from sqlalchemy.engine import URL, make_url

from core.itar import ItarOutboundBlocked, is_itar_mode

DEFAULT_PORT = 5432
_POSTGRES_BACKENDS = frozenset({"postgresql", "postgres"})
REFUSAL = ("ITAR mode is on and the PostgreSQL server address is not on the local network "
           "(checked: the DATABASE_URL host, host/hostaddr in its query, PGHOST and PGHOSTADDR). "
           "ODIN will not connect to it.")
SERVICE_REFUSAL = ("ITAR mode is on and a PostgreSQL service file is in use (service= or PGSERVICE). "
                   "ODIN cannot check the address it names; put the host in DATABASE_URL instead.")


def _is_socket(entry: str) -> bool:
    return entry.startswith("/") or entry.startswith("@")


def _split_entry(entry: str, port):
    """(host, port) for one libpq host entry: name, name:port, [v6], [v6]:port or a bare v6."""
    entry = entry.strip()
    if entry.startswith("["):
        host, _, rest = entry[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else port
    if entry.count(":") == 1:
        host, _, value = entry.partition(":")
        return host, int(value) if value.isdigit() else port
    return entry, port


def _values(value) -> list[str]:
    """A query value (a string or a tuple of strings), split on libpq's comma lists."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    return [part.strip() for item in items for part in str(item).split(",") if part.strip()]


def postgres_destinations(url: URL, environ=None) -> list[tuple[str, int]]:
    """Every (host, port) libpq could connect to for `url`, sockets left out.

    Raises ItarOutboundBlocked when a libpq service is in use.
    """
    environ = os.environ if environ is None else environ
    if url.query.get("service") or (environ.get("PGSERVICE") or "").strip():
        raise ItarOutboundBlocked(SERVICE_REFUSAL)
    ports = [url.port, *_values(url.query.get("port")), environ.get("PGPORT")]
    try:
        port = int(next(value for value in ports if value))
    except (StopIteration, ValueError):
        port = DEFAULT_PORT
    entries = _values(url.host) + _values(url.query.get("host")) + _values(url.query.get("hostaddr"))
    entries += _values(environ.get("PGHOST")) + _values(environ.get("PGHOSTADDR"))
    found: list[tuple[str, int]] = []
    for entry in entries:
        if _is_socket(entry):
            continue
        destination = _split_entry(entry, port)
        if destination[0] and destination not in found:
            found.append(destination)
    return found


def _literal_local(entry: str) -> bool:
    import ipaddress
    from core.itar import _is_local_address
    try:
        return _is_local_address(ipaddress.ip_address(entry.strip("[]").split("%", 1)[0]))
    except ValueError:
        return False   # libpq wants a numeric address here; a name is not one


def pinned_hostaddr(database_url, environ=None):
    """Check a PostgreSQL URL under ITAR mode and return the `hostaddr` value libpq must
    connect with, or None when there is nothing to add.

    None: ITAR mode off, SQLite, a Unix socket, or a URL that already carries `hostaddr`
    (checked here, used as given). Otherwise the comma list of checked addresses, one per
    host in libpq's order, or PGHOSTADDR as given when the environment sets it.
    Each name is looked up ONCE here; the caller hands the result to libpq, which then
    looks nothing up. Raises ItarOutboundBlocked for a server that is not on the local
    network, a name that does not resolve, or a libpq service.
    """
    if not is_itar_mode():
        return None
    environ = os.environ if environ is None else environ
    try:
        url = database_url if isinstance(database_url, URL) else make_url(database_url)
    except Exception as exc:
        raise ItarOutboundBlocked("ITAR mode is on and the database URL could not be read") from exc
    if url.get_backend_name() not in _POSTGRES_BACKENDS:
        return None
    from core.itar_connect import local_network_addresses
    postgres_destinations(url, environ)   # refuses a libpq service
    ports = [url.port, *_values(url.query.get("port")), environ.get("PGPORT")]
    try:
        port = int(next(value for value in ports if value))
    except (StopIteration, ValueError):
        port = DEFAULT_PORT
    in_url = _values(url.query.get("hostaddr"))
    in_environment = _values(environ.get("PGHOSTADDR"))
    if not all(_literal_local(entry) for entry in in_url + in_environment):
        raise ItarOutboundBlocked(REFUSAL)
    url_hosts = _values(url.host) + _values(url.query.get("host"))
    environment_hosts = _values(environ.get("PGHOST"))
    pinned = []
    try:
        # libpq uses PGHOST only when the URL names no host; it is checked either way.
        for index, entry in enumerate(url_hosts + environment_hosts):
            used = index < len(url_hosts) or not url_hosts
            if _is_socket(entry):
                address = ""
            else:
                host, host_port = _split_entry(entry, port)
                addresses = local_network_addresses(host, host_port, "postgresql")
                address = next((item for item in addresses if ":" not in item), addresses[0])
            if used:
                pinned.append(address)
    except ItarOutboundBlocked as exc:
        raise ItarOutboundBlocked(REFUSAL) from exc
    if in_url:
        return None
    if in_environment:
        return ",".join(in_environment)
    return ",".join(pinned) if any(pinned) else None


def enforce_postgres_destination(database_url, environ=None) -> None:
    """Raise ItarOutboundBlocked when ITAR mode is on and `database_url` (a string or a
    SQLAlchemy URL) is a PostgreSQL URL whose server is not on the local network.
    No-op with ITAR mode off and for SQLite. Callers that go on to connect use
    pinned_hostaddr, which makes the same check and returns the address to connect to."""
    pinned_hostaddr(database_url, environ)


def _dbname_arguments(command) -> list:
    arguments = [str(item) for item in command]
    found = []
    for index, item in enumerate(arguments):
        if item in ("--dbname", "-d") and index + 1 < len(arguments):
            found.append(arguments[index + 1])
        elif item.startswith("--dbname="):
            found.append(item.split("=", 1)[1])
    return found


def enforce_postgres_command(command, database_url=None, environ=None) -> None:
    """The same check for a PostgreSQL client command line: every --dbname / -d value, and
    `database_url` when given. No-op with ITAR mode off."""
    if not is_itar_mode():
        return
    for target in ([database_url] if database_url else []) + _dbname_arguments(command):
        enforce_postgres_destination(target, environ)


def pinned_client_environment(command, environment, database_url=None) -> dict:
    """`environment` for a PostgreSQL client program, with PGHOSTADDR set to the checked
    address of the server the command names (its --dbname / -d value), so the program
    connects there without looking the name up. Returned unchanged with ITAR mode off, for
    a command that names no server, and when the URL carries its own hostaddr.
    Raises ItarOutboundBlocked like enforce_postgres_command, which it includes."""
    if not is_itar_mode():
        return environment
    if database_url:
        enforce_postgres_destination(database_url, environment)
    pinned = [pinned_hostaddr(target, environment) for target in _dbname_arguments(command)]
    pinned = [value for value in pinned if value]
    if len(pinned) > 1:
        raise ItarOutboundBlocked("ITAR mode is on and a PostgreSQL client was given more than one server")
    return {**environment, "PGHOSTADDR": pinned[0]} if pinned else environment
