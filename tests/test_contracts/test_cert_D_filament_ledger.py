"""cert-D round 5: one filament consumption ledger (core.filament_ledger).

Every deducer -- operator /complete, the Moonraker monitor, archive accounting
(Bambu/MQTT and every archived attempt) and manual spool use/weigh -- goes
through the ledger; automatic deductions are once per physical print / job
attempt, claimed atomically and only when something is actually deducted.
"""
import ast
import json
import re
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_cert_D_jobs_lifecycle_idempotency import lifecycle_client, _spool, owned_completion_background  # noqa: F401
from tests.test_contracts.test_archive_persistence_accounting_behavior import archive_case  # noqa: F401
from tests.test_contracts import job_census


@pytest.fixture(autouse=True)
def _owned_archive_completion_threads(request):
    if 'archive_case' in request.fixturenames:
        request.getfixturevalue('owned_completion_background')


def _keys(db, job_id=111):
    db.expire_all()
    return dict(db.execute(text("SELECT key, value FROM system_config WHERE key LIKE :p"),
                           {"p": f"quota_attempt:%{job_id}%"}).all())


def _usage(db, job_id=111):
    db.expire_all()
    return db.execute(text("SELECT spool_id, print_job_id, weight_used_g, notes FROM spool_usage WHERE job_id=:j ORDER BY id"),
                      {"j": job_id}).all()


def _moonraker(db, print_job_id=None):
    from modules.printers.monitors.moonraker_monitor import MoonrakerMonitor
    db.execute(text("UPDATE spools SET location_slot=NULL WHERE id=111")); db.commit()
    fake = types.SimpleNamespace(printer_id=9, _current_job_db_id=print_job_id)
    engine = db.get_bind()

    def deduct(grams=10.0):
        with engine.begin() as conn:
            return MoonrakerMonitor._auto_deduct_filament(fake, conn, 111, observed_grams=grams)
    return deduct


def _print_job(db, print_job_id, job_id=111):
    db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) "
                    "VALUES(:id,9,:j,'Synthetic','completed',:at)"),
               {"id": print_job_id, "j": job_id, "at": datetime.now(timezone.utc).replace(tzinfo=None)})
    db.commit()


# ---------------- manual /complete ----------------

def test_operator_completion_claims_a_job_level_attempt_marker_as_json(lifecycle_client):
    client, db = lifecycle_client
    assert client.post("/jobs/111/complete").status_code == 200
    assert _spool(db) == (90.0, 1)
    marker = json.loads(_keys(db)["quota_attempt:filament:job:111:0"])
    assert marker == {"source": "operator_complete", "print_job_id": None}
    assert _usage(db)[0].notes.startswith("auto:operator_complete")


# ---------------- monitor per protocol ----------------

def test_moonraker_physical_print_deducts_once_and_operator_correction_does_not_add(lifecycle_client):
    client, db = lifecycle_client
    _print_job(db, 5001)
    deduct = _moonraker(db, print_job_id=5001)
    assert deduct().deducted
    assert deduct().state == "already_deducted"                     # duplicate end event
    assert _spool(db) == (90.0, 1)
    db.execute(text("UPDATE jobs SET status='failed' WHERE id=111")); db.commit()
    assert client.post("/jobs/111/complete").status_code == 200      # same attempt: nothing more
    assert _spool(db) == (90.0, 1)
    assert _usage(db)[0].print_job_id == 5001


def test_prusalink_and_elegoo_completion_never_writes_stock():
    """job_events (PrusaLink/Elegoo/OctoPrint) records the print; the archive path accounts it."""
    for target in (job_census.SPOOLS, job_census.SPOOL_USAGE):
        writes, _, _ = job_census.scan(target=target)
        assert not any(path.endswith("notifications/job_events.py") for path, *_ in writes)


# ---------------- archive accounting ----------------

