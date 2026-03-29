"""
O.D.I.N. — Database compatibility layer.

Provides dialect-aware SQL helpers so module code works on both SQLite and PostgreSQL
without scattering if/else checks everywhere.

Usage:
    from core.db_compat import sql

    # Instead of: datetime('now')
    sql.now()  # Returns "datetime('now')" for SQLite, "NOW()" for PostgreSQL

    # Instead of: strftime('%Y-%m-%d', timestamp)
    sql.date_format(column, '%Y-%m-%d')

    # Instead of: INSERT OR REPLACE
    sql.upsert_sql(table, columns, conflict_column)

    # Instead of: LIKE (case-insensitive)
    sql.ilike(column, pattern)
"""

from core.db import IS_SQLITE, IS_POSTGRES


class _SQLCompat:
    """Dialect-aware SQL fragment generator."""

    @staticmethod
    def now() -> str:
        """Current timestamp expression."""
        return "datetime('now')" if IS_SQLITE else "NOW()"

    @staticmethod
    def now_local() -> str:
        """Current local timestamp expression."""
        return "datetime('now', 'localtime')" if IS_SQLITE else "NOW()"

    @staticmethod
    def now_offset(offset: str, local: bool = False) -> str:
        """Current timestamp with an offset.

        Args:
            offset: SQLite modifier string, e.g. '-5 minutes', '-90 days', '-24 hours'.
            local: If True, use localtime (SQLite: datetime('now', 'localtime', offset)).

        SQLite:  datetime('now', '-5 minutes') or datetime('now', 'localtime', '-2 hours')
        PostgreSQL: NOW() + INTERVAL '-5 minutes'
        """
        if IS_SQLITE:
            if local:
                return f"datetime('now', 'localtime', '{offset}')"
            return f"datetime('now', '{offset}')"
        return f"NOW() + INTERVAL '{offset}'"

    @staticmethod
    def date_format(column: str, fmt: str) -> str:
        """Format a timestamp column as a date string.

        SQLite: strftime('%Y-%m-%d', column)
        PostgreSQL: TO_CHAR(column, 'YYYY-MM-DD')
        """
        if IS_SQLITE:
            return f"strftime('{fmt}', {column})"

        # Convert SQLite format codes to PostgreSQL
        pg_fmt = (
            fmt.replace("%Y", "YYYY")
            .replace("%m", "MM")
            .replace("%d", "DD")
            .replace("%H", "HH24")
            .replace("%M", "MI")
            .replace("%S", "SS")
            .replace("%w", "D")
        )
        return f"TO_CHAR({column}, '{pg_fmt}')"

    @staticmethod
    def date_trunc(unit: str, column: str) -> str:
        """Truncate a timestamp to a given unit (day, hour, month, etc.).

        SQLite: date(column) for day, strftime('%Y-%m', column) for month
        PostgreSQL: DATE_TRUNC('day', column)
        """
        if IS_SQLITE:
            if unit == "day":
                return f"date({column})"
            elif unit == "month":
                return f"strftime('%Y-%m', {column})"
            elif unit == "year":
                return f"strftime('%Y', {column})"
            elif unit == "hour":
                return f"strftime('%Y-%m-%d %H:00:00', {column})"
            return f"date({column})"
        return f"DATE_TRUNC('{unit}', {column})"

    @staticmethod
    def ilike(column: str, pattern: str) -> str:
        """Case-insensitive LIKE.

        SQLite: LIKE is already case-insensitive for ASCII
        PostgreSQL: Must use ILIKE
        """
        if IS_SQLITE:
            return f"{column} LIKE {pattern}"
        return f"{column} ILIKE {pattern}"

    @staticmethod
    def upsert_prefix() -> str:
        """INSERT OR REPLACE / INSERT ... ON CONFLICT prefix.

        Returns the dialect-appropriate upsert syntax.
        For PostgreSQL, use with on_conflict_suffix().
        """
        if IS_SQLITE:
            return "INSERT OR REPLACE INTO"
        return "INSERT INTO"

    @staticmethod
    def on_conflict_suffix(conflict_column: str, update_columns: list[str]) -> str:
        """ON CONFLICT clause for PostgreSQL upsert.

        SQLite uses INSERT OR REPLACE which handles this automatically.
        PostgreSQL needs an explicit ON CONFLICT DO UPDATE clause.
        """
        if IS_SQLITE:
            return ""
        updates = ", ".join(f"{col} = EXCLUDED.{col}" for col in update_columns)
        return f" ON CONFLICT ({conflict_column}) DO UPDATE SET {updates}"

    @staticmethod
    def insert_or_ignore_prefix() -> str:
        """INSERT OR IGNORE / INSERT ... ON CONFLICT DO NOTHING."""
        if IS_SQLITE:
            return "INSERT OR IGNORE INTO"
        return "INSERT INTO"

    @staticmethod
    def on_conflict_ignore(conflict_column: str) -> str:
        """ON CONFLICT DO NOTHING clause for PostgreSQL."""
        if IS_SQLITE:
            return ""
        return f" ON CONFLICT ({conflict_column}) DO NOTHING"

    @staticmethod
    def boolean(value: bool) -> str:
        """Boolean literal."""
        if IS_SQLITE:
            return "1" if value else "0"
        return "TRUE" if value else "FALSE"

    @staticmethod
    def json_extract(column: str, path: str) -> str:
        """Extract a value from a JSON column.

        SQLite: json_extract(column, '$.path')
        PostgreSQL: column->>'path'
        """
        if IS_SQLITE:
            return f"json_extract({column}, '$.{path}')"
        return f"{column}->>'{path}'"

    @staticmethod
    def pragma_integrity_check() -> str | None:
        """Return PRAGMA integrity_check SQL or None for non-SQLite."""
        if IS_SQLITE:
            return "PRAGMA integrity_check"
        return None

    @property
    def is_sqlite(self) -> bool:
        return IS_SQLITE

    @property
    def is_postgres(self) -> bool:
        return IS_POSTGRES


# Singleton instance — import as: from core.db_compat import sql
sql = _SQLCompat()
