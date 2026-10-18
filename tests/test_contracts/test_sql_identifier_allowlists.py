"""SQL helpers that interpolate table/column names refuse names outside their allowlist.

Each helper below builds statement text from identifiers its callers pass. The
helper itself must reject any identifier outside its explicit closed set before a
statement reaches the connection, so a future caller cannot widen it by accident.
"""
import pytest

from core import dispatch_claims, economic_identity
from modules.archives import timelapse_frame_capture, valuation_content_visibility
from modules.organizations import education_ams_payload_authority, education_ams_teacher_authority
from modules.printers import dispatch_admission
from modules.printers.monitors import ams_inventory

HOSTILE = ("jobs; DROP TABLE users", "users--", "api_tokens", "")


class _Result:
    rowcount = 1

    def first(self):
        return (1,)

    def fetchall(self):
        return []

    def mappings(self):
        return self

    def one_or_none(self):
        return {"id": 1}

    def all(self):
        return []


class _Dialect:
    def __init__(self, name):
        self.name = name


class _Recorder:
    """Connection/session double that records every statement it is given."""

    def __init__(self, dialect="sqlite"):
        self.dialect = _Dialect(dialect)
        self._postgres = dialect == "postgresql"
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return _Result()


def _sql(recorder):
    return [statement for statement, _ in recorder.statements]


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_dispatch_terminal_lock_accepts_only_allowlisted_targets(dialect):
    for table, column in sorted(dispatch_claims._TERMINAL_LOCK_TARGETS):
        conn = _Recorder(dialect)
        dispatch_claims._lock_terminal_resource(conn, table, 7, column)
        assert len(conn.statements) == 1 and table in _sql(conn)[0] and conn.statements[0][1] == {"id": 7}
    for name in HOSTILE:
        for table, column in ((name, "status"), ("jobs", name)):
            conn = _Recorder(dialect)
            with pytest.raises(ValueError):
                dispatch_claims._lock_terminal_resource(conn, table, 7, column)
            assert conn.statements == []


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_dispatch_admission_lock_accepts_only_allowlisted_targets(dialect):
    for table, column in sorted(dispatch_admission._ADMISSION_LOCK_TARGETS):
        conn = _Recorder(dialect)
        dispatch_admission._lock(conn, table, 7, column)
        assert len(conn.statements) == 1 and table in _sql(conn)[0]
    for name in HOSTILE:
        for table, column in ((name, "status"), ("jobs", name), ("jobs", "is_active")):
            conn = _Recorder(dialect)
            with pytest.raises(ValueError):
                dispatch_admission._lock(conn, table, 7, column)
            assert conn.statements == []


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_ams_inventory_rows_accepts_only_allowlisted_selections(dialect):
    for table, selections in sorted(ams_inventory._ROW_SELECTIONS.items()):
        for columns in sorted(selections):
            conn = _Recorder(dialect)
            ams_inventory._rows(conn, table, {3, 1}, columns, lock=True)
            assert len(conn.statements) == 1 and conn.statements[0][1] == (1, 3)
    for name in HOSTILE:
        for table, columns in ((name, "id"), ("spools", name), ("filament_slots", "id,org_id")):
            conn = _Recorder(dialect)
            with pytest.raises(ValueError):
                ams_inventory._rows(conn, table, {1}, columns)
            with pytest.raises(ValueError):
                ams_inventory._rows(conn, table, set(), columns)
            assert conn.statements == []


@pytest.mark.parametrize("postgres", [False, True])
def test_teacher_ams_lock_accepts_only_allowlisted_rows(postgres):
    params = {"user_id": 1}
    for table, where, column in sorted(education_ams_teacher_authority._TEACHER_LOCKS):
        db = _Recorder()
        education_ams_teacher_authority._lock_teacher_row(db, postgres, params, table, where, column)
        assert len(db.statements) == 1 and where in _sql(db)[0] and db.statements[0][1] is params
    table, where, column = sorted(education_ams_teacher_authority._TEACHER_LOCKS)[0]
    for name in HOSTILE:
        for candidate in ((name, where, column), (table, name, column), (table, where, name), (table, where + " OR 1=1", column)):
            db = _Recorder()
            with pytest.raises(ValueError):
                education_ams_teacher_authority._lock_teacher_row(db, postgres, params, *candidate)
            assert db.statements == []


@pytest.mark.parametrize("postgres", [False, True])
def test_payload_row_lock_accepts_only_allowlisted_rows(postgres):
    params = {"model": 1, "file": 2, "org": 3}
    for table, column, key in education_ams_payload_authority._PAYLOAD_ROW_LOCKS:
        db = _Recorder()
        education_ams_payload_authority._lock_payload_row(db, postgres, params, table, column, key)
        assert len(db.statements) == 1 and ":" + key in _sql(db)[0]
    for name in HOSTILE:
        for candidate in ((name, "name", "model"), ("models", name, "model"), ("models", "name", name), ("models", "name", "file")):
            db = _Recorder()
            with pytest.raises(ValueError):
                education_ams_payload_authority._lock_payload_row(db, postgres, params, *candidate)
            assert db.statements == []


def test_capture_graph_lock_accepts_only_allowlisted_tables():
    for table in sorted(timelapse_frame_capture._CAMERA_GRAPH_LOCK_TABLES):
        db = _Recorder("postgresql")
        timelapse_frame_capture._lock_graph_rows(db, table, [2, 1, 2])
        assert [params for _, params in db.statements] == [{"id": 1}, {"id": 2}]
    for name in HOSTILE + ("jobs",):
        db = _Recorder("postgresql")
        with pytest.raises(ValueError):
            timelapse_frame_capture._lock_graph_rows(db, name, [1])
        with pytest.raises(ValueError):
            timelapse_frame_capture._lock_graph_rows(db, name, [])
        assert db.statements == []


def test_economic_identity_row_helpers_accept_only_allowlisted_tables():
    for table in sorted(economic_identity._TABLE_NAMES):
        db = _Recorder()
        economic_identity._row(db, table, 5)
        economic_identity._lock(db, table, 5)
        assert len(db.statements) == 3 and all(table in sql for sql in _sql(db))
    for name in HOSTILE + ("users",):
        db = _Recorder()
        with pytest.raises(ValueError):
            economic_identity._row(db, name, 5)
        with pytest.raises(ValueError):
            economic_identity._lock(db, name, 5)
        assert db.statements == []


def test_bounded_alias_query_accepts_only_allowlisted_statements():
    module = valuation_content_visibility
    expansions = {module._MODEL_ALIASES: ("models", "files"), module._FILE_ALIASES: ("files", "models", "paths"),
                  module._SUBMISSION_ALIASES: ("files", "models", "operations"),
                  module._OPERATION_ALIASES: ("operations", "paths")}
    assert set(expansions) == module._BOUNDED_STATEMENTS
    for statement, expanding in expansions.items():
        db = _Recorder()
        params = {key: [1] for key in expanding} | {"job": 1}
        assert module._bounded(db, statement, params, expanding) == []
        assert len(db.statements) == 1
    for statement in HOSTILE + (module._MODEL_ALIASES + " OR 1=1", "SELECT * FROM users"):
        db = _Recorder()
        with pytest.raises(ValueError):
            module._bounded(db, statement, {}, ())
        assert db.statements == []
