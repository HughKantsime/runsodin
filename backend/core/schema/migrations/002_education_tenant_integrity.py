"""Create the tenant-enforced Education workflow foundation."""

from __future__ import annotations

import re
from collections.abc import Iterable

from sqlalchemy import create_engine, inspect, text


MIGRATION_ID = "python:002-education-tenant-integrity"


ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "users": (("oidc_issuer", "VARCHAR(500)"),),
    "oidc_config": (("default_group_id", "INTEGER"),),
    "print_files": (
        ("org_id", "INTEGER"),
        ("created_by", "INTEGER"),
        ("storage_bytes", "BIGINT NOT NULL DEFAULT 0"),
        ("blob_state", "VARCHAR(24) NOT NULL DEFAULT 'present'"),
        ("compatibility_facts_json", "TEXT"),
    ),
}


REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "education_cost_centers": frozenset(
        "id org_id name_key code_key display_name code description state revision "
        "created_by created_at updated_at".split()
    ),
    "education_cost_center_grants": frozenset(
        "id org_id cost_center_id user_id role state granted_by granted_at "
        "revoked_by revoked_at".split()
    ),
    "education_cost_center_printers": frozenset(
        "id org_id cost_center_id printer_id state granted_by granted_at "
        "revoked_by revoked_at".split()
    ),
    "education_commands": frozenset(
        "org_id actor_kind actor_id action command_id request_hash state result_json "
        "created_at completed_at".split()
    ),
    "education_upload_operations": frozenset(
        "operation_id org_id user_id state reserved_bytes accounted_bytes "
        "expected_bytes expected_hash staging_path final_path cleanup_path "
        "reservation_released_at purge_requested_at purged_at lease_owner "
        "lease_expires_at created_at updated_at".split()
    ),
    "education_submissions": frozenset(
        "id org_id operation_id job_id print_file_id model_id cost_center_id "
        "submitted_by approved_printer_id approved_by status lifecycle_revision "
        "compatibility_engine_version created_at updated_at".split()
    ),
    "education_audit_events": frozenset(
        "event_id org_id actor_kind actor_id action command_id request_hash "
        "resource_type resource_id cost_center_id lifecycle_revision details_json "
        "result_json created_at".split()
    ),
    "education_notification_outbox": frozenset(
        "id event_id org_id recipient_user_id state available_at lease_owner "
        "lease_expires_at attempt_count last_error delivered_at created_at".split()
    ),
    "education_rate_counters": frozenset(
        "org_id scope_kind scope_id user_id bucket_kind bucket_start attempt_count updated_at".split()
    ),
    "education_storage_accounts": frozenset(
        "org_id scope_kind scope_id user_id reserved_bytes accounted_bytes revision updated_at".split()
    ),
}


REQUIRED_INDEXES: dict[str, tuple[tuple[str, tuple[str, ...], bool], ...]] = {
    "users": (
        ("uq_users_id_group", ("id", "group_id"), True),
        ("uq_users_oidc_identity", ("oidc_issuer", "oidc_subject"), True),
    ),
    "printers": (("uq_printers_id_org", ("id", "org_id"), True),),
    "models": (("uq_models_id_org", ("id", "org_id"), True),),
    "jobs": (("uq_jobs_id_org", ("id", "charged_to_org_id"), True),),
    "print_files": (("uq_print_files_id_org", ("id", "org_id"), True),),
    "education_cost_centers": (
        ("uq_education_centers_id_org", ("id", "org_id"), True),
        ("uq_education_centers_name", ("org_id", "name_key"), True),
        ("uq_education_centers_code", ("org_id", "code_key"), True),
    ),
}


