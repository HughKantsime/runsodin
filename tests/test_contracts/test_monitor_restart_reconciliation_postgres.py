"""The restart reconciliation contracts on PostgreSQL, where the daemons' DBAPI adapter differs."""
import json
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import _file_facts
from tests.test_contracts.test_monitor_restart_reconciliation import (  # noqa: F401
    delivered, dispatched_moonraker, generic, manual_bambu, moonraker, owned_bambu, running_bambu,
    test_bambu_first_seen_pause_alerts_once_on_its_own_row,
    test_durable_terminal_intent_is_claimed_once,
    test_education_row_ended_in_downtime_keeps_its_policy_path,
    test_education_sensitive_end_stays_untouched,
    test_elegoo_end_after_an_undecided_first_frame_still_closes_the_row,
    test_elegoo_first_look_lasts_until_a_known_state,
    test_end_during_a_database_outage_stays_pending,
    test_failed_end_does_not_fail_connect_and_retries,
    test_failing_recovery_backs_off_instead_of_retrying_every_report,
    test_generic_education_named_start_closes_nothing,
    test_generic_education_row_is_never_resumed_or_closed,
    test_generic_first_look_past_the_pause_allowance_is_refused,
    test_generic_pause_after_a_restart_still_reaches_the_attention_lane,
    test_generic_print_paused_across_a_restart_ends_on_its_own_row,
    test_generic_reprint_of_a_retained_dispatch_file_cannot_block_the_printer,
    test_generic_terminal_first_seen_closes_the_unique_row,
    test_generic_unrelated_start_closes_a_reserved_row_with_no_claim,
    test_generic_unrelated_start_under_a_held_claim_leaves_an_unknown_row,
    test_malformed_ams_report_does_not_cost_a_print_its_end,
    test_moonraker_education_named_start_closes_nothing,
    test_moonraker_first_seen_pause_alerts_once_on_its_own_row,
    test_moonraker_live_end_is_not_bounded_by_the_print_duration,
    test_moonraker_live_end_lost_to_a_transient_error_retries_too,
    test_moonraker_reprint_of_a_retained_dispatch_file_cannot_block_the_printer,
    test_moonraker_unproven_identity_changes_nothing,
    test_moonraker_unrelated_start_closes_a_reserved_row_with_no_claim,
    test_moonraker_unrelated_start_never_closes_an_education_owned_row,
    test_moonraker_unrelated_start_under_a_held_claim_leaves_an_unknown_row,
    test_prusalink_first_look_lasts_until_a_state_that_decides,
    test_prusalink_matching_job_id_does_not_resume_a_reserved_row,
    test_reconciled_end_cannot_charge_a_single_tray_it_did_not_watch,
    test_reconciled_unreserved_end_marks_its_tray_evidence_incomplete,
    test_retained_report_is_logged_once_per_interval_without_its_payload,
    test_watched_cancel_to_idle_measures_filament_in_its_own_packet,
    test_watched_end_keeps_its_tray_evidence,
    test_dispatched_print_ended_in_downtime_is_closed_once,
    test_end_lost_to_a_transient_database_error_retries_on_this_connection,
    test_failed_commit_does_not_keep_the_ended_row_tracked,
    test_failed_recovery_retries_on_the_next_report,
    test_first_packet_of_a_print_under_way_is_not_its_start_evidence,
    test_generic_first_look_at_a_continuing_print_resumes_within_the_pause_allowance,
    test_generic_reprint_days_later_gets_its_own_row,
    test_generic_watched_start_is_new_however_recent_the_stale_row,
    test_generic_different_print_does_not_adopt_the_old_row,
    test_generic_dispatched_attempt_keeps_the_receipt_rules,
    test_generic_print_continuing_through_a_restart_keeps_its_row,
    test_generic_unnamed_print_does_not_resume_or_close_anything,
    test_loadout_changed_in_downtime_still_proves_the_end,
    test_moonraker_dispatched_end_follows_the_receipt_rules,
    test_moonraker_dispatched_end_retires_the_claim_and_dispatches_again,
    test_moonraker_bounded_end_still_retires_a_dispatched_claim,
    test_moonraker_end_lost_to_a_transient_error_retries_and_the_next_print_is_new,
    test_moonraker_end_without_history_closes_its_row_once,
    test_moonraker_failed_lookup_retries_on_the_next_poll,
    test_moonraker_history_supplies_truth_long_after_the_end,
    test_moonraker_unrelated_start_leaves_a_receipt_bound_row_and_its_claim,
    test_no_status_after_the_wait_leaves_a_live_print_alone,
    test_older_duplicates_stay_stale_and_cannot_take_the_outcome,
    test_prusalink_next_job_id_is_a_new_print,
    test_prusalink_real_status_shape_keeps_one_row_through_a_restart,
    test_recovered_end_uses_the_first_start_filament_evidence,
    test_refused_identity_keeps_the_fence,
    test_retained_end_of_a_dispatched_print_keeps_the_fence_until_a_live_report,
    test_retained_first_end_is_not_proof_until_a_live_report,
    test_unproven_end_keeps_the_dispatch_fence,
    test_unreserved_end_measures_filament_against_its_first_start,
    test_unreserved_print_ended_in_downtime_maps_like_a_watched_end,
    test_watched_end_still_measures_filament,
)

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'
TEMPLATE = 'reconciliation_template'