def _archive_client(engine, monkeypatch):
    from core import db as core_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.jobs.routes import jobs_lifecycle
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, "EducationPolicyProvider", EducationPolicyService())
    with engine.begin() as conn:
        conn.execute(text("UPDATE print_files SET job_id=7099 WHERE id=7099"))
        conn.execute(text("UPDATE jobs SET status='printing', quantity=1, hold=0 WHERE id=7099"))
        conn.execute(text("UPDATE spools SET location_printer_id=9, location_slot=id-7098, status='active' WHERE id IN (7099,7100)"))
    app = FastAPI(); app.include_router(jobs_lifecycle.router)

    def request_db():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[core_db.get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: {"id": 3, "role": "operator", "group_id": 1, "is_active": True}
    return TestClient(app)


def _balances(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id")).scalars().all()


def _accounting(engine, print_job_id=7099):
    with engine.connect() as conn:
        return tuple(conn.execute(text("SELECT accounting_state, accounting_reason FROM print_archives WHERE print_job_id=:p"),
                                  {"p": print_job_id}).one())


def test_archive_then_operator_completion_deducts_once(archive_case, monkeypatch):
    archive, engine = archive_case
    client = _archive_client(engine, monkeypatch)
    assert archive.create_print_archive(7099, 9, True) is not None
    assert _accounting(engine) == ("accounted", None) and _balances(engine) == [960, 960]
    assert client.post("/jobs/7099/complete").status_code == 200    # the operator confirms the same print
    assert _balances(engine) == [960, 960]


def test_operator_completion_then_archive_is_reconciled_not_deducted_twice(archive_case, monkeypatch):
    archive, engine = archive_case
    client = _archive_client(engine, monkeypatch)
    assert client.post("/jobs/7099/complete").status_code == 200
    assert _balances(engine) == [960, 960]
    assert archive.create_print_archive(7099, 9, True) is not None
    assert _accounting(engine) == ("unresolved", "job_usage_unattributed")
    assert _balances(engine) == [960, 960]


def test_moonraker_deduction_then_its_archive_is_already_accounted(archive_case, monkeypatch):
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    archive, engine = archive_case
    with engine.begin() as conn:
        assert record_job_filament(sqlalchemy_runner(conn), 7099, source="moonraker", print_job_id=7099,
                                   grams_by_spool={7099: 40, 7100: 40}).deducted
    assert archive.create_print_archive(7099, 9, True) is not None
    assert _accounting(engine) == ("accounted", "attempt_already_accounted")
    assert _balances(engine) == [960, 960]


# ---------------- correction cycles, reset / reprint ----------------

def test_correction_cycles_stay_on_one_attempt_and_reset_reprints_deduct_again(lifecycle_client):
    client, db = lifecycle_client
    for route in ("complete", "fail", "complete", "fail", "complete"):
        assert client.post(f"/jobs/111/{route}").status_code == 200
    assert _spool(db) == (90.0, 1)
    assert client.post("/jobs/111/reset").status_code == 200
    db.execute(text("UPDATE jobs SET printer_id=9 WHERE id=111")); db.commit()
    assert client.post("/jobs/111/start").status_code == 200
    assert client.post("/jobs/111/complete").status_code == 200
    assert _spool(db) == (80.0, 2)
    assert "quota_attempt:started:111:1" in _keys(db)


# ---------------- pre-upgrade data ----------------

LEGACY_NOTE = "Auto-deducted on job #111 complete (Fixture print)"


def _legacy_row(db, at):
    db.execute(text("UPDATE spools SET remaining_weight_g=90 WHERE id=111"))
    db.execute(text("INSERT INTO spool_usage(spool_id,job_id,weight_used_g,used_at,notes) VALUES(111,111,10,:at,:n)"),
               {"at": at, "n": LEGACY_NOTE})
    db.execute(text("UPDATE jobs SET status='failed' WHERE id=111")); db.commit()


def test_pre_upgrade_deduction_without_reset_is_not_repeated(lifecycle_client):
    client, db = lifecycle_client
    _legacy_row(db, "2026-01-01 10:00:00")
    assert client.post("/jobs/111/complete").status_code == 200
    assert _spool(db) == (90.0, 1)
    assert "quota_attempt:filament:job:111:0" not in _keys(db)   # nothing deducted, nothing claimed


def test_pre_upgrade_reset_is_anchored_at_the_audited_reset(lifecycle_client):
    """No counter (the reset predates it): attempt 0 begins at the last job.reset audit entry."""
    client, db = lifecycle_client
    _legacy_row(db, "2026-01-01 10:00:00")
    db.execute(text("INSERT INTO audit_logs(timestamp,action,entity_type,entity_id) VALUES('2026-01-02 09:00:00','job.reset','job',111)"))
    db.commit()
    assert client.post("/jobs/111/complete").status_code == 200   # the reprint after that reset
    assert _spool(db) == (80.0, 2)


@pytest.mark.parametrize("row_after_start,expected", [(False, (80.0, 2)), (True, (90.0, 1))])
def test_pre_upgrade_reset_without_audit_falls_back_to_actual_start(lifecycle_client, row_after_start, expected):
    client, db = lifecycle_client
    start = datetime(2026, 1, 5, 12, 0, 0)
    _legacy_row(db, (start + timedelta(hours=1) if row_after_start else start - timedelta(days=1)).isoformat(" "))
    db.execute(text("UPDATE jobs SET actual_start=:s WHERE id=111"), {"s": start}); db.commit()
    assert client.post("/jobs/111/complete").status_code == 200
    assert _spool(db) == expected


# ---------------- manual spool use / weigh ----------------

def _spool_client(client):
    from fastapi.responses import JSONResponse
    from core.errors import OdinError
    from modules.inventory.routes import spools
    client.app.include_router(spools.router)
    client.app.add_exception_handler(OdinError, lambda request, exc: JSONResponse(status_code=exc.status,
                                                                                  content=exc.to_envelope()))
    client.app.middleware_stack = None  # rebuild with the handler (the client already started the app)
    return client


def test_manual_spool_use_naming_the_job_is_an_adjustment_not_its_deduction(lifecycle_client):
    client, db = lifecycle_client
    _spool_client(client)
    used = client.post("/spools/111/use", json={"weight_used_g": 5, "job_id": 111, "notes": "purge line"})
    assert used.status_code == 200, used.text
    assert _spool(db) == (95.0, 1)
    assert client.post("/jobs/111/complete").status_code == 200        # the print itself still deducts
    assert _spool(db) == (85.0, 2)
    assert [row.notes for row in _usage(db)] == ["manual: purge line", "auto:operator_complete: job #111 complete (Fixture print)"]


def test_manual_spool_use_refuses_non_positive_grams(lifecycle_client):
    client, db = lifecycle_client
    _spool_client(client)
    assert client.post("/spools/111/use", json={"weight_used_g": -5}).status_code == 400
    assert _spool(db) == (100.0, 0)


def test_manual_usage_notes_fit_the_column_with_their_prefix(lifecycle_client):
    from core.filament_ledger import MANUAL_NOTES_MAX
    client, db = lifecycle_client
    _spool_client(client)
    assert MANUAL_NOTES_MAX == 255 - len("manual: ")
    assert client.post("/spools/111/use", json={"weight_used_g": 1, "notes": "n" * (MANUAL_NOTES_MAX + 1)}).status_code == 422
    assert _spool(db) == (100.0, 0)
    assert client.post("/spools/111/use", json={"weight_used_g": 1, "notes": "n" * MANUAL_NOTES_MAX}).status_code == 200
    db.expire_all()
    assert len(db.execute(text("SELECT notes FROM spool_usage WHERE spool_id=111")).scalar_one()) == 255


def test_automatic_labels_are_capped_at_the_column_width(lifecycle_client):
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    client, db = lifecycle_client
    assert record_job_filament(sqlalchemy_runner(db), 111, source="operator_complete",
                               grams_by_spool={111: 10}, notes="y" * 400).deducted
    db.commit()
    assert len(_usage(db)[0].notes) == 255 and _usage(db)[0].notes.startswith("auto:operator_complete: y")


def test_weighing_records_a_manual_adjustment_that_never_counts_as_a_deduction(lifecycle_client):
    client, db = lifecycle_client
    _spool_client(client)
    db.execute(text("UPDATE spools SET spool_weight_g=200 WHERE id=111")); db.commit()
    assert client.post("/spools/111/weigh", json={"gross_weight_g": 297}).status_code == 200
    db.expire_all()
    assert db.execute(text("SELECT remaining_weight_g FROM spools WHERE id=111")).scalar_one() == 97
    assert db.execute(text("SELECT notes FROM spool_usage WHERE spool_id=111")).scalars().all() == ["manual: weigh adjustment"]
    assert client.post("/jobs/111/complete").status_code == 200
    assert _spool(db) == (87.0, 1)                                      # only job-linked rows: the deduction


# ---------------- nothing to deduct ----------------

@pytest.mark.parametrize("setup", ["UPDATE spools SET remaining_weight_g=NULL WHERE id=111",
                                   "UPDATE filament_slots SET assigned_spool_id=NULL WHERE printer_id=9 AND slot_number=1"],
                         ids=["unknown-balance", "no-matching-spool"])
def test_nothing_deducted_claims_nothing_so_a_correction_still_deducts(lifecycle_client, setup):
    client, db = lifecycle_client
    db.execute(text(setup)); db.commit()
    assert client.post("/jobs/111/complete").status_code == 200
    assert "quota_attempt:filament:job:111:0" not in _keys(db)
    db.execute(text("UPDATE spools SET remaining_weight_g=100, location_slot=1 WHERE id=111"))
    db.execute(text("UPDATE filament_slots SET assigned_spool_id=111 WHERE printer_id=9 AND slot_number=1"))
    db.execute(text("UPDATE jobs SET status='failed' WHERE id=111")); db.commit()
    assert client.post("/jobs/111/complete").status_code == 200        # operator fixed the spool, corrected
    assert _spool(db) == (90.0, 1)


def test_moonraker_with_unknown_balance_does_not_claim(lifecycle_client):
    client, db = lifecycle_client
    deduct = _moonraker(db)
    db.execute(text("UPDATE spools SET remaining_weight_g=NULL WHERE id=111")); db.commit()
    assert deduct().state == "nothing_deducted"
    assert "quota_attempt:filament:job:111:0" not in _keys(db)


# ---------------- concurrency ----------------

def test_two_deducers_that_both_pass_the_read_check_deduct_once(lifecycle_client, monkeypatch):
    from core import filament_ledger
    from core.print_attempts import sqlalchemy_runner
    client, db = lifecycle_client
    monkeypatch.setattr(filament_ledger, "prior_attempt_usage", lambda *args, **kwargs: None)
    engine = db.get_bind()
    results = []
    for source in ("operator_complete", "moonraker"):
        with engine.begin() as conn:
            results.append(filament_ledger.record_job_filament(sqlalchemy_runner(conn), 111, source=source,
                                                                grams_by_spool={111: 10}).state)
    assert results == ["deducted", "already_deducted"]
    assert _spool(db) == (90.0, 1)


def test_concurrent_writers_serialise_on_the_claim(lifecycle_client):
    """The second writer waits on the first transaction and then loses the claim (SQLite writer lock)."""
    from core import filament_ledger
    from core.print_attempts import sqlalchemy_runner
    client, db = lifecycle_client
    engine = db.get_bind()
    first = engine.connect(); tx = first.begin()
    assert filament_ledger.record_job_filament(sqlalchemy_runner(first), 111, source="operator_complete",
                                               grams_by_spool={111: 10}).deducted
    outcome = {}

    def second():
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA busy_timeout=10000")
            outcome["state"] = filament_ledger.record_job_filament(
                sqlalchemy_runner(conn), 111, source="moonraker", grams_by_spool={111: 10}).state
    worker = threading.Thread(target=second); worker.start()
    worker.join(0.5)
    assert worker.is_alive()                                           # blocked behind the first writer
    tx.commit(); first.close()
    worker.join(10)
    assert outcome == {"state": "already_deducted"} and _spool(db) == (90.0, 1)


# ---------------- census: the ledger is the only consumption writer ----------------

LEDGER = "backend/core/filament_ledger.py"
FILAMENT_TABLES = {"spools", "spool_usage"}


def _function_node(path, name):
    import ast
    tree = ast.parse((job_census.ROOT / path).read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _tables_at(sites, path, resolve_parameter=None):
    """Resolve the table expression of each site's SQL (its first interpolation)."""
    tables = set()
    for site in sites:
        parts = job_census.dynamic_parts(site.detail)
        expression = parts[1] if parts and parts[0] == "prefix" else parts[0]   # 'public.' + table
        if resolve_parameter and expression in resolve_parameter:
            tables |= resolve_parameter[expression]()
        else:
            tables.add(job_census.resolve(path, expression))
    return tables


def _resolved_call_args(path, callee, index):
    return lambda: {job_census.resolve(path, a) if not a.startswith("'") else a.strip("'")
                    for a in job_census.call_arguments(path, callee, index)}


def _never_filament(path, resolve_parameter=None):
    return lambda sites: not (_tables_at(sites, path, resolve_parameter) & FILAMENT_TABLES)


def _controlled_mark_tables(sites):
    path = "backend/modules/organizations/routes_controlled_parts.py"
    # change_mark writes `'controlled_' + resource_type`; every backend call passes a literal 'files' or
    # 'jobs' positionally, so the table is controlled_files / controlled_jobs, never a filament table.
    function = ast.parse(job_census.function_source(path, "change_mark")).body[0]
    stores = [n for n in ast.walk(function) if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign))
              and any(isinstance(t, ast.Name) and t.id == "table"
                      for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    assigned = len(stores) == 1 and isinstance(stores[0], ast.Assign) and ast.unparse(stores[0].value) == "'controlled_' + resource_type"
    arguments, keyword_or_dynamic = set(), False
    for source in job_census.backend_sources().values():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == "change_mark":
                literal = len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                keyword_or_dynamic |= not literal or any(k.arg in (None, "resource_type") for k in node.keywords)
                if literal:
                    arguments.add(node.args[1].value)
    tables = {"controlled_" + value for value in arguments}
    return (all(job_census.dynamic_parts(site.detail)[0] == "table" for site in sites) and assigned
            and not keyword_or_dynamic and tables == {"controlled_files", "controlled_jobs"}
            and not tables & FILAMENT_TABLES)


def _exact(*details):
    return lambda sites: sorted(site.detail for site in sites) == sorted(details)


def _source_factory(sites):
    path = "backend/core/economic_source_creation.py"
    # The site inserts into its `table` parameter, which _insert passes through,
    # and _insert is only ever called for spools (a fresh spool's opening balance) and print_jobs.
    return (all(job_census.dynamic_parts(site.detail)[0] == "table" for site in sites)
            and job_census.call_arguments(path, "_insert_inside", 1) == ["table"]
            and set(job_census.call_arguments(path, "_insert", 1)) == {"'spools'", "'print_jobs'"})


def _source_factory_not_usage(sites):
    return _source_factory(sites) and "spool_usage" not in {
        a.strip("'") for a in job_census.call_arguments("backend/core/economic_source_creation.py", "_insert", 1)}


def _enum_repair_columns(sites):
    import ast
    node = _function_node("backend/core/schema/migrations/001_legacy_columns.py", "apply")
    rules = next(n.value for n in ast.walk(node) if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) == "enum_rules" for t in n.targets))
    columns = {ast.literal_eval(rule)[1] for rule in rules.elts}
    return (columns == {"status"} and all(job_census.dynamic_parts(site.detail)[:2] == ["table_name", "column_name"]
                                         for site in sites))


def _quiet_hours_tables(sites):
    path = "backend/core/schema/migrations/004_quiet_hours_scoped_claims.py"
    tables = set(job_census.resolve(path, "TABLES"))
    return not (tables & FILAMENT_TABLES) and all(
        job_census.dynamic_parts(site.detail)[0] == "replacement" and job_census.dynamic_parts(site.detail)[-1] == "table"
        for site in sites)


def _purchase_evidence_set_list(sites):
    import ast
    node = _function_node("backend/core/spool_purchase_evidence.py", "write_purchase")
    source = ast.unparse(node)
    updates = next(n.value for n in ast.walk(node) if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", None) == "updates" for t in n.targets) and isinstance(n.value, ast.Dict))
    keys = {ast.literal_eval(k) for k in updates.keys}
    return (all(job_census.dynamic_parts(site.detail) == ["assignments"] for site in sites)
            and keys == {"purchase_evidence_nonce", "purchase_revision"}
            and "set(legacy_fields) - {'price', 'initial_weight_g'}" in source)


PRICED = "backend/core/priced_estimate_restore.py"
PRICED_TABLES = {"table": lambda: _resolved_call_args(PRICED, "_insert", 1)() | _resolved_call_args(PRICED, "_cas", 1)()}

# Writers the census sees outside the ledger that do not consume filament.
# Each (file, function, kind) pins its exact number of call sites, and its check
# runs on those sites: the SQL text the node builds, or the call it makes.
SHARED_DYNAMIC = {
    ("backend/core/global_order_origin_schema.py", "apply", "sql insert (dynamic table)"):
        (1, "order-origin exclusions table", _never_filament("backend/core/global_order_origin_schema.py")),
    ("backend/core/power_target_operations.py", "_stage_operation", "sql insert (dynamic table)"):
        (1, "power-operation table", _never_filament("backend/core/power_target_operations.py")),
    ("backend/core/power_target_operations.py", "_stage_transition", "sql update (dynamic table)"):
        (1, "power-operation table", _never_filament("backend/core/power_target_operations.py")),
    ("backend/core/priced_estimate_restore.py", "_insert", "sql insert (dynamic table)"):
        (1, "priced-estimate restore tables", _never_filament(PRICED, PRICED_TABLES)),
    ("backend/core/priced_estimate_restore.py", "_cas", "sql update (dynamic table)"):
        (1, "priced-estimate restore tables", _never_filament(PRICED, PRICED_TABLES)),
    ("backend/core/schema/migrations/001_legacy_columns.py", "apply", "sql update (dynamic table)"):
        (1, "legacy enum casing repair of status columns only", _enum_repair_columns),
    ("backend/core/schema/migrations/004_quiet_hours_scoped_claims.py", "apply", "sql insert (dynamic table)"):
        (1, "quiet-hours claim tables", _quiet_hours_tables),
    # Integration (migration 032, cert-fix-E): the module's TABLE constant is the deferral table.
    ("backend/modules/notifications/deferred_deliveries.py", "record_suppressed", "sql insert (dynamic table)"):
        (1, "quiet-hours deferred-delivery table", _never_filament("backend/modules/notifications/deferred_deliveries.py")),
    ("backend/modules/organizations/routes_controlled_parts.py", "change_mark", "sql insert (dynamic table)"):
        (1, "controlled-part marking tables (controlled_files / controlled_jobs)", _controlled_mark_tables),
}
ALLOWED = {
    "spools": {
        **SHARED_DYNAMIC,
        ("backend/core/economic_source_creation.py", "_insert_inside", "sql insert (dynamic table)"):
            (1, "a fresh spool's opening balance (spool birth through the source factory)", _source_factory),
        ("backend/core/spool_purchase_evidence.py", "write_purchase", "sql update (dynamic SET)"):
            (1, "purchase evidence: the SET list is limited to price/initial_weight_g", _purchase_evidence_set_list),
        ("backend/modules/inventory/routes/spools.py", "update_spool", "setattr"):
            (1, "operator edit of spool fields, including an absolute weight correction",
             _exact("setattr(spool, field, value)")),
    },
    "spool_usage": {
        **SHARED_DYNAMIC,
        ("backend/core/economic_source_creation.py", "_insert_inside", "sql insert (dynamic table)"):
            (1, "the source factory inserts spools and print_jobs, never spool_usage", _source_factory_not_usage),
    },
}


@pytest.mark.parametrize("target", [job_census.SPOOLS, job_census.SPOOL_USAGE], ids=lambda t: t.table)
def test_census_no_filament_stock_writer_outside_the_ledger(target):
    writes, _, _ = job_census.scan(job_census.backend_sources(("backend/scripts/seed_",)), target)
    outside = [w for w in writes if w.path != LEDGER]
    found = job_census.counts(outside)
    allowed = ALLOWED[target.table]
    assert {key: n for key, n in found.items() if key not in allowed} == {}, "written outside core.filament_ledger"
    assert {key: (n, allowed[key][0]) for key, n in found.items() if allowed[key][0] != n} == {}, "call-site count changed"
    assert set(allowed) - set(found) == set(), f"stale allowance: {set(allowed) - set(found)}"
    for key, (count, reason, check) in allowed.items():
        assert check([w for w in outside if w[:3] == key]), (key, reason)
    assert any(w.path == LEDGER for w in writes)  # the ledger itself is seen


def test_census_refuses_a_weight_write_reintroduced_in_the_monitor():
    """AMS no longer has a stock-writer exception; any direct write is refused."""
    path = "backend/modules/printers/monitors/ams_inventory.py"
    sources = job_census.backend_sources(("backend/scripts/seed_",))
    sources[path] = sources[path].replace(
        "    return len(trays)",
        "    conn.execute('UPDATE spools SET remaining_weight_g=0 WHERE id=?', (1,))\n    return len(trays)")
    writes, _, _ = job_census.scan(sources, job_census.SPOOLS)
    key = (path, "sync_ams_inventory", "sql update")
    assert job_census.counts(writes)[key] == 1
    assert key not in ALLOWED["spools"]


def _filament_writes(source):
    found = []
    for target in (job_census.SPOOLS, job_census.SPOOL_USAGE):
        writes, _, _ = job_census.scan({"backend/modules/planted.py": source}, target)
        found += writes
    return found


@pytest.mark.parametrize("planted", [
    "def f(db):\n    db.execute(text('INSERT INTO spool_usage (spool_id) VALUES (1)'))\n",
    "def f(db):\n    db.execute(text('insert or ignore into \"spool_usage\" values (1)'))\n",
    "def f(db):\n    db.execute(text('INSERT INTO public.spool_usage (spool_id) VALUES (1)'))\n",
    "def f(db):\n    db.execute(text('UPDATE \"public\".\"spools\" SET remaining_weight_g=0 WHERE id=1'))\n",
    "def f(db):\n    db.execute(text('UPDATE spools '\n                    'SET remaining_weight_g = remaining_weight_g - :g '\n                    'WHERE id = :id'))\n",
    "def f(db):\n    db.execute(text('UPDATE spools ' + 'SET remaining_weight_g=0'))\n",
    "def f(db):\n    db.execute(text('UPDATE spools sp SET remaining_weight_g=0'))\n",
    "def f(db):\n    db.execute(text('INSERT INTO spools (id, remaining_weight_g) VALUES (1, 5) ON CONFLICT (id) DO UPDATE SET remaining_weight_g=excluded.remaining_weight_g'))\n",
    "def f(db, s):\n    db.add(SpoolUsage(spool_id=s.id))\n",
    "def f(db):\n    db.execute(insert(SpoolUsage.__table__).values(spool_id=1))\n",
    "def f(db):\n    db.execute(insert(SpoolUsage).values(spool_id=1))\n",
    "def f(db):\n    db.execute(SpoolUsage.__table__.insert().values(spool_id=1))\n",
    "def f(db, rows):\n    db.bulk_insert_mappings(SpoolUsage, rows)\n",
    "def f(s, g):\n    s.remaining_weight_g -= g\n",
    "def f(s, g):\n    s.remaining_weight_g = max(0, s.remaining_weight_g - g)\n",
    "def f(db):\n    db.query(Spool).filter(Spool.id == 1).update({'remaining_weight_g': 0})\n",
    "def f(db):\n    (db.query(Spool)\n       .filter(Spool.id == 1)\n       .filter(Spool.org_id == 2)\n       .update({'remaining_weight_g': 0}))\n",
    "def f(db):\n    db.execute(Spool.__table__.update().values(remaining_weight_g=0))\n",
    "def f(db):\n    spools = Spool.__table__\n    db.execute(spools.update().values(remaining_weight_g=0))\n",
    "def f(db, metadata):\n    usage = Table('spool_usage', metadata)\n    db.execute(usage.insert().values(spool_id=1))\n",
    "def f(db):\n    db.execute(sa_update(Spool).values(x=1))\n",
    "def f(spool, k, v):\n    setattr(spool, k, v)\n",
    "def f(x, v):\n    setattr(x, 'remaining_weight_g', v)\n",
    "def f(u):\n    u.weight_used_g = 3\n",
    "def f(db):\n    found = find_spool_by_rfid('t')\n    setattr(found, 'notes', 'x')\n    setattr(found, k, 1)\n",
    "q = 'INSERT INTO spools VALUES (1, 2, 3)'",
    "q = 'MERGE INTO spools s USING x ON s.id=x.id WHEN MATCHED THEN UPDATE SET remaining_weight_g=x.w'",
    "q = 'MERGE INTO spool_usage u USING x ON 1=0 WHEN NOT MATCHED THEN INSERT (spool_id) VALUES (x.id)'",
    "q = 'UPDATE spools SET (status, remaining_weight_g) = (:s, :w) WHERE id=:id'",
    "def f(db, g):\n    db.execute(text('UPDATE spools SET remaining_weight_g={} WHERE id=1'.format(g)))",
    "def f(cur, g):\n    cur.execute('INSERT INTO spool_usage (spool_id, weight_used_g) VALUES (1, %s)' % g)",
    "q = 'UPDATE spools /* adjust */ SET remaining_weight_g=0'",
    "from modules.inventory.models import SpoolUsage as U\ndef f(db):\n    db.add(U(spool_id=1))",
    "import modules.inventory.models as m\ndef f(db):\n    db.query(m.Spool).filter(m.Spool.id == 1).update({'x': 0})",
])
def test_census_catches_planted_writers(planted):
    assert _filament_writes(planted), planted


def test_census_ignores_reads_and_other_tables():
    assert not _filament_writes("def f(db):\n    db.execute(text('SELECT remaining_weight_g FROM spools'))\n")
    assert not _filament_writes("def f(db):\n    db.execute(text('UPDATE spools SET id = id WHERE id = 1'))\n")
    assert not _filament_writes("def f(db):\n    db.execute(text('UPDATE spool_lots SET remaining_weight_g=0'))\n")
    assert not _filament_writes("def f(db):\n    db.execute(text('UPDATE spools SET status=:s WHERE id=1'))\n")


# ---------------- round 6: a print is accounted to its own attempt ----------------

def _reset_after_print(client, engine, job_id=7099):
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='completed' WHERE id=:j AND status!='completed'"), {"j": job_id})
    assert client.post(f"/jobs/{job_id}/reset").status_code == 200       # attempt 1 begins now (after the print)


def _reprint(client, engine, job_id=7099):
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET printer_id=9 WHERE id=:j"), {"j": job_id})
    assert client.post(f"/jobs/{job_id}/start").status_code == 200
    assert client.post(f"/jobs/{job_id}/complete").status_code == 200


def test_late_archive_after_reset_sees_the_operator_deduction_of_its_own_attempt(archive_case, monkeypatch):
    archive, engine = archive_case
    client = _archive_client(engine, monkeypatch)
    assert client.post("/jobs/7099/complete").status_code == 200        # attempt 0, job-level
    assert _balances(engine) == [960, 960]
    _reset_after_print(client, engine)
    assert archive.create_print_archive(7099, 9, True) is not None       # the attempt-0 print archives late
    assert _accounting(engine) == ("unresolved", "job_usage_unattributed")
    assert _balances(engine) == [960, 960]


def test_late_archive_claims_its_own_attempt_not_the_next_one(archive_case, monkeypatch):
    archive, engine = archive_case
    client = _archive_client(engine, monkeypatch)
    _reset_after_print(client, engine)
    assert archive.create_print_archive(7099, 9, True) is not None       # attempt-0 print, archived in attempt 1
    assert _accounting(engine) == ("accounted", None) and _balances(engine) == [960, 960]
    with engine.connect() as conn:
        keys = set(conn.execute(text("SELECT key FROM system_config WHERE key LIKE 'quota_attempt:filament:job:7099:%'")).scalars())
    assert "quota_attempt:filament:job:7099:0" in keys and "quota_attempt:filament:job:7099:1" not in keys
    _reprint(client, engine)                                              # attempt 1 deducts its own print
    assert _balances(engine) == [920, 920]


def test_startup_recovery_accounts_a_late_print_to_its_own_attempt(archive_case, monkeypatch):
    archive, engine = archive_case
    client = _archive_client(engine, monkeypatch)
    _reset_after_print(client, engine)
    result = archive.recover_missing_archives()
    assert result["recovered"] == 1 and _balances(engine) == [960, 960]
    _reprint(client, engine)
    assert _balances(engine) == [920, 920]


@pytest.mark.parametrize("print_started,expected", [
    ("2026-01-01 08:00:00", ("unresolved", "legacy_usage_unattributed")),  # same attempt as the legacy row
    ("2026-01-03 08:00:00", ("accounted", None)),                          # after the audited reset
])
def test_legacy_operator_rows_belong_to_the_attempt_of_their_time(archive_case, monkeypatch, print_started, expected):
    archive, engine = archive_case
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO spool_usage(spool_id,job_id,weight_used_g,used_at,notes) "
                          "VALUES(7099,7099,40,'2026-01-01 10:00:00','Auto-deducted on job #7099 complete (Synthetic)')"))
        conn.execute(text("INSERT INTO audit_logs(timestamp,action,entity_type,entity_id) VALUES('2026-01-02 09:00:00','job.reset','job',7099)"))
        conn.execute(text("UPDATE print_jobs SET started_at=:s WHERE id=7099"), {"s": print_started})
    assert archive.create_print_archive(7099, 9, True) is not None
    assert _accounting(engine) == expected