EXPECTED_FOREIGN_KEYS: dict[
    str, tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...]
] = {
    "education_cost_center_grants": (
        (("cost_center_id", "org_id"), "education_cost_centers", ("id", "org_id")),
        (("user_id", "org_id"), "users", ("id", "group_id")),
    ),
    "education_cost_center_printers": (
        (("cost_center_id", "org_id"), "education_cost_centers", ("id", "org_id")),
        (("printer_id", "org_id"), "printers", ("id", "org_id")),
    ),
    "education_submissions": (
        (("operation_id", "org_id"), "education_upload_operations", ("operation_id", "org_id")),
        (("cost_center_id", "org_id"), "education_cost_centers", ("id", "org_id")),
        (("submitted_by", "org_id"), "users", ("id", "group_id")),
        (("job_id", "org_id"), "jobs", ("id", "charged_to_org_id")),
        (("print_file_id", "org_id"), "print_files", ("id", "org_id")),
        (("model_id", "org_id"), "models", ("id", "org_id")),
    ),
    "education_notification_outbox": (
        (("event_id", "org_id"), "education_audit_events", ("event_id", "org_id")),
        (("recipient_user_id", "org_id"), "users", ("id", "group_id")),
    ),
    "education_rate_counters": (
        (("user_id", "org_id"), "users", ("id", "group_id")),
    ),
    "education_storage_accounts": (
        (("user_id", "org_id"), "users", ("id", "group_id")),
    ),
}


def _timestamp_type(connection) -> str:
    return (
        "TIMESTAMP WITH TIME ZONE"
        if connection.dialect.name == "postgresql"
        else "DATETIME"
    )


def _identity_type(connection) -> str:
    return "SERIAL PRIMARY KEY" if connection.dialect.name == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"


def _columns(connection, table_name: str) -> set[str]:
    inspector = inspect(connection)
    if table_name not in inspector.get_table_names():
        raise RuntimeError(f"Education migration requires missing table: {table_name}")
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_columns(connection) -> None:
    from core.schema.migrator import validate_column_shape

    for table_name, definitions in ADDED_COLUMNS.items():
        existing = _columns(connection, table_name)
        for column_name, definition in definitions:
            if column_name not in existing:
                connection.execute(
                    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- immutable migration identifiers
                    text(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
                    )
                )
                existing.add(column_name)
            validate_column_shape(connection, table_name, column_name, definition)


def _validate_legacy_ownership(connection) -> None:
    ambiguous = int(
        connection.execute(
            text(
                "SELECT COUNT(*) FROM print_files pf "
                "LEFT JOIN jobs j ON j.id = pf.job_id "
                "LEFT JOIN models m ON m.id = pf.model_id "
                "WHERE j.charged_to_org_id IS NOT NULL AND m.org_id IS NOT NULL "
                "AND j.charged_to_org_id <> m.org_id"
            )
        ).scalar_one()
    )
    if ambiguous:
        raise RuntimeError(
            f"Education migration found {ambiguous} print_files with ambiguous tenant ownership"
        )
    connection.execute(
        text(
            "UPDATE print_files SET org_id = COALESCE("
            "(SELECT j.charged_to_org_id FROM jobs j WHERE j.id = print_files.job_id), "
            "(SELECT m.org_id FROM models m WHERE m.id = print_files.model_id)) "
            "WHERE org_id IS NULL"
        )
    )
    connection.execute(
        text(
            "UPDATE print_files SET created_by = ("
            "SELECT j.submitted_by FROM jobs j WHERE j.id = print_files.job_id) "
            "WHERE created_by IS NULL"
        )
    )