def _docker(*args, timeout=60):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope='module')
def pg_server():
    if not shutil.which('docker') or _docker('info', timeout=20).returncode:
        pytest.skip('Docker unavailable')
    from core.schema import bootstrap_database
    name = 'reconciliation_' + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(24)
    try:
        started = _docker('run', '-d', '--name', name, '-p', '127.0.0.1::5432', '--tmpfs',
                          '/var/lib/postgresql/data:rw,size=512m', '-e', 'POSTGRES_PASSWORD=' + password, IMAGE, timeout=120)
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 180
        while _docker('exec', name, 'pg_isready', '-U', 'postgres', '-h', '127.0.0.1', timeout=60).returncode:
            assert time.monotonic() < deadline, 'owned PostgreSQL not ready'
            time.sleep(0.3)
        port = _docker('port', name, '5432/tcp').stdout.strip().rsplit(':', 1)[-1]
        base = f'postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/'
        admin = create_engine(base + 'postgres', isolation_level='AUTOCOMMIT')
        for _ in range(50):
            try:
                admin.connect().close()
                break
            except Exception:
                time.sleep(0.3)
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE {TEMPLATE}'))
        seeded = create_engine(base + TEMPLATE)
        bootstrap_database(seeded, ROOT / 'backend')
        with seeded.begin() as conn:
            conn.execute(text("INSERT INTO groups (id,name,is_org) VALUES (1,'school',TRUE),(2,'other',TRUE)"))
            conn.execute(text("INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) VALUES "
                              "(1,'student','student@example.test','fixture','viewer',TRUE,1),"
                              "(3,'admin','admin@example.test','fixture','admin',TRUE,1)"))
            # The one Education submission the SQLite review_db fixture also carries.
            conn.execute(text("INSERT INTO education_cost_centers (id,org_id,name_key,code_key,display_name,code,state,revision,created_by) "
                              "VALUES (7,1,'engineering','eng','Engineering','ENG','active',1,3)"))
            conn.execute(text("INSERT INTO education_upload_operations (operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,expected_bytes,expected_hash,reservation_released_at) "
                              "VALUES ('upload-1',1,1,'committed',100,100,100,'digest',CURRENT_TIMESTAMP)"))
            conn.execute(text("INSERT INTO print_files (id,filename,original_filename,project_name,stored_path,org_id,created_by,storage_bytes,blob_state,compatibility_facts_json) "
                              "VALUES (11,'upload-1.3mf','gear.3mf','Class Gear','/opaque/upload-1.3mf',1,1,100,'present',:facts)"),
                         {'facts': json.dumps(_file_facts(), sort_keys=True)})
            conn.execute(text("INSERT INTO models (id,name,print_file_id,category,org_id) VALUES (12,'Class Gear',11,'Education Submission',1)"))
            conn.execute(text('UPDATE print_files SET model_id=12 WHERE id=11'))
            conn.execute(text("INSERT INTO jobs (id,model_id,item_name,status,submitted_by,charged_to_user_id,charged_to_org_id) VALUES (13,12,'Class Gear','submitted',1,1,1)"))
            conn.execute(text("INSERT INTO education_submissions (id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status,lifecycle_revision) "
                              "VALUES (14,1,'upload-1',13,11,12,7,1,'submitted',1)"))
        seeded.dispose()
        yield base, admin
        admin.dispose()
    finally:
        _docker('rm', '-f', name)


@pytest.fixture
def review_db(pg_server, monkeypatch):
    """Same name as the SQLite fixture, so the imported contracts run here unchanged."""
    from core import db_compat
    # The daemons' SQL fragments follow the process's configured database, not the engine.
    monkeypatch.setattr(db_compat, 'IS_SQLITE', False)
    monkeypatch.setattr(db_compat, 'IS_POSTGRES', True)
    base, admin = pg_server
    name = 'case_' + uuid.uuid4().hex[:12]
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE {name} TEMPLATE {TEMPLATE}'))
    engine = create_engine(base + name, pool_size=10)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE {name} WITH (FORCE)'))


@pytest.fixture
def creators(review_db, monkeypatch):
    """test_economic_creator_wiring.creators without its SQLite PRAGMA."""
    from core.audit_actor import clear_session, register_request_session
    from core.auth import create_access_token
    from core.db import get_db
    from core.errors import OdinError
    from core.middleware.audit_actor import AuditActorMiddleware
    from core.middleware.dry_run import dry_run_middleware
    from core.registry import registry
    from fastapi.responses import JSONResponse
    from modules.jobs.routes.jobs_crud import router as jobs
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    review_db.execute(text('UPDATE users SET quota_jobs=100 WHERE id=3'))
    review_db.commit()
    monkeypatch.setattr('modules.notifications.alert_dispatcher.deliver_staged_alert', lambda db, plan: None)
    app = FastAPI()
    app.middleware('http')(dry_run_middleware)
    app.add_middleware(AuditActorMiddleware)
    app.include_router(jobs, prefix='/api')

    def database(request: Request):
        with Session(review_db.get_bind(), autoflush=False) as db:
            register_request_session(db, request)
            try:
                yield db
            finally:
                clear_session(db)

    @app.exception_handler(OdinError)
    async def error(request, exc):
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())
    app.dependency_overrides[get_db] = database

    def auth(name='admin'):
        return {'Authorization': 'Bearer ' + create_access_token({'sub': name})}
    return TestClient(app, client=('198.51.100.7', 1)), auth, None, []