def test_a_bound_print_keeps_its_attempt_and_that_attempts_window_whatever_its_started_at(lifecycle_client):
    """Bound to attempt 0, with a clock-skewed started_at after the reset: the print is
    accounted against attempt 0's window, so attempt 0's marker-less legacy deduction
    is seen (reconcile, no second deduction) -- not missed by a started_at window."""
    from core.filament_ledger import LEGACY_JOB_USAGE, print_attempt, record_job_filament
    from core.print_attempts import bind_print_attempt, bump_print_attempt, sqlalchemy_runner
    client, db = lifecycle_client
    _print_job(db, 5101)
    run = sqlalchemy_runner(db)
    bind_print_attempt(run, 5101, 111)                                    # created in attempt 0
    db.execute(text("INSERT INTO spool_usage(spool_id,job_id,weight_used_g,used_at,notes) VALUES(111,111,10,:at,:n)"),
               {"at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1), "n": LEGACY_NOTE})
    db.execute(text("UPDATE spools SET remaining_weight_g=90 WHERE id=111"))
    bump_print_attempt(run, 111)                                          # the job is reset
    db.execute(text("UPDATE print_jobs SET started_at='2099-01-01 00:00:00' WHERE id=5101"))   # skewed clock
    view = print_attempt(run, 111, 5101)
    assert view.seq == 0 and view.end is not None                         # attempt 0 ends at the reset
    bind_print_attempt(run, 5101, 111)                                    # first binding wins
    assert print_attempt(run, 111, 5101).seq == 0
    outcome = record_job_filament(run, 111, source="archive", print_job_id=5101, grams_by_spool={111: 10})
    assert (outcome.state, outcome.reason) == ("already_deducted", LEGACY_JOB_USAGE)
    db.commit()
    assert _spool(db) == (90.0, 1)                                         # no second deduction
    keys = _keys(db)
    assert not any(key.startswith("quota_attempt:filament:job:111:") for key in keys), keys


def test_rows_the_ledger_wrote_are_never_taken_for_legacy_usage(lifecycle_client):
    """A ledger row is covered by its own claim marker; only marker-less legacy rows block."""
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    client, db = lifecycle_client
    _print_job(db, 5103)
    db.execute(text("INSERT INTO spool_usage(spool_id,job_id,weight_used_g,used_at,notes) VALUES(111,111,1,:at,"
                    "'auto:operator_complete: job #111 complete (another attempt)')"),
               {"at": datetime.now(timezone.utc).replace(tzinfo=None)})
    db.commit()
    outcome = record_job_filament(sqlalchemy_runner(db), 111, source="archive", print_job_id=5103, grams_by_spool={111: 10})
    assert outcome.deducted, outcome
    db.commit()
    assert _spool(db)[0] == 90.0


def test_moonraker_flags_an_unattributable_print_for_reconciliation(lifecycle_client, caplog):
    client, db = lifecycle_client
    assert client.post("/jobs/111/complete").status_code == 200        # job-level deduction of attempt 0
    assert _spool(db) == (90.0, 1)
    _print_job(db, 5104)
    deduct = _moonraker(db, print_job_id=5104)
    outcome = deduct()
    assert outcome.unattributed and outcome.reason == "job_usage"
    assert _spool(db) == (90.0, 1)
    flag = json.loads(_keys(db)["quota_attempt:filament:job:111:reconcile:print:5104"])
    assert flag["reason"] == "job_usage" and flag["source"] == "moonraker"
    assert any("reconciliation required" in record.getMessage() for record in caplog.records)


# ---------------- round 7: link-print never re-attributes an earlier attempt's print ----------------

def test_link_print_refuses_a_print_that_started_before_the_last_reset(lifecycle_client):
    from core.print_attempts import bound_print_attempt, sqlalchemy_runner
    client, db = lifecycle_client
    db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(5105,9,'x','completed','2026-01-01 08:00:00')"))
    db.execute(text("UPDATE jobs SET status='completed' WHERE id=111")); db.commit()
    assert client.post("/jobs/111/reset").status_code == 200           # reset after the print started
    db.execute(text("UPDATE jobs SET printer_id=9 WHERE id=111")); db.commit()
    response = client.post("/jobs/111/link-print", params={"print_job_id": 5105})
    assert response.status_code == 409, response.text
    db.expire_all()
    assert db.execute(text("SELECT scheduled_job_id FROM print_jobs WHERE id=5105")).scalar_one() is None
    assert bound_print_attempt(sqlalchemy_runner(db), 5105, 111) is None
    db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(5106,9,'y','completed',:s)"),
               {"s": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=5)}); db.commit()
    assert client.post("/jobs/111/link-print", params={"print_job_id": 5106}).status_code == 200
    db.expire_all()
    assert bound_print_attempt(sqlalchemy_runner(db), 5106, 111) == 1


