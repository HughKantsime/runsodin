"""
Contract tests — Schema consistency between ORM models and SQL migrations.

Audits the DUAL SCHEMA problem: some tables exist in both SQLAlchemy ORM
(Base.metadata.create_all) and raw SQL migration files. This test ensures
columns match between the two definitions.

Findings from audit:
~~~~~~~~~~~~~~~~~~~~~
ORM-managed tables (Base.metadata.create_all):
  core/models.py:
    - system_config (key, value, updated_at)
    - audit_logs (id, timestamp, action, entity_type, entity_id, details, ip_address)
  modules/printers/models.py:
    - printers (27+ columns — see model for full list)
    - filament_slots (id, printer_id, slot_number, filament_type, color, color_hex,
                      spoolman_spool_id, assigned_spool_id, spool_confirmed, loaded_at)
    - nozzle_lifecycle (id, printer_id, nozzle_type, nozzle_diameter, installed_at,
                        removed_at, print_hours_accumulated, print_count, notes)
  modules/jobs/models.py:
    - jobs (30+ columns)
    - scheduler_runs (id, run_at, total_jobs, scheduled_count, skipped_count,
                      setup_blocks, avg_match_score, avg_job_duration, notes)
    - print_presets (id, name, model_id, item_name, quantity, priority,
                     duration_hours, colors_required, filament_type, required_tags,
                     notes, created_at)
  modules/inventory/models.py:
    - filament_library, spools, spool_usage, drying_logs,
      consumables, product_consumables, consumable_usage
  modules/models_library/models.py:
    - models (id, name, build_time_hours, default_filament_type,
              color_requirements, category, thumbnail_url, thumbnail_b64,
              print_file_id, notes, cost_per_item, units_per_bed,
              quantity_per_bed, markup_percent, is_favorite, org_id,
              created_at, updated_at)
  modules/orders/models.py:
    - orders, order_items, products, product_components
  modules/notifications/models.py:
    - alerts, alert_preferences, push_subscriptions
  modules/archives/models.py:
    - timelapses (DUAL SCHEMA — also in entrypoint.sh historically)
  modules/vision/models.py:
    - vision_detections, vision_settings, vision_models (DUAL SCHEMA — also in entrypoint.sh historically)
  modules/system/models.py:
    - maintenance_tasks, maintenance_logs

Raw-SQL-only tables (module migrations):
  core/migrations/001_initial.sql:
    - users, api_tokens, active_sessions, token_blacklist,
      password_reset_tokens, login_attempts
  printers/migrations/001_initial.sql:
    - printer_telemetry, hms_error_history, ams_telemetry
  jobs/migrations/001_initial.sql:
    - print_jobs, print_files
  notifications/migrations/001_initial.sql:
    - webhooks
  organizations/migrations/001_initial.sql:
    - groups, oidc_config, oidc_pending_states, oidc_auth_codes, quota_usage
  reporting/migrations/001_initial.sql:
    - report_schedules
  system/migrations/001_initial.sql:
    - printer_profiles
  archives/migrations/001_initial.sql:
    - print_archives, projects
  models_library/migrations/001_initial.sql:
    - model_revisions

Key finding: The DUAL SCHEMA tables (vision_*, timelapses, consumables,
product_consumables, consumable_usage, nozzle_lifecycle) were reconciled
in a prior refactor. The SQL migration files for vision and inventory now
contain only comments pointing to the ORM as canonical source. The
entrypoint.sh no longer contains CREATE TABLE for these — it only runs
ALTER TABLE ADD COLUMN for upgrade migrations. This is correct.

No mismatches found between ORM columns and entrypoint.sh ALTER TABLE
columns. The entrypoint.sh upgrade migrations add columns that already
exist in the ORM models, which is the expected pattern for upgrading
older installations.

Run: pytest tests/test_contracts/test_schema_consistency.py -v
"""

import sys
import re
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.base import Base  # noqa: E402