def _create_parent_indexes(connection) -> None:
    parent_columns = {
        "users": {"id", "group_id", "oidc_issuer", "oidc_subject"},
        "printers": {"id", "org_id"},
        "models": {"id", "org_id"},
        "jobs": {"id", "charged_to_org_id"},
        "print_files": {"id", "org_id"},
    }
    for table_name, required in parent_columns.items():
        missing = sorted(required - _columns(connection, table_name))
        if missing:
            raise RuntimeError(
                f"Schema validation missing columns on {table_name}: {', '.join(missing)}"
            )
    statements = (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_id_group ON users(id, group_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_printers_id_org ON printers(id, org_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_models_id_org ON models(id, org_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_id_org ON jobs(id, charged_to_org_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_print_files_id_org ON print_files(id, org_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_oidc_identity "
        "ON users(oidc_issuer, oidc_subject) "
        "WHERE oidc_issuer IS NOT NULL AND oidc_subject IS NOT NULL",
    )
    for statement in statements:
        connection.execute(text(statement))


def _table_statements(connection) -> Iterable[str]:
    pk = _identity_type(connection)
    timestamp = _timestamp_type(connection)
    now = "CURRENT_TIMESTAMP"
    return (
        f"""CREATE TABLE IF NOT EXISTS education_cost_centers (
            id {pk},
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
            name_key VARCHAR(200) NOT NULL,
            code_key VARCHAR(100) NOT NULL,
            display_name VARCHAR(200) NOT NULL,
            code VARCHAR(100) NOT NULL,
            description TEXT,
            state VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (state IN ('active','archived')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at {timestamp} NOT NULL DEFAULT {now},
            updated_at {timestamp} NOT NULL DEFAULT {now},
            UNIQUE (id, org_id),
            UNIQUE (org_id, name_key),
            UNIQUE (org_id, code_key)
        )""",
        f"""CREATE TABLE IF NOT EXISTS education_cost_center_grants (
            id {pk},
            org_id INTEGER NOT NULL,
            cost_center_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role VARCHAR(16) NOT NULL CHECK (role IN ('student','manager')),
            state VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (state IN ('active','revoked')),
            granted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            granted_at {timestamp} NOT NULL DEFAULT {now},
            revoked_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            revoked_at {timestamp},
            UNIQUE (cost_center_id, user_id, role),
            FOREIGN KEY (cost_center_id, org_id) REFERENCES education_cost_centers(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (user_id, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        )""",
        f"""CREATE TABLE IF NOT EXISTS education_cost_center_printers (
            id {pk},
            org_id INTEGER NOT NULL,
            cost_center_id INTEGER NOT NULL,
            printer_id INTEGER NOT NULL,
            state VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (state IN ('active','revoked')),
            granted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            granted_at {timestamp} NOT NULL DEFAULT {now},
            revoked_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            revoked_at {timestamp},
            UNIQUE (cost_center_id, printer_id),
            FOREIGN KEY (cost_center_id, org_id) REFERENCES education_cost_centers(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (printer_id, org_id) REFERENCES printers(id, org_id) ON DELETE RESTRICT
        )""",
        f"""CREATE TABLE IF NOT EXISTS education_commands (
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
            actor_kind VARCHAR(32) NOT NULL,
            actor_id VARCHAR(100) NOT NULL,
            action VARCHAR(64) NOT NULL,
            command_id VARCHAR(64) NOT NULL,
            request_hash VARCHAR(128) NOT NULL,
            state VARCHAR(16) NOT NULL CHECK (state IN ('pending','complete')),
            result_json TEXT,
            created_at {timestamp} NOT NULL DEFAULT {now},
            completed_at {timestamp},
            PRIMARY KEY (org_id, actor_kind, actor_id, action, command_id),
            CHECK ((state='pending' AND result_json IS NULL AND completed_at IS NULL) OR
                   (state='complete' AND result_json IS NOT NULL AND completed_at IS NOT NULL))
        )""",
        f"""CREATE TABLE IF NOT EXISTS education_upload_operations (
            operation_id VARCHAR(64) PRIMARY KEY,
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
            user_id INTEGER NOT NULL,
            state VARCHAR(32) NOT NULL CHECK (state IN ('reserved','streaming','staged','renamed','committed','aborted','needs_reconciliation','purge_pending','purged')),
            reserved_bytes BIGINT NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
            accounted_bytes BIGINT NOT NULL DEFAULT 0 CHECK (accounted_bytes >= 0),
            expected_bytes BIGINT,
            expected_hash VARCHAR(128),
            staging_path TEXT,
            final_path TEXT,
            cleanup_path TEXT,
            reservation_released_at {timestamp},
            purge_requested_at {timestamp},
            purged_at {timestamp},
            lease_owner VARCHAR(100),
            lease_expires_at {timestamp},
            created_at {timestamp} NOT NULL DEFAULT {now},
            updated_at {timestamp} NOT NULL DEFAULT {now},
            UNIQUE (operation_id, org_id),
            FOREIGN KEY (user_id, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        )""",
        f"""CREATE TABLE IF NOT EXISTS education_submissions (
            id {pk},
            org_id INTEGER NOT NULL,
            operation_id VARCHAR(64) NOT NULL UNIQUE,
            job_id INTEGER NOT NULL UNIQUE,
            print_file_id INTEGER NOT NULL UNIQUE,
            model_id INTEGER NOT NULL,
            cost_center_id INTEGER NOT NULL,
            submitted_by INTEGER NOT NULL,
            approved_printer_id INTEGER,
            approved_by INTEGER,
            status VARCHAR(24) NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted','pending','scheduled','printing','completed','failed','rejected','cancelled')),
            lifecycle_revision INTEGER NOT NULL DEFAULT 1 CHECK (lifecycle_revision >= 1),
            compatibility_engine_version VARCHAR(40),
            created_at {timestamp} NOT NULL DEFAULT {now},
            updated_at {timestamp} NOT NULL DEFAULT {now},
            FOREIGN KEY (operation_id, org_id) REFERENCES education_upload_operations(operation_id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (cost_center_id, org_id) REFERENCES education_cost_centers(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (submitted_by, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT,
            FOREIGN KEY (job_id, org_id) REFERENCES jobs(id, charged_to_org_id) ON DELETE RESTRICT,
            FOREIGN KEY (print_file_id, org_id) REFERENCES print_files(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (model_id, org_id) REFERENCES models(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (approved_printer_id, org_id) REFERENCES printers(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (approved_by, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        )""",
        f"""CREATE TABLE IF NOT EXISTS education_audit_events (
            event_id VARCHAR(64) PRIMARY KEY,
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
            actor_kind VARCHAR(32) NOT NULL,
            actor_id VARCHAR(100) NOT NULL,
            action VARCHAR(64) NOT NULL,
            command_id VARCHAR(64) NOT NULL,
            request_hash VARCHAR(128) NOT NULL,
            resource_type VARCHAR(64) NOT NULL,
            resource_id VARCHAR(100) NOT NULL,
            cost_center_id INTEGER,
            lifecycle_revision INTEGER,
            details_json TEXT NOT NULL DEFAULT '{{}}',
            result_json TEXT NOT NULL DEFAULT '{{}}',
            created_at {timestamp} NOT NULL DEFAULT {now},
            UNIQUE (event_id, org_id),
            UNIQUE (org_id, actor_kind, actor_id, action, command_id),
            FOREIGN KEY (cost_center_id, org_id) REFERENCES education_cost_centers(id, org_id) ON DELETE RESTRICT
        )""",
        f"""CREATE TABLE IF NOT EXISTS education_notification_outbox (
            id {pk},
            event_id VARCHAR(64) NOT NULL,
            org_id INTEGER NOT NULL,
            recipient_user_id INTEGER NOT NULL,
            state VARCHAR(24) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','leased','delivered','suppressed','dead_letter')),
            available_at {timestamp} NOT NULL DEFAULT {now},
            lease_owner VARCHAR(100),
            lease_expires_at {timestamp},
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            last_error VARCHAR(500),
            delivered_at {timestamp},
            created_at {timestamp} NOT NULL DEFAULT {now},
            UNIQUE (event_id, recipient_user_id),
            FOREIGN KEY (event_id, org_id) REFERENCES education_audit_events(event_id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (recipient_user_id, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        )""",
        f"""CREATE TABLE IF NOT EXISTS education_rate_counters (
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            scope_kind VARCHAR(16) NOT NULL CHECK (scope_kind IN ('user','tenant')),
            scope_id INTEGER NOT NULL,
            user_id INTEGER,
            bucket_kind VARCHAR(16) NOT NULL CHECK (bucket_kind IN ('hour','day')),
            bucket_start {timestamp} NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            updated_at {timestamp} NOT NULL DEFAULT {now},
            PRIMARY KEY (org_id, scope_kind, scope_id, bucket_kind, bucket_start),
            CHECK ((scope_kind='tenant' AND scope_id=org_id AND user_id IS NULL) OR
                   (scope_kind='user' AND user_id=scope_id)),
            FOREIGN KEY (user_id, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        )""",
        f"""CREATE TABLE IF NOT EXISTS education_storage_accounts (
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
            scope_kind VARCHAR(16) NOT NULL CHECK (scope_kind IN ('user','tenant')),
            scope_id INTEGER NOT NULL,
            user_id INTEGER,
            reserved_bytes BIGINT NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
            accounted_bytes BIGINT NOT NULL DEFAULT 0 CHECK (accounted_bytes >= 0),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            updated_at {timestamp} NOT NULL DEFAULT {now},
            PRIMARY KEY (org_id, scope_kind, scope_id),
            CHECK ((scope_kind='tenant' AND scope_id=org_id AND user_id IS NULL) OR
                   (scope_kind='user' AND user_id=scope_id)),
            FOREIGN KEY (user_id, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        )""",
    )


def _create_indexes(connection) -> None:
    statements = (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_education_centers_id_org ON education_cost_centers(id, org_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_education_centers_name ON education_cost_centers(org_id, name_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_education_centers_code ON education_cost_centers(org_id, code_key)",
        "CREATE INDEX IF NOT EXISTS ix_education_grants_active ON education_cost_center_grants(org_id, user_id, state, role)",
        "CREATE INDEX IF NOT EXISTS ix_education_printers_active ON education_cost_center_printers(org_id, printer_id, state)",
        "CREATE INDEX IF NOT EXISTS ix_education_submissions_queue ON education_submissions(org_id, cost_center_id, status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_education_upload_state ON education_upload_operations(state, lease_expires_at)",
        "CREATE INDEX IF NOT EXISTS ix_education_outbox_claim ON education_notification_outbox(state, available_at, lease_expires_at)",
    )
    for statement in statements:
        connection.execute(text(statement))


def _create_audit_immutability(connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION odin_reject_education_audit_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'education audit events are immutable'; END; $$"
            )
        )
        for action in ("UPDATE", "DELETE"):
            trigger = f"trg_education_audit_no_{action.lower()}"
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- trigger is derived only from the fixed UPDATE/DELETE tuple above
            connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON education_audit_events"))
            connection.execute(
                # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- trigger and action are derived only from the fixed UPDATE/DELETE tuple above
                text(
                    f"CREATE TRIGGER {trigger} BEFORE {action} ON education_audit_events "
                    "FOR EACH ROW EXECUTE FUNCTION odin_reject_education_audit_mutation()"
                )
            )
        return

    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS trg_education_audit_no_update "
        "BEFORE UPDATE ON education_audit_events BEGIN "
        "SELECT RAISE(ABORT, 'education audit events are immutable'); END"
    )
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS trg_education_audit_no_delete "
        "BEFORE DELETE ON education_audit_events BEGIN "
        "SELECT RAISE(ABORT, 'education audit events are immutable'); END"
    )


def apply(connection) -> None:
    _add_columns(connection)
    _validate_legacy_ownership(connection)
    _create_parent_indexes(connection)
    for statement in _table_statements(connection):
        connection.execute(text(statement))
    _create_indexes(connection)
    _create_audit_immutability(connection)


def _index_map(connection, table_name: str) -> dict[str, tuple[tuple[str, ...], bool]]:
    return {
        str(item["name"]): (
            tuple(item.get("column_names") or ()),
            bool(item.get("unique")),
        )
        for item in inspect(connection).get_indexes(table_name)
        if item.get("name")
    }


def _type_signature(value) -> str:
    rendered = str(value).upper()
    if "TIMESTAMP" in rendered or "DATETIME" in rendered:
        return "datetime"
    if rendered.startswith("VARCHAR"):
        match = re.search(r"\((\d+)\)", rendered)
        return f"varchar:{match.group(1) if match else '*'}"
    if "BIGINT" in rendered:
        return "bigint"
    if "INT" in rendered or "SERIAL" in rendered:
        return "integer"
    if "TEXT" in rendered:
        return "text"
    return rendered.lower()


def _default_signature(value) -> str | None:
    if value is None:
        return None
    rendered = re.sub(r"::[a-zA-Z_ ]+(?:\[\])?", "", str(value))
    rendered = rendered.strip().strip("()").replace('"', "'").lower()
    return re.sub(r"\s+", "", rendered)


def _check_signature(sqltext: str, column_names: set[str]) -> tuple:
    normalized = str(sqltext).lower()
    referenced = tuple(sorted(name for name in column_names if re.search(rf"\b{re.escape(name)}\b", normalized)))
    literals = tuple(sorted(set(re.findall(r"'([^']*)'", normalized))))
    column_pattern = "|".join(re.escape(name) for name in sorted(column_names, key=len, reverse=True))
    cast = r"(?:\s*::\s*[a-z_]+(?:\s+[a-z_]+)?(?:\[\])?)?"
    relations: set[tuple[str, str, str]] = set()
    memberships: set[tuple[str, str]] = set()
    for name in column_names:
        relation_pattern = (
            rf"\b{re.escape(name)}\b{cast}\s*(>=|<=|<>|=|>|<)\s*"
            rf"('(?:''|[^'])*'|-?\d+(?:\.\d+)?|\b(?:{column_pattern})\b)"
        )
        for operator, rhs in re.findall(relation_pattern, normalized):
            relations.add((name, operator, rhs))
        if re.search(
            rf"\b{re.escape(name)}\b{cast}\s+not\s+in\s*\(", normalized
        ) or re.search(
            rf"\b{re.escape(name)}\b{cast}\s*(?:<>|!=)\s*all\s*\(", normalized
        ):
            memberships.add((name, "not_in"))
        if re.search(
            rf"\b{re.escape(name)}\b{cast}\s+in\s*\(", normalized
        ) or re.search(
            rf"\b{re.escape(name)}\b{cast}\s*=\s*any\s*\(", normalized
        ):
            memberships.add((name, "in"))
    return referenced, literals, tuple(sorted(relations)), tuple(sorted(memberships))


def _declared_check_expressions(ddl: str) -> list[str]:
    """Extract complete CHECK bodies without SQLite inspector truncation."""
    expressions: list[str] = []
    offset = 0
    while match := re.search(r"\bCHECK\s*\(", ddl[offset:], re.IGNORECASE):
        opening = offset + match.end() - 1
        depth = 0
        quoted = False
        index = opening
        while index < len(ddl):
            character = ddl[index]
            if character == "'":
                if quoted and index + 1 < len(ddl) and ddl[index + 1] == "'":
                    index += 2
                    continue
                quoted = not quoted
            elif not quoted:
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0:
                        expressions.append(ddl[opening + 1:index])
                        offset = index + 1
                        break
            index += 1
        else:
            raise RuntimeError("Unbalanced CHECK constraint in Education schema")
    return expressions


def _schema_manifest(connection, table_names: set[str]) -> dict[str, dict]:
    inspector = inspect(connection)
    manifest: dict[str, dict] = {}
    for table_name in sorted(table_names):
        columns = inspector.get_columns(table_name)
        column_names = {str(column["name"]) for column in columns}
        pk_columns = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ())
        sqlite_delete_actions: dict[tuple[str, ...], str] = {}
        sqlite_uniques: set[tuple[str, ...]] | None = None
        if connection.dialect.name == "sqlite":
            sqlite_uniques = set()
            ddl = connection.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
                {"name": table_name},
            ).scalar_one()
            for line in str(ddl).splitlines():
                inline = re.search(
                    r"^\s*(\w+)\s+.*REFERENCES\s+\w+\([^)]*\)\s+ON DELETE\s+(\w+)",
                    line,
                    re.IGNORECASE,
                )
                composite = re.search(
                    r"FOREIGN KEY\s*\(([^)]*)\).*ON DELETE\s+(\w+)",
                    line,
                    re.IGNORECASE,
                )
                if inline:
                    sqlite_delete_actions[(inline.group(1),)] = inline.group(2).upper()
                elif composite:
                    sqlite_delete_actions[
                        tuple(item.strip() for item in composite.group(1).split(","))
                    ] = composite.group(2).upper()
                column_unique = re.search(
                    r"^\s*(\w+)\s+.*\bUNIQUE\b", line, re.IGNORECASE
                )
                table_unique = re.search(
                    r"^\s*UNIQUE\s*\(([^)]*)\)", line, re.IGNORECASE
                )
                if table_unique:
                    sqlite_uniques.add(
                        tuple(item.strip() for item in table_unique.group(1).split(","))
                    )
                elif column_unique and "PRIMARY KEY" not in line.upper():
                    sqlite_uniques.add((column_unique.group(1),))
        check_sqltexts = (
            _declared_check_expressions(str(ddl))
            if connection.dialect.name == "sqlite"
            else [
                item.get("sqltext") or ""
                for item in inspector.get_check_constraints(table_name)
            ]
        )
        checks = [_check_signature(sqltext, column_names) for sqltext in check_sqltexts]
        manifest[table_name] = {
            "columns": tuple(
                (
                    str(column["name"]),
                    _type_signature(column["type"]),
                    None if column["name"] in pk_columns else bool(column.get("nullable", True)),
                    None if column["name"] in pk_columns else _default_signature(column.get("default")),
                )
                for column in columns
            ),
            "pk": pk_columns,
            "unique": tuple(sorted(
                sqlite_uniques
                if sqlite_uniques is not None
                else {
                    tuple(item.get("column_names") or ())
                    for item in inspector.get_unique_constraints(table_name)
                }
            )),
            "foreign_keys": tuple(sorted(
                (
                    tuple(item.get("constrained_columns") or ()),
                    str(item.get("referred_table")),
                    tuple(item.get("referred_columns") or ()),
                    sqlite_delete_actions.get(
                        tuple(item.get("constrained_columns") or ()),
                        str((item.get("options") or {}).get("ondelete") or "NO ACTION").upper(),
                    ),
                )
                for item in inspector.get_foreign_keys(table_name)
            )),
            "checks": (
                len(checks),
                tuple(sorted({column for check in checks for column in check[0]})),
                tuple(sorted({literal for check in checks for literal in check[1]})),
                tuple(sorted({relation for check in checks for relation in check[2]})),
                tuple(sorted({membership for check in checks for membership in check[3]})),
            ),
            "indexes": tuple(sorted(
                (
                    str(item.get("name")),
                    tuple(item.get("column_names") or ()),
                    bool(item.get("unique")),
                )
                for item in inspector.get_indexes(table_name)
                if item.get("name") and not item.get("duplicates_constraint")
            )),
        }
    return manifest


def _expected_manifest() -> dict[str, dict]:
    """Build the exact declared Education schema on isolated in-memory SQLite."""
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as reference:
            statements = tuple(_table_statements(reference))
            declared_delete_actions: dict[tuple[str, tuple[str, ...]], str] = {}
            declared_uniques: dict[str, set[tuple[str, ...]]] = {}
            for statement in statements:
                table_match = re.search(
                    r"CREATE TABLE IF NOT EXISTS\s+(\w+)", statement, re.IGNORECASE
                )
                if not table_match:
                    continue
                table_name = table_match.group(1)
                declared_uniques.setdefault(table_name, set())
                for line in statement.splitlines():
                    inline = re.search(
                        r"^\s*(\w+)\s+.*REFERENCES\s+\w+\([^)]*\)\s+ON DELETE\s+(\w+)",
                        line,
                        re.IGNORECASE,
                    )
                    composite = re.search(
                        r"FOREIGN KEY\s*\(([^)]*)\).*ON DELETE\s+(\w+)",
                        line,
                        re.IGNORECASE,
                    )
                    if inline:
                        declared_delete_actions[(table_name, (inline.group(1),))] = inline.group(2).upper()
                    elif composite:
                        columns = tuple(
                            item.strip() for item in composite.group(1).split(",")
                        )
                        declared_delete_actions[(table_name, columns)] = composite.group(2).upper()
                    column_unique = re.search(
                        r"^\s*(\w+)\s+.*\bUNIQUE\b", line, re.IGNORECASE
                    )
                    table_unique = re.search(
                        r"^\s*UNIQUE\s*\(([^)]*)\)", line, re.IGNORECASE
                    )
                    if table_unique:
                        declared_uniques[table_name].add(
                            tuple(item.strip() for item in table_unique.group(1).split(","))
                        )
                    elif column_unique and "PRIMARY KEY" not in line.upper():
                        declared_uniques[table_name].add((column_unique.group(1),))
                reference.execute(text(statement))
            _create_indexes(reference)
            manifest = _schema_manifest(reference, set(REQUIRED_COLUMNS))
            for table_name, shape in manifest.items():
                shape["unique"] = tuple(sorted(declared_uniques[table_name]))
                shape["foreign_keys"] = tuple(
                    sorted(
                        (
                            columns,
                            referred_table,
                            referred_columns,
                            declared_delete_actions.get(
                                (table_name, columns), ondelete
                            ),
                        )
                        for columns, referred_table, referred_columns, ondelete in shape["foreign_keys"]
                    )
                )
            return manifest
    finally:
        engine.dispose()


def _validate_exact_manifest(connection) -> None:
    expected = _expected_manifest()
    actual = _schema_manifest(connection, set(REQUIRED_COLUMNS))
    for table_name, expected_shape in expected.items():
        actual_shape = actual[table_name]
        for key in ("columns", "pk", "unique", "foreign_keys", "checks", "indexes"):
            actual_value = actual_shape[key]
            if table_name == "education_submissions" and key == "indexes":
                # Migration 003 owns and independently validates this parent key.
                # Migration 002 is revalidated before 003 on later bootstraps, so
                # its original exact manifest must tolerate that one later index.
                actual_value = tuple(
                    item
                    for item in actual_value
                    if item[0] != "uq_education_submissions_id_org"
                )
            if actual_value != expected_shape[key]:
                raise RuntimeError(
                    f"Education migration {key} manifest mismatch: {table_name}; "
                    f"expected={expected_shape[key]!r} actual={actual_value!r}"
                )


def validate(connection) -> None:
    from core.schema.migrator import validate_column_shape

    for table_name, definitions in ADDED_COLUMNS.items():
        columns = _columns(connection, table_name)
        for column_name, definition in definitions:
            if column_name not in columns:
                raise RuntimeError(
                    f"Education migration postcondition failed: {table_name}.{column_name}"
                )
            validate_column_shape(connection, table_name, column_name, definition)

    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names())
    for table_name, required in REQUIRED_COLUMNS.items():
        if table_name not in actual_tables:
            raise RuntimeError(
                f"Education migration postcondition failed: missing {table_name}"
            )
        missing = sorted(required - _columns(connection, table_name))
        if missing:
            raise RuntimeError(
                f"Education migration postcondition failed: {table_name} missing {', '.join(missing)}"
            )

    _validate_exact_manifest(connection)

    for table_name, expectations in REQUIRED_INDEXES.items():
        indexes = _index_map(connection, table_name)
        for index_name, columns, unique in expectations:
            if indexes.get(index_name) != (columns, unique):
                raise RuntimeError(
                    f"Education migration index shape mismatch: {index_name}"
                )

    for table_name, expectations in EXPECTED_FOREIGN_KEYS.items():
        actual = {
            (
                tuple(item.get("constrained_columns") or ()),
                str(item.get("referred_table")),
                tuple(item.get("referred_columns") or ()),
            )
            for item in inspector.get_foreign_keys(table_name)
        }
        for expectation in expectations:
            if expectation not in actual:
                raise RuntimeError(
                    f"Education migration foreign key mismatch: {table_name} {expectation}"
                )

    if connection.dialect.name == "sqlite":
        triggers = {
            str(row[0])
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='education_audit_events'"
                )
            )
        }
    else:
        triggers = {
            str(row[0])
            for row in connection.execute(
                text(
                    "SELECT trigger_name FROM information_schema.triggers "
                    "WHERE event_object_table='education_audit_events'"
                )
            )
        }
    required_triggers = {
        "trg_education_audit_no_update",
        "trg_education_audit_no_delete",
    }
    if not required_triggers.issubset(triggers):
        raise RuntimeError("Education audit immutability triggers are missing")