# ---------------- round 7: one precision for every window ----------------

def test_times_share_one_precision_so_no_row_falls_between_windows(lifecycle_client):
    """A reset anchor with microseconds, its audit entry and a row in the same second."""
    from core.filament_ledger import current_attempt, print_attempt
    from core.print_attempts import moment, sqlalchemy_runner
    client, db = lifecycle_client
    db.execute(text("INSERT INTO system_config(key,value) VALUES('quota_attempt:seq:111','1')"))
    db.execute(text("INSERT INTO system_config(key,value) VALUES('quota_attempt:started:111:1',:v)"),
               {"v": json.dumps("2026-01-02T09:00:00.700000+00:00")})
    db.execute(text("INSERT INTO audit_logs(timestamp,action,entity_type,entity_id) VALUES('2026-01-02 09:00:00','job.reset','job',111)"))
    db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) "
                    "VALUES(5107,9,111,'old','completed','2026-01-02 08:00:00')"))
    db.commit()
    run = sqlalchemy_runner(db)
    row_time = moment("2026-01-02 09:00:00")
    previous, current = print_attempt(run, 111, 5107), current_attempt(run, 111)
    assert [previous.holds(row_time), current.holds(row_time)] == [False, True]   # exactly one window
    assert previous.end == current.start == moment("2026-01-02T09:00:00Z")
    assert moment("2026-01-02T09:00:00.999999+00:00") == moment("2026-01-02 09:00:00")