# Import all ORM models so they register with Base.metadata
import core.models  # noqa: E402, F401
import modules.printers.models  # noqa: E402, F401
import modules.jobs.models  # noqa: E402, F401
import modules.inventory.models  # noqa: E402, F401
import modules.models_library.models  # noqa: E402, F401
import modules.vision.models  # noqa: E402, F401
import modules.notifications.models  # noqa: E402, F401
import modules.orders.models  # noqa: E402, F401
import modules.archives.models  # noqa: E402, F401
import modules.system.models  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_create_tables_from_sql(sql_path: Path) -> dict:
    """Parse CREATE TABLE statements from a SQL file.

    Returns {table_name: set_of_column_names}.
    Skips files that only have comments.
    """
    if not sql_path.exists():
        return {}

    text = sql_path.read_text(encoding="utf-8")
    tables = {}

    # Regex to capture CREATE TABLE IF NOT EXISTS <name> ( ... )
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        table_name = match.group(1)
        body = match.group(2)
        columns = set()
        for line in body.split(","):
            line = line.strip()
            if not line:
                continue
            # Skip constraints (PRIMARY KEY, UNIQUE, CHECK, FOREIGN KEY, CREATE INDEX)
            first_word = line.split()[0].upper() if line.split() else ""
            if first_word in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT", "CREATE"):
                continue
            # Column name is the first word
            col_name = line.split()[0].strip('"').strip("'").strip("`")
            if col_name.upper() not in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"):
                columns.add(col_name)
        tables[table_name] = columns

    return tables


def _get_orm_tables() -> dict:
    """Get all ORM-registered tables and their column names.

    Returns {table_name: set_of_column_names}.
    """
    result = {}
    for table_name, table in Base.metadata.tables.items():
        result[table_name] = {col.name for col in table.columns}
    return result


def _get_all_sql_tables() -> dict:
    """Parse all SQL migration files and return merged table definitions."""
    modules_dir = BACKEND_DIR / "modules"
    core_dir = BACKEND_DIR / "core"
    all_tables = {}

    # Core migrations
    core_sql = core_dir / "migrations" / "001_initial.sql"
    all_tables.update(_parse_create_tables_from_sql(core_sql))

    # Module migrations
    if modules_dir.exists():
        for module_dir in sorted(modules_dir.iterdir()):
            if not module_dir.is_dir():
                continue
            sql_file = module_dir / "migrations" / "001_initial.sql"
            all_tables.update(_parse_create_tables_from_sql(sql_file))

    return all_tables


