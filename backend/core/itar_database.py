"""ITAR mode for the PostgreSQL database connection.

The database is ODIN's own store, but it is still a connection: with ITAR mode on, a
DATABASE_URL (or DATABASE_MAINTENANCE_URL) that names a public PostgreSQL server would send
every row to it. Under ITAR mode ODIN refuses to build an engine for, or start a PostgreSQL
client (pg_dump, pg_restore, psql) against, a server whose host is not on the local network.

What is checked: the host in the URL, the libpq overrides `host` and `hostaddr` in the URL
query, and the PGHOST and PGHOSTADDR environment variables (libpq reads those itself, and
PGHOSTADDR replaces the address even when the URL names a host). A libpq service
(`service=` in the query, PGSERVICE) can name a host in a file ODIN does not read, so it is
refused. A Unix socket directory, `localhost` and an empty host (libpq's default socket)
pass. SQLite has no connection and is never checked.

What is NOT done: the connection is not pinned. libpq (psycopg and the PostgreSQL client
programs) looks the host name up again each time it connects, and ODIN does not replace
that lookup. The check here is repeated every time an engine is built or a client program
is started, not on each new pooled connection. docs/ITAR_MODE.md says so: use an IP
address, a Unix socket, or a name your own resolver controls.

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


def enforce_postgres_destination(database_url, environ=None) -> None:
    """Raise ItarOutboundBlocked when ITAR mode is on and `database_url` (a string or a
    SQLAlchemy URL) is a PostgreSQL URL whose server is not on the local network.

    Each name is looked up once and every address it has must be local; a name that does
    not resolve is refused. No-op with ITAR mode off and for SQLite.
    """
    if not is_itar_mode():
        return
    try:
        url = database_url if isinstance(database_url, URL) else make_url(database_url)
    except Exception as exc:
        raise ItarOutboundBlocked("ITAR mode is on and the database URL could not be read") from exc
    if url.get_backend_name() not in _POSTGRES_BACKENDS:
        return
    from core.itar_connect import local_network_addresses
    for host, port in postgres_destinations(url, environ):
        try:
            local_network_addresses(host, port, "postgresql")
        except ItarOutboundBlocked as exc:
            raise ItarOutboundBlocked(REFUSAL) from exc


def enforce_postgres_command(command, database_url=None, environ=None) -> None:
    """The same check for a PostgreSQL client command line: every --dbname / -d value, and
    `database_url` when given. No-op with ITAR mode off."""
    if not is_itar_mode():
        return
    targets = [database_url] if database_url else []
    arguments = [str(item) for item in command]
    for index, item in enumerate(arguments):
        if item in ("--dbname", "-d") and index + 1 < len(arguments):
            targets.append(arguments[index + 1])
        elif item.startswith("--dbname="):
            targets.append(item.split("=", 1)[1])
    for target in targets:
        enforce_postgres_destination(target, environ)