def test_an_unparseable_anchor_is_reported_and_falls_back_to_audited_resets(lifecycle_client, caplog):
    from core.filament_ledger import current_attempt
    from core.print_attempts import moment, sqlalchemy_runner
    client, db = lifecycle_client
    db.execute(text("INSERT INTO system_config(key,value) VALUES('quota_attempt:seq:111','1')"))
    db.execute(text("INSERT INTO system_config(key,value) VALUES('quota_attempt:started:111:1',:v)"), {"v": json.dumps("garbage")})
    db.execute(text("INSERT INTO audit_logs(timestamp,action,entity_type,entity_id) VALUES('2026-01-02 09:00:00','job.reset','job',111)"))
    db.commit()
    view = current_attempt(sqlalchemy_runner(db), 111)
    assert view.start == moment("2026-01-02 09:00:00")
    messages = [record.getMessage() for record in caplog.records]
    assert any("Unparseable attempt anchor" in m for m in messages) and any("no reset anchor" in m for m in messages)


def test_postgres_sessions_are_pinned_to_utc(monkeypatch):
    from core import database_config
    captured = {}
    from sqlalchemy.engine import make_url
    monkeypatch.setattr(database_config, "assert_postgres_restore_ready", lambda *_: None)
    monkeypatch.setattr(database_config, "resolve_database_url", lambda url, **_: make_url(url))
    monkeypatch.setattr(database_config, "create_engine", lambda url, **kwargs: captured.update(kwargs) or
                        type("FakeEngine", (), {})())
    monkeypatch.setattr(database_config.event, "listens_for", lambda *a, **k: (lambda f: f))
    database_config.create_database_engine("postgresql+psycopg://odin@db/odin", role="api", password_file=None)
    assert "-c TimeZone=UTC" in captured["connect_args"]["options"]


def test_link_print_binds_the_print_to_the_jobs_current_attempt(lifecycle_client):
    from core.print_attempts import bound_print_attempt, sqlalchemy_runner
    client, db = lifecycle_client
    db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(5102,9,'x','completed',CURRENT_TIMESTAMP)"))
    db.commit()
    response = client.post("/jobs/111/link-print", params={"print_job_id": 5102})
    assert response.status_code == 200, response.text
    db.expire_all()
    assert bound_print_attempt(sqlalchemy_runner(db), 5102, 111) == 0


# ---------------- round 6: one lock order on every ledger path ----------------

def _ledger_statement_order(engine, action):
    """Run ``action`` and return the ledger's lock/claim statements in order.

    SQLite's statement trace sees SQLAlchemy and raw DBAPI (archive) paths alike.
    """
    from sqlalchemy import event
    seen, traced = [], []

    def record(statement):
        flat = " ".join(statement.split())
        if flat.startswith("UPDATE spools SET id = id") or flat.startswith("UPDATE spools SET id=id"):
            seen.append("lock spool")
        elif flat.startswith("INSERT INTO system_config") and "ON CONFLICT" in flat and "filament:" in flat:
            seen.append("claim")

    def checkout(dbapi_connection, record_, proxy):
        dbapi_connection.set_trace_callback(record)
        traced.append(dbapi_connection)
    event.listen(engine, "checkout", checkout)
    try:
        action()
    finally:
        event.remove(engine, "checkout", checkout)
        for connection in traced:
            connection.set_trace_callback(None)
    return seen


def _locks_before_claims(order):
    """No spool lock is ever taken after a claim (the order that could deadlock)."""
    assert "lock spool" in order and "claim" in order, order
    first_claim = order.index("claim")
    assert "lock spool" not in order[first_claim:], order


def test_operator_completion_locks_spools_before_claiming(lifecycle_client):
    client, db = lifecycle_client
    order = _ledger_statement_order(db.get_bind(), lambda: client.post("/jobs/111/complete"))
    _locks_before_claims(order)


def test_archive_accounting_locks_spools_before_claiming(archive_case):
    archive, engine = archive_case
    order = _ledger_statement_order(engine, lambda: archive.create_print_archive(7099, 9, True))
    _locks_before_claims(order)


# ---------------- round 7: every multi-spool writer locks in ascending id order ----------------