def _parse_alter_table_columns(entrypoint_path: Path) -> dict:
    """Parse ALTER TABLE ADD COLUMN statements from entrypoint.sh.

    Returns {table_name: set_of_column_names_added}.
    """
    if not entrypoint_path.exists():
        return {}

    text = entrypoint_path.read_text(encoding="utf-8")
    result = {}

    # Match: ALTER TABLE <table> ADD COLUMN <col> ...
    pattern = re.compile(
        r'ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)',
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        table_name = match.group(1)
        col_name = match.group(2)
        result.setdefault(table_name, set()).add(col_name)

    return result


# ---------------------------------------------------------------------------
# Computed once
# ---------------------------------------------------------------------------

ORM_TABLES = _get_orm_tables()
SQL_TABLES = _get_all_sql_tables()
ENTRYPOINT = BACKEND_DIR.parent / "docker" / "entrypoint.sh"
ALTER_COLUMNS = _parse_alter_table_columns(ENTRYPOINT)

# Tables that exist in BOTH ORM and SQL migrations — the "dual schema" tables.
# These were historically problematic but should now be reconciled.
KNOWN_DUAL_SCHEMA = {
    "vision_detections", "vision_settings", "vision_models",
    "timelapses", "nozzle_lifecycle",
    "consumables", "product_consumables", "consumable_usage",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestORMTablesRegistered:
    """Verify that all expected ORM tables are registered in Base.metadata."""

    EXPECTED_ORM_TABLES = {
        # core
        "system_config", "audit_logs",
        # printers
        "printers", "filament_slots", "nozzle_lifecycle",
        # jobs
        "jobs", "scheduler_runs", "print_presets",
        # inventory
        "filament_library", "spools", "spool_usage", "drying_logs",
        "consumables", "product_consumables", "consumable_usage",
        # models_library
        "models",
        # orders
        "orders", "order_items", "products", "product_components",
        # notifications
        "alerts", "alert_preferences", "push_subscriptions",
        # archives
        "timelapses",
        # vision
        "vision_detections", "vision_settings", "vision_models",
        # system
        "maintenance_tasks", "maintenance_logs",
    }

    @pytest.mark.parametrize("table_name", sorted(EXPECTED_ORM_TABLES))
    def test_orm_table_registered(self, table_name):
        assert table_name in ORM_TABLES, (
            f"ORM table '{table_name}' not found in Base.metadata. "
            f"Is the model imported at startup?"
        )


class TestSQLMigrationTablesExist:
    """Verify that all expected raw-SQL tables are defined in migration files."""

    EXPECTED_SQL_TABLES = {
        # core
        "users", "api_tokens", "active_sessions", "token_blacklist",
        "password_reset_tokens", "login_attempts",
        # printers
        "printer_telemetry", "hms_error_history", "ams_telemetry",
        # jobs
        "print_jobs", "print_files",
        # notifications
        "webhooks",
        # organizations
        "groups", "oidc_config", "oidc_pending_states", "oidc_auth_codes", "quota_usage",
        # reporting
        "report_schedules",
        # system
        "printer_profiles",
        # archives
        "print_archives", "projects",
        # models_library
        "model_revisions",
    }

    @pytest.mark.parametrize("table_name", sorted(EXPECTED_SQL_TABLES))
    def test_sql_table_exists(self, table_name):
        assert table_name in SQL_TABLES, (
            f"Raw SQL table '{table_name}' not found in any migration file."
        )


class TestDualSchemaTables:
    """Verify that DUAL SCHEMA tables have their SQL migration properly deferred to ORM."""

    @pytest.mark.parametrize("table_name", sorted(KNOWN_DUAL_SCHEMA))
    def test_dual_schema_table_is_orm_canonical(self, table_name):
        """Dual schema tables should be defined in ORM, NOT in SQL migration CREATE TABLE."""
        assert table_name in ORM_TABLES, (
            f"Dual schema table '{table_name}' missing from ORM models"
        )
        # The table should NOT have a CREATE TABLE in SQL migrations
        # (it should only be in comments there)
        if table_name in SQL_TABLES:
            pytest.fail(
                f"Dual schema table '{table_name}' has CREATE TABLE in both ORM "
                f"and SQL migration. Remove the SQL migration CREATE TABLE and "
                f"leave only a comment pointing to the ORM model."
            )


class TestEntrypointUpgradeMigrations:
    """Verify that ALTER TABLE ADD COLUMN statements in entrypoint.sh match ORM columns."""

    def test_all_alter_columns_exist_in_orm(self):
        """Every column added via ALTER TABLE in entrypoint.sh must exist in the ORM model."""
        mismatches = []
        for table_name, alter_cols in ALTER_COLUMNS.items():
            if table_name not in ORM_TABLES:
                # Table is raw-SQL only (e.g. print_jobs, print_files, groups) — skip
                continue
            orm_cols = ORM_TABLES[table_name]
            missing_in_orm = alter_cols - orm_cols
            if missing_in_orm:
                mismatches.append(
                    f"  {table_name}: ALTER adds {sorted(missing_in_orm)} "
                    f"but ORM model has {sorted(orm_cols)}"
                )
        assert not mismatches, (
            "Columns in entrypoint.sh ALTER TABLE not found in ORM:\n"
            + "\n".join(mismatches)
        )

    def test_all_orm_columns_covered_by_schema(self):
        """Every ORM column should be created by EITHER create_all or ALTER TABLE migration.

        This is informational — ORM create_all handles new installs, ALTER handles upgrades.
        Both paths must produce the same final schema.
        """
        # ORM tables created by create_all always have all columns — this is inherent.
        # The ALTER TABLE migrations in entrypoint.sh are for OLDER installs that
        # already had the table created before new columns were added.
        # We verify the ALTER statements don't reference columns NOT in the ORM.
        for table_name, alter_cols in ALTER_COLUMNS.items():
            if table_name in ORM_TABLES:
                orm_cols = ORM_TABLES[table_name]
                extra = alter_cols - orm_cols
                assert not extra, (
                    f"entrypoint.sh adds column(s) {sorted(extra)} to {table_name} "
                    f"that do not exist in ORM model"
                )


class TestNoOrphanedORMTables:
    """Verify no ORM table is completely unknown — it should be documented somewhere."""

    def test_all_orm_tables_are_expected(self):
        expected = (
            TestORMTablesRegistered.EXPECTED_ORM_TABLES
        )
        unknown = set(ORM_TABLES.keys()) - expected
        assert not unknown, (
            f"ORM tables not in expected list: {sorted(unknown)}. "
            f"Add them to EXPECTED_ORM_TABLES if intentional."
        )


class TestSQLMigrationCoverage:
    """Verify that raw-SQL tables that are NOT ORM-managed have migration files."""

    def test_raw_sql_tables_not_in_orm(self):
        """Tables in SQL migrations that are not in ORM are raw-SQL only — this is fine."""
        raw_only = set(SQL_TABLES.keys()) - set(ORM_TABLES.keys())
        # These should all be in the expected raw SQL list
        expected_raw = TestSQLMigrationTablesExist.EXPECTED_SQL_TABLES
        unknown_raw = raw_only - expected_raw
        assert not unknown_raw, (
            f"SQL migration tables not in ORM AND not in expected raw list: "
            f"{sorted(unknown_raw)}"
        )
