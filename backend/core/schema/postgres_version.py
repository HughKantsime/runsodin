"""PostgreSQL startup version policy, independent of schema bootstrap."""

import logging

from core.schema.migrator import MigrationError


TESTED_POSTGRES_MAJOR = 16
FIRST_REFUSED_POSTGRES_MAJOR = 18

logger = logging.getLogger(__name__)


class UnsupportedPostgresVersion(MigrationError):
    """The server version cannot run ODIN; this is not an archive fault."""


def check_postgres_version(connection, *, warn_untested=True):
    major = int(connection.exec_driver_sql("SHOW server_version_num").scalar_one()) // 10000
    version = connection.exec_driver_sql("SHOW server_version").scalar_one()
    if major >= FIRST_REFUSED_POSTGRES_MAJOR:
        raise UnsupportedPostgresVersion(
            f"ODIN does not support PostgreSQL {major} yet. "
            f"This server is PostgreSQL {version}. Nothing was changed. "
            f"Use PostgreSQL {TESTED_POSTGRES_MAJOR}, the version ODIN ships with "
            "(docker-compose.enterprise.yml provides one)."
        )
    if major != TESTED_POSTGRES_MAJOR and warn_untested:
        logger.warning(
            "ODIN is tested on PostgreSQL %s. This server is PostgreSQL %s.",
            TESTED_POSTGRES_MAJOR, version,
        )