def test_bulk_spool_update_writes_in_ascending_id_order(lifecycle_client):
    from sqlalchemy import event
    from modules.inventory.routes import spool_ops
    client, db = lifecycle_client
    client.app.include_router(spool_ops.router)
    for spool_id in (112, 113):
        db.execute(text("INSERT INTO spools(id,filament_id,status,remaining_weight_g,initial_weight_g,org_id) "
                        "VALUES(:id,111,'archived',100,100,1)"), {"id": spool_id})
    db.commit()
    seen = []

    def record(conn, cursor, statement, parameters, *args):
        if statement.startswith("UPDATE spools SET status"):
            seen.append(parameters[0] if isinstance(parameters, (list, tuple)) else parameters.get("id"))
    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = client.post("/spools/bulk-update", json={"spool_ids": [113, 111, 112, 113], "action": "activate"})
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    assert seen == [111, 112, 113]


def test_operator_completion_does_not_charge_pending_identity(lifecycle_client):
    client, db = lifecycle_client
    db.execute(text('UPDATE filament_slots SET tray_observation=:state WHERE printer_id=9 AND slot_number=1'),
               {'state':json.dumps({'id':'a'*32,'pending':True,'observed':{}})})
    db.commit()
    assert client.post('/jobs/111/complete').status_code == 200
    assert _spool(db) == (100.0,0)
    assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='filament.unattributed' AND entity_id=111")).scalar_one()==1


def test_operator_completion_does_not_use_location_only_binding(lifecycle_client):
    client, db = lifecycle_client
    db.execute(text('UPDATE filament_slots SET assigned_spool_id=NULL WHERE printer_id=9'));db.commit()
    assert client.post('/jobs/111/complete').status_code == 200
    assert _spool(db) == (100.0,0)

# Explicit delegation is independent of the read-only display adapter.
def test_external_accounting_central_guard_has_no_claim_or_usage(lifecycle_client, monkeypatch):
    from core.config import settings
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    _, db = lifecycle_client
    _print_job(db, 5101)
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    result = record_job_filament(sqlalchemy_runner(db), 111, source="operator_complete",
                                 print_job_id=5101, grams_by_spool={111: 10})
    assert (result.state, result.reason) == ('nothing_deducted', 'external_accounting')
    assert _spool(db) == (100.0, 0)
    assert not _keys(db)
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [])
    assert record_job_filament(sqlalchemy_runner(db), 111, source="operator_complete",
                               print_job_id=5101, grams_by_spool={111: 10}).deducted


def test_external_accounting_preserves_completion_and_manual_adjustments(lifecycle_client, monkeypatch):
    from core.config import settings
    client, db = lifecycle_client
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    _spool_client(client)
    assert client.post('/spools/111/use', json={'weight_used_g': 5, 'job_id': 111}).status_code == 200
    db.execute(text('UPDATE spools SET spool_weight_g=200 WHERE id=111')); db.commit()
    assert client.post('/spools/111/weigh', json={'gross_weight_g': 290}).status_code == 200
    assert client.post('/jobs/111/complete').status_code == 200
    assert db.execute(text('SELECT status FROM jobs WHERE id=111')).scalar_one() == 'completed'
    assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=111')).scalar_one() == 90
    assert not _keys(db)
    assert all(row.notes.startswith('manual:') for row in _usage(db))


def test_display_binding_does_not_delegate_accounting(lifecycle_client, monkeypatch):
    from core.config import settings
    client, db = lifecycle_client
    monkeypatch.setattr(settings, 'filament_ledger_printers', {9: 'display-only'})
    assert client.post('/jobs/111/complete').status_code == 200
    assert _spool(db) == (90.0, 1)


def test_external_accounting_moonraker_skips_and_other_printers_still_account(lifecycle_client, monkeypatch, caplog):
    from core.config import settings
    _, db = lifecycle_client
    deduct = _moonraker(db)
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    with caplog.at_level("INFO", logger="moonraker_monitor"):
        assert deduct() is None
    assert "external_accounting" in caplog.text
    assert "Printer 9 job 111" in caplog.text
    assert _spool(db) == (100.0, 0)
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [999])
    assert deduct().deducted


@pytest.mark.parametrize("external_printer, expected_state", [(9, "nothing_deducted"), (10, "deducted")])
def test_external_accounting_uses_physical_printer_after_job_reassignment(lifecycle_client, monkeypatch, external_printer, expected_state):
    from core.config import settings
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    _, db = lifecycle_client
    _print_job(db, 5101)
    db.execute(text('UPDATE jobs SET printer_id=10 WHERE id=111')); db.commit()
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [external_printer])
    result = record_job_filament(sqlalchemy_runner(db), 111, source='archive',
                                 print_job_id=5101, grams_by_spool={111: 10})
    assert result.state == expected_state
    assert _spool(db) == ((100.0, 0) if external_printer == 9 else (90.0, 1))


def test_external_archive_records_delegation_without_outbox_or_claim(archive_case, monkeypatch):
    from core.config import settings
    archive, engine = archive_case
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    assert archive.create_print_archive(7099, 9, True) is not None
    assert _accounting(engine) == ('externally_managed', 'external_accounting')
    assert _balances(engine) == [1000, 1000]
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM filament_delivery_outbox')).scalar_one() == 0
        assert conn.execute(text('SELECT COUNT(*) FROM spool_usage')).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'quota_attempt:filament:%'")).scalar_one() == 0


@pytest.mark.parametrize('value', ['{}', '["9"]', '[true]', '[0]', '[-1]', '[1.5]', 'null', 'invalid'])
def test_external_accounting_configuration_rejects_invalid_ids(value):
    from core.config import Settings
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(_env_file=None, external_filament_accounting_printers=value)


def test_external_accounting_configuration_defaults_and_explicit_ids(monkeypatch):
    from core.config import Settings
    monkeypatch.delenv('EXTERNAL_FILAMENT_ACCOUNTING_PRINTERS', raising=False)
    assert Settings(_env_file=None).external_filament_accounting_printers == []
    monkeypatch.setenv('EXTERNAL_FILAMENT_ACCOUNTING_PRINTERS', '[9, 12]')
    assert Settings(_env_file=None).external_filament_accounting_printers == [9, 12]


def test_external_archive_explicit_reinvocation_rechecks_policy_and_physical_identity(archive_case, monkeypatch):
    from core.config import settings
    from modules.archives.attempt_accounting import account_archive
    archive, engine = archive_case
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    archive_id = archive.create_print_archive(7099, 9, True)
    with engine.begin() as conn:
        snapshot = conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=7099')).scalar_one()
        args = dict(archive_id=archive_id, print_job_id=7099, printer_id=9,
                    snapshot_json=snapshot, scheduled_job_id=7099, measured_grams=None, success=True)
        assert account_archive(conn, **args) == ('externally_managed', 'external_accounting', [])
        assert account_archive(conn, **dict(args, printer_id=999)) == ('unresolved', 'foreign_job_printer', [])
        assert account_archive(conn, **dict(args, print_job_id=999)) == ('unresolved', 'missing_physical_print', [])
        monkeypatch.setattr(settings, 'external_filament_accounting_printers', [])
        assert account_archive(conn, **args)[0] == 'accounted'
    assert _balances(engine) == [960, 960]


def test_missing_physical_print_cannot_bypass_accounting_policy(lifecycle_client, monkeypatch):
    from core.config import settings
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    _, db = lifecycle_client
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    result = record_job_filament(sqlalchemy_runner(db), 111, source='archive',
                                 print_job_id=999999, grams_by_spool={111: 10})
    assert (result.state, result.reason) == ('nothing_deducted', 'missing_physical_print')
    assert _spool(db) == (100.0, 0)
    assert not _keys(db)


def test_external_delegation_does_not_resolve_unknown_archive_ownership(archive_case, monkeypatch):
    from core.config import settings
    from modules.archives.attempt_accounting import capture_attempt_spools
    archive, engine = archive_case
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    with engine.begin() as conn:
        conn.execute(text("UPDATE education_upload_operations SET final_path='/opaque/archive-synthetic.3mf' WHERE operation_id='upload-1'"))
        conn.execute(text("UPDATE print_jobs SET status='running',spool_snapshot=NULL WHERE id=7099"))
        assert capture_attempt_spools(conn, 7099, 9, source='synthetic-unresolved-owner')
        conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7099"))
    assert archive.create_print_archive(7099, 9, True) is not None
    assert _accounting(engine) == ('unresolved', 'archive_ownership_unresolved')
    assert _balances(engine) == [1000, 1000]


@pytest.mark.parametrize('delegated, reset, expected', [([9], False, 100), ([10], False, 100),
                                                       ([9], True, 90), ([10], True, 100),
                                                       ([], False, 90), ([999], False, 90)])
def test_operator_reassignment_uses_current_attempt_accounting_policy(lifecycle_client, monkeypatch, delegated, reset, expected):
    from core.config import settings
    from core.print_attempts import bind_print_attempt, sqlalchemy_runner
    from modules.jobs.routes import jobs_crud
    client, db = lifecycle_client
    client.app.include_router(jobs_crud.router)
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', delegated)
    _print_job(db, 5201)
    bind_print_attempt(sqlalchemy_runner(db), 5201, 111)
    db.execute(text("INSERT INTO spools(id,filament_id,status,location_printer_id,location_slot,remaining_weight_g,initial_weight_g,org_id) VALUES(112,111,'active',10,1,100,100,1)"))
    db.execute(text('UPDATE filament_slots SET assigned_spool_id=112 WHERE printer_id=10 AND slot_number=1'))
    db.commit()
    # Single-spool printers have no AMS mapping to edit during reassignment.
    db.execute(text("UPDATE printers SET api_type='moonraker' WHERE id IN (9,10)"))
    db.execute(text('UPDATE jobs SET ams_mapping=NULL WHERE id=111')); db.commit()
    # The physical print failed. Reassignment alone is not a new attempt.
    assert client.post('/jobs/111/fail').status_code == 200
    changed = client.patch('/jobs/111', json={'printer_id': 10})
    assert changed.status_code == 200, changed.text
    if reset:
        assert client.post('/jobs/111/reset').status_code == 200
        assert client.patch('/jobs/111', json={'printer_id': 10}).status_code == 200
        # A real new physical print belongs to the reset attempt on printer 10.
        db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) VALUES(5202,10,111,'New attempt','failed',CURRENT_TIMESTAMP)"))
        bind_print_attempt(sqlalchemy_runner(db), 5202, 111)
        db.commit()
    completed = client.post('/jobs/111/complete')
    assert completed.status_code == 200, completed.text
    assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=112')).scalar_one() == expected
    assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=111')).scalar_one() == 100
    assert len(_usage(db)) == (1 if expected == 90 else 0)
    audit = json.loads(db.execute(text("SELECT details FROM audit_logs WHERE action='job.completed' AND entity_id=111 ORDER BY id DESC LIMIT 1")).scalar_one())
    expected_skip = None if expected == 90 else ('external_accounting' if reset or delegated == [9] else 'printer_reassigned')
    assert audit['accounting_skip'] == expected_skip


@pytest.mark.parametrize('delegated', [[9], [10]])
def test_job_only_ledger_cannot_bypass_physical_accounting_owner(lifecycle_client, monkeypatch, delegated):
    from core.config import settings
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    _, db = lifecycle_client
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', delegated)
    _print_job(db, 5201)
    db.execute(text('UPDATE jobs SET printer_id=10 WHERE id=111')); db.commit()
    result = record_job_filament(sqlalchemy_runner(db), 111, source='operator_complete', grams_by_spool={111: 10})
    assert result.state == 'nothing_deducted'
    assert result.reason == ('external_accounting' if delegated == [9] else 'printer_reassigned')
    assert _spool(db) == (100.0, 0)
    assert not _keys(db)


def test_multiple_current_prints_cannot_choose_local_history_over_external(lifecycle_client, monkeypatch):
    from core.config import settings
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    client, db = lifecycle_client
    _print_job(db, 5201)
    _print_job(db, 5202)
    db.execute(text('UPDATE print_jobs SET printer_id=10 WHERE id=5202')); db.commit()
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    result = record_job_filament(sqlalchemy_runner(db), 111, source='operator_complete', grams_by_spool={111: 10})
    assert (result.state, result.reason) == ('nothing_deducted', 'ambiguous_physical_print')
    assert _spool(db) == (100.0, 0)

    assert client.post('/jobs/111/complete').status_code == 200
    audit = json.loads(db.execute(text("SELECT details FROM audit_logs WHERE action='job.completed' AND entity_id=111")).scalar_one())
    assert audit['accounting_skip'] == 'ambiguous_physical_print'


def test_legacy_unbound_external_history_before_actual_start_is_not_current(lifecycle_client, monkeypatch):
    from core.config import settings
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    _, db = lifecycle_client
    _print_job(db, 5201)
    db.execute(text("UPDATE print_jobs SET started_at='2026-01-01 08:00:00' WHERE id=5201"))
    db.execute(text("UPDATE jobs SET printer_id=10,actual_start='2026-01-02 08:00:00' WHERE id=111")); db.commit()
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    assert record_job_filament(sqlalchemy_runner(db), 111, source='operator_complete', grams_by_spool={111: 10}).deducted


def test_bound_external_current_attempt_survives_clock_skew(lifecycle_client, monkeypatch):
    from core.config import settings
    from core.filament_ledger import record_job_filament
    from core.print_attempts import bind_print_attempt, sqlalchemy_runner
    _, db = lifecycle_client
    _print_job(db, 5201)
    bind_print_attempt(sqlalchemy_runner(db), 5201, 111)
    db.execute(text("UPDATE print_jobs SET started_at='2026-01-01 08:00:00' WHERE id=5201"))
    db.execute(text("UPDATE jobs SET printer_id=10,actual_start='2026-01-02 08:00:00' WHERE id=111")); db.commit()
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    result = record_job_filament(sqlalchemy_runner(db), 111, source='operator_complete', grams_by_spool={111: 10})
    assert result.reason == 'external_accounting'
    assert _spool(db) == (100.0, 0)


def test_unknown_legacy_print_time_cannot_exclude_external_owner(lifecycle_client, monkeypatch):
    from core.config import settings
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    _, db = lifecycle_client
    _print_job(db, 5201)
    db.execute(text('UPDATE print_jobs SET started_at=:started WHERE id=5201'), {'started': 'unparseable'})
    db.execute(text("UPDATE jobs SET printer_id=10,actual_start='2026-01-02 08:00:00' WHERE id=111")); db.commit()
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    result = record_job_filament(sqlalchemy_runner(db), 111, source='operator_complete', grams_by_spool={111: 10})
    assert result.reason == 'external_accounting'
    assert _spool(db) == (100.0, 0)


def test_empty_policy_preserves_legacy_missing_physical_behavior(lifecycle_client, monkeypatch):
    from core.config import settings
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    from modules.archives.attempt_accounting import account_archive
    _, db = lifecycle_client
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [])
    result = record_job_filament(sqlalchemy_runner(db), 111, source='archive',
                                 print_job_id=999999, grams_by_spool={})
    assert (result.state, result.reason) == ('nothing_deducted', None)
    args = dict(archive_id=999999, print_job_id=999999, printer_id=999999,
                snapshot_json=None, scheduled_job_id=111, measured_grams=None, success=True)
    assert account_archive(db.connection(), **args) == ('unresolved', 'missing_or_invalid_start_snapshot', [])
    _print_job(db, 5201)
    assert account_archive(db.connection(), **dict(args, print_job_id=5201)) == ('unresolved', 'missing_or_invalid_start_snapshot', [])


@pytest.mark.parametrize('delegated', [[], [9]])
def test_operator_evaluates_job_policy_once_per_transaction(lifecycle_client, monkeypatch, delegated):
    from core import filament_ledger
    from core.config import settings
    client, _ = lifecycle_client
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', delegated)
    original = filament_ledger.job_accounting_skip_reason
    calls = []
    def evaluate(run, job_id):
        calls.append(job_id)
        return original(run, job_id)
    monkeypatch.setattr(filament_ledger, 'job_accounting_skip_reason', evaluate)
    assert client.post('/jobs/111/complete').status_code == 200
    assert calls == [111]


def test_job_policy_decision_cannot_authorize_another_job_or_runner(lifecycle_client):
    from core.filament_ledger import job_accounting_decision, record_job_filament
    from core.print_attempts import sqlalchemy_runner
    _, db = lifecycle_client
    run = sqlalchemy_runner(db)
    decision = job_accounting_decision(run, 111)
    for runner, job, physical in ((run, 112, None), (sqlalchemy_runner(db), 111, None), (run, 111, 5201)):
        with pytest.raises(ValueError, match='this job and transaction runner'):
            record_job_filament(runner, job, source='operator_complete', print_job_id=physical,
                                grams_by_spool={111: 10}, _job_accounting=decision)
    assert _spool(db) == (100.0, 0)


def test_delegated_completion_charges_quota_without_starting_spoolman_push(lifecycle_client, monkeypatch):
    import threading
    from core.config import settings
    from modules.inventory import services
    client, db = lifecycle_client
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    db.execute(text('UPDATE jobs SET submitted_by=1,duration_hours=2 WHERE id=111'))
    db.execute(text('UPDATE spools SET spoolman_spool_id=123 WHERE id=111')); db.commit()
    pushes, starts = [], []
    monkeypatch.setattr(services, 'push_consumption_to_spoolman', lambda rows: pushes.append(rows))
    original_thread = threading.Thread
    def thread(*args, **kwargs):
        if getattr(kwargs.get('target'), '__name__', None) == '_push_async':
            starts.append(kwargs)
        return original_thread(*args, **kwargs)
    monkeypatch.setattr(threading, 'Thread', thread)
    assert client.post('/jobs/111/complete').status_code == 200
    assert tuple(db.execute(text('SELECT grams_used,hours_used FROM quota_usage WHERE user_id=1')).one()) == (10, 2)
    assert _spool(db) == (100.0, 0)
    assert not starts and not pushes
    assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'quota_attempt:filament:%'")).scalar_one() == 0
    audit = json.loads(db.execute(text("SELECT details FROM audit_logs WHERE action='job.completed' AND entity_id=111")).scalar_one())
    assert audit['accounting_skip'] == 'external_accounting'


@pytest.mark.parametrize('initial, final, expected', [([], [9], 'externally_managed'), ([9], [], 'accounted')])
def test_midprint_policy_flip_applies_whole_archive_consumption(archive_case, monkeypatch, initial, final, expected):
    from core.config import settings
    from modules.archives.attempt_accounting import capture_attempt_spools
    archive, engine = archive_case
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', initial)
    with engine.begin() as conn:
        conn.execute(text('UPDATE spools SET spoolman_spool_id=123 WHERE id=7099'))
        conn.execute(text("UPDATE print_jobs SET status='running',spool_snapshot=NULL WHERE id=7099"))
        assert capture_attempt_spools(conn, 7099, 9, source='synthetic-policy-flip')
        conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7099"))
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', final)
    assert archive.create_print_archive(7099, 9, True) is not None
    assert _accounting(engine)[0] == expected
    assert _balances(engine) == ([1000, 1000] if final else [960, 960])
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM filament_delivery_outbox')).scalar_one() == (0 if final else 1)
        assert conn.execute(text('SELECT COUNT(*) FROM spool_usage')).scalar_one() == (0 if final else 2)


def test_policy_flip_does_not_relabel_prior_physical_deduction(archive_case, monkeypatch):
    from core.config import settings
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    archive, engine = archive_case
    with engine.begin() as conn:
        assert record_job_filament(sqlalchemy_runner(conn), 7099, source='moonraker',
                                   print_job_id=7099, grams_by_spool={7099: 40, 7100: 40}).deducted
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    assert archive.create_print_archive(7099, 9, True) is not None
    assert _accounting(engine) == ('accounted', 'attempt_already_accounted')
    assert _balances(engine) == [960, 960]


def test_tray_confirmation_reaccount_respects_delegated_printer(archive_case, monkeypatch):
    from core.config import settings
    from modules.inventory.routes.filament_slots import confirm_tray_identity, ConfirmTrayIdentity
    from modules.archives.attempt_accounting import capture_attempt_spools
    archive, engine = archive_case
    state_id = 'a' * 32
    state = dict(id=state_id, pending=True, hold_id=state_id, observed={'type': 'PLA'})
    with engine.begin() as conn:
        conn.execute(text('UPDATE filament_slots SET tray_observation=:state WHERE printer_id=9 AND slot_number=1'), {'state': json.dumps(state)})
        conn.execute(text("UPDATE spools SET status='active',spoolman_spool_id=123 WHERE id IN (7099,7100)"))
        conn.execute(text("UPDATE print_jobs SET status='running',spool_snapshot=NULL WHERE id=7099"))
        assert capture_attempt_spools(conn, 7099, 9, source='synthetic-pending-tray')
        conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7099"))
    assert archive.create_print_archive(7099, 9, True) is not None
    assert _accounting(engine) == ('unresolved', 'spool_change_pending')
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    with Session(engine) as db:
        result = confirm_tray_identity(9, 1, ConfirmTrayIdentity(observed_state=state_id, decision='same'),
                                       current_user={'id': 3, 'role': 'admin', 'group_id': 1}, db=db)
    assert result['success'] is True
    assert _accounting(engine) == ('externally_managed', 'external_accounting')
    assert _balances(engine) == [1000, 1000]
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM filament_delivery_outbox')).scalar_one() == 0
        assert conn.execute(text('SELECT COUNT(*) FROM spool_usage')).scalar_one() == 0
        assert conn.execute(text('SELECT COUNT(*) FROM tray_confirmations')).scalar_one() == 1


@pytest.mark.parametrize('delegated', [[], [10, 9]])
def test_startup_logs_effective_external_printer_ids(monkeypatch, caplog, delegated):
    import logging
    from core.config import settings
    from core.interfaces.filament_ownership import log_external_filament_accounting
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', delegated)
    for process in ('uvicorn.error', 'moonraker_monitor', 'mqtt_monitor', 'prusalink_monitor', 'elegoo_monitor', 'anycubic_lan_monitor'):
        with caplog.at_level('INFO', logger=process):
            log_external_filament_accounting(logging.getLogger(process))
    assert len(caplog.records) == 6
    assert all(str(sorted(delegated)) in record.message for record in caplog.records)


@pytest.mark.parametrize('prior, expected_reason', [
    ('print_usage', 'attempt_usage_without_archive'),
    ('legacy_job_usage', 'legacy_usage_unattributed'),
    ('job_usage', 'job_usage_unattributed'),
])
def test_delegated_archive_preserves_unattributed_prior_usage(archive_case, monkeypatch, prior, expected_reason):
    from core.config import settings
    from core.filament_ledger import prior_attempt_usage
    from core.print_attempts import sqlalchemy_runner
    archive, engine = archive_case
    with engine.begin() as conn:
        conn.execute(text('UPDATE spools SET remaining_weight_g=960,spoolman_spool_id=123 WHERE id=7099'))
        conn.execute(text("INSERT INTO spool_usage(spool_id,job_id,print_job_id,weight_used_g,used_at,notes) "
                          "VALUES(7099,7099,:print_job_id,40,'2026-10-11 13:00:00',:notes)"),
                     {'print_job_id': 7099 if prior == 'print_usage' else None,
                      'notes': 'auto:operator_complete' if prior == 'job_usage' else 'Auto-deducted on job #7099 complete (Synthetic)'})
        if prior == 'job_usage':
            conn.execute(text("INSERT INTO system_config(key,value) VALUES('quota_attempt:filament:job:7099:0',:value)"),
                         {'value': json.dumps({'source': 'operator_complete', 'print_job_id': None})})
        assert prior_attempt_usage(sqlalchemy_runner(conn), 7099, 7099) == prior
        before_usage = conn.execute(text('SELECT * FROM spool_usage ORDER BY id')).all()
        before_claims = conn.execute(text("SELECT key,value FROM system_config WHERE key LIKE 'quota_attempt:filament:%' ORDER BY key")).all()
        before_outbox = conn.execute(text('SELECT * FROM filament_delivery_outbox ORDER BY id')).all()
    monkeypatch.setattr(settings, 'external_filament_accounting_printers', [9])
    assert archive.create_print_archive(7099, 9, True) is not None
    assert _accounting(engine) == ('unresolved', expected_reason)
    assert _balances(engine) == [960, 1000]
    with engine.connect() as conn:
        assert conn.execute(text('SELECT * FROM spool_usage ORDER BY id')).all() == before_usage
        assert conn.execute(text("SELECT key,value FROM system_config WHERE key LIKE 'quota_attempt:filament:%' ORDER BY key")).all() == before_claims
        assert conn.execute(text('SELECT * FROM filament_delivery_outbox ORDER BY id')).all() == before_outbox
        assert conn.execute(text('SELECT consumption_json FROM print_archives WHERE print_job_id=7099')).scalar_one() is None


def test_startup_entrypoints_log_external_filament_accounting():
    import ast
    from pathlib import Path
    backend = Path(__file__).resolve().parents[2] / 'backend'
    targets = ['core/app.py'] + [
        f'modules/printers/monitors/{name}_monitor.py'
        for name in ('moonraker', 'mqtt', 'prusalink', 'elegoo', 'anycubic_lan')
    ]
    for target in targets:
        tree = ast.parse((backend / target).read_text())
        if target == 'core/app.py':
            entrypoints = [node for node in ast.walk(tree)
                           if isinstance(node, ast.AsyncFunctionDef) and node.name == 'lifespan']
        else:
            entrypoints = [node for node in tree.body
                           if isinstance(node, ast.If) and '__main__' in ast.unparse(node.test)]
        assert len(entrypoints) == 1, target
        startup = entrypoints[0]
        if target.endswith(('mqtt_monitor.py', 'anycubic_lan_monitor.py')):
            assert any(isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                       and isinstance(statement.value.func, ast.Name) and statement.value.func.id == 'main'
                       for statement in startup.body), target
            startup = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        calls = [statement.value for statement in startup.body
                 if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                 and isinstance(statement.value.func, ast.Name)
                 and statement.value.func.id == 'log_external_filament_accounting']
        assert len(calls) == 1, f'{target}: log the effective policy directly on startup'
