"""Local, isolated settlement requests exercising the real ledger and transaction."""
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_archive_persistence_accounting_behavior import archive_case  # noqa: F401


@pytest.fixture
def settlement_case(archive_case, monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.archives.routes import router
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    archive, engine = archive_case
    with engine.begin() as db:
        db.execute(text("UPDATE print_jobs SET status='failed' WHERE id=7099"))
        # These fixture spools supplied the historical print, so predate its start.
        db.execute(text("UPDATE spools SET created_at='2026-10-01' WHERE id IN (7099,7100,7101)"))
    identifier = archive.create_print_archive(7099, 9, False)
    assert identifier
    user = dict(id=3, username='operator', role='operator', group_id=1, is_active=True)
    app = FastAPI()
    app.include_router(router)
    def session():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, engine, identifier, user


def post(case, **body):
    return case[0].post(f'/archives/{case[2]}/settle-filament', json=body)


def preview(case, **params):
    return case[0].get(f'/archives/{case[2]}/filament-settlement', params=params)


def query(case, sql, params=None):
    with case[1].connect() as db:
        return db.execute(text(sql), params or {}).all()


def write(case, sql, params=None):
    with case[1].begin() as db:
        db.execute(text(sql), params or {})


def manual(case, grams=10, spool=7099):
    from core.filament_ledger import record_manual_usage
    from core.print_attempts import sqlalchemy_runner
    with case[1].begin() as db:
        record_manual_usage(sqlalchemy_runner(db), spool, grams, job_id=7099, notes='test use')
    return query(case, 'SELECT MAX(id) FROM spool_usage')[0][0]


@pytest.mark.parametrize('reason', ['no_bound_spool', 'unknown_spool_balance', 'non_successful_attempt_quantity_unconfirmed', 'spool_change_pending', 'first_seen_mid_print', 'ended_while_unconfirmed'])
def test_settle_named_causes_and_repeat(settlement_case, reason):
    case = settlement_case
    write(case, 'UPDATE print_archives SET accounting_reason=:reason WHERE id=:id', dict(reason=reason, id=case[2]))
    if reason == 'unknown_spool_balance':
        write(case, 'UPDATE spools SET remaining_weight_g=NULL WHERE id=7099')
    data = preview(case).json()
    assert data['settleable'] and data['cause']['code'] == reason
    response = post(case, entries=[dict(spool_id=7099, grams=40)])
    assert response.status_code == 200, response.text
    assert response.json()['accounting_state'] == 'accounted'
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099')[0][0] == (None if reason == 'unknown_spool_balance' else 960)
    assert query(case, 'SELECT weight_used_g,notes FROM spool_usage')[0] == (40, f'auto:settlement: archive {case[2]}')
    assert post(case, entries=[dict(spool_id=7099, grams=40)]).json()['detail']['code'] == 'not_unresolved'
    detail = case[0].get(f'/archives/{case[2]}').json()
    assert detail['accounting_settled']['at']
    assert detail['consumption'][0]['grams_used'] == 40
    assert detail['filament_used_grams'] == 80  # original sliced estimate, unchanged


def test_unowned_single_install_and_authoritative_job_link(settlement_case):
    case = settlement_case
    write(case, 'UPDATE groups SET is_org=FALSE')
    write(case, "UPDATE print_archives SET ownership_state='unresolved',ownership_source='source_graph_changed',accounting_reason='archive_ownership_unresolved',org_id=NULL,job_id=NULL")
    assert preview(case).json()['settleable']
    assert post(case, entries=[dict(spool_id=7099, grams=40)]).status_code == 200
    assert query(case, 'SELECT job_id FROM spool_usage')[0][0] == 7099
    assert query(case, "SELECT source FROM print_filament_evidence WHERE print_job_id=7099 AND claim=1")


@pytest.mark.parametrize('reason', ['nothing_extruded', 'recorded_by_hand', 'spool_not_tracked', 'balance_already_updated'])
def test_zero_deduction_is_final(settlement_case, reason):
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    case = settlement_case
    assert post(case, no_deduction=reason).status_code == 200
    with case[1].begin() as db:
        run = sqlalchemy_runner(db)
        for source in ('archive', 'moonraker', 'operator_complete'):
            result = record_job_filament(run, 7099, source=source, print_job_id=7099, grams_by_spool={7099: 40})
            assert result.state == 'already_deducted'
        assert record_job_filament(run, 7099, source='operator_complete', grams_by_spool={7099: 40}).state == 'already_deducted'
    assert not query(case, 'SELECT * FROM spool_usage')


@pytest.mark.parametrize('counted,expected', [(True, 960), (False, 950)])
def test_manual_review_reduces_only_when_counted(settlement_case, counted, expected):
    case = settlement_case
    row = manual(case)
    assert preview(case).json()['manual_usage'][0]['usage_id'] == row
    refused = post(case, entries=[dict(spool_id=7099, grams=40)])
    assert refused.status_code == 409 and refused.json()['detail']['code'] == 'manual_usage_unreviewed'
    response = post(case, entries=[dict(spool_id=7099, grams=40)], **{'counted_usage_ids' if counted else 'separate_usage_ids': [row]})
    assert response.status_code == 200, response.text
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099')[0][0] == expected
    entry = json.loads(query(case, 'SELECT consumption_json FROM print_archives')[0][0])[0]
    assert entry['grams_used'] == 40 and entry['deducted_now_g'] == (30 if counted else 40)


def test_manual_overcount_missing_spool_and_counted_once(settlement_case):
    from core.filament_ledger import manual_rows_for_print
    from core.print_attempts import sqlalchemy_runner
    case = settlement_case
    row = manual(case, 50)
    assert post(case, entries=[dict(spool_id=7099, grams=40)], counted_usage_ids=[row]).json()['detail']['code'] == 'manual_usage_exceeds'
    assert post(case, entries=[dict(spool_id=7100, grams=60)], counted_usage_ids=[row]).status_code == 422
    assert post(case, entries=[dict(spool_id=7099, grams=60)], counted_usage_ids=[row]).status_code == 200
    with case[1].begin() as db:
        db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,started_at) VALUES(7102,9,7099,'2026-10-11 14:00:00')"))
        assert manual_rows_for_print(sqlalchemy_runner(db), 7099, 7102) == []


@pytest.mark.parametrize('kind', ['marker', 'print_usage', 'job_usage', 'legacy_job_usage'])
def test_accept_each_existing_usage_kind(settlement_case, kind):
    case = settlement_case
    evidence_kind = 'closed' if kind == 'marker' else 'usage' if kind == 'print_usage' else 'unattributed'
    write(case, "INSERT INTO print_filament_evidence(print_job_id,kind,claim,source,reason) VALUES(7099,:kind,:claim,'backfill','marker')",
          dict(kind=evidence_kind, claim=1 if evidence_kind in ('closed', 'usage') else None))
    data = preview(case).json()
    assert data['prior_usage'] == ('marker' if evidence_kind in ('closed', 'usage') else 'job_usage')
    assert data['cause']['title'] == 'Filament was already deducted another way'
    assert post(case, entries=[dict(spool_id=7099, grams=40)]).json()['detail']['code'] == 'already_deducted'
    assert post(case, no_deduction='accept_existing').status_code == 200
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099')[0][0] == 1000


def test_shortfall_outbox_and_audit(settlement_case):
    case = settlement_case
    case[3]['_token_id'] = 42
    write(case, 'UPDATE spools SET remaining_weight_g=10,spoolman_spool_id=123 WHERE id=7099')
    response = post(case, entries=[dict(spool_id=7099, grams=40)])
    assert response.status_code == 200, response.text
    assert response.json()['accounting_state'] == 'reconciliation_required'
    assert response.json()['deductions'][0]['shortfall_g'] == 30
    assert query(case, 'SELECT grams_used,state FROM filament_delivery_outbox') == [(40, 'pending')]
    audit = json.loads(query(case, "SELECT details FROM audit_logs WHERE action='archive.filament_settled'")[0][0])
    assert audit['actor_token_id'] == 42 and audit['request']['deduct_despite_later_balance'] == []


@pytest.mark.parametrize('method', ['weighed', 'measured'])
def test_later_balance_requires_explicit_confirmation(settlement_case, method):
    from core.filament_ledger import record_weighed_spool, record_ams_measurement
    from core.print_attempts import sqlalchemy_runner
    case = settlement_case
    with case[1].begin() as db:
        run = sqlalchemy_runner(db)
        if method == 'weighed':
            record_weighed_spool(run, 7100, 900)
        else:
            record_ams_measurement(run, 7100, 900, 9)
    assert preview(case, spool_id=7100).json()['balance_set_after_print'][0]['how'] == method
    refusal = post(case, entries=[dict(spool_id=7100, grams=40)])
    assert refusal.status_code == 409 and refusal.json()['detail']['code'] == 'balance_set_after_print'
    assert post(case, entries=[dict(spool_id=7100, grams=40)], deduct_despite_later_balance=[7100]).status_code == 200


@pytest.mark.parametrize('change,status', [('foreign_spool', 404), ('invisible', 404), ('viewer', 403), ('token', 403), ('education', 409), ('import', 409), ('ownership', 409)])
def test_access_and_eligibility(settlement_case, change, status):
    case = settlement_case
    spool = 7099
    if change == 'foreign_spool': spool = 7101
    if change == 'invisible': case[3]['group_id'] = 2
    if change == 'viewer': case[3]['role'] = 'viewer'
    if change == 'token': case[3].update(_auth_kind='user_api_token', _token_scopes=['read:jobs'])
    if change in ('education', 'ownership', 'import'):
        case[3].update(role='admin', group_id=None, is_superadmin=True)
        clause = {'education': 'education_submission_id=-1', 'ownership': "ownership_state='unresolved'", 'import': "ownership_source='project_import',print_job_id=NULL"}[change]
        write(case, 'UPDATE print_archives SET ' + clause)
        assert not preview(case).json()['settleable']
    response = post(case, entries=[dict(spool_id=spool, grams=40)])
    assert response.status_code == status, response.text
    assert not query(case, 'SELECT * FROM spool_usage')


def test_external_owner_refuses(settlement_case, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, 'filament_ledger_printers', {9: 'test'})
    response = post(settlement_case, no_deduction='nothing_extruded')
    assert response.status_code == 409


@pytest.mark.parametrize('after_insert', [False, True], ids=['before-audit', 'after-audit'])
def test_audit_failure_rolls_back_everything(settlement_case, monkeypatch, after_insert):
    from modules.archives import filament_settlement
    case = settlement_case
    audit = filament_settlement.log_audit

    def fail(db, *args, **kwargs):
        if after_insert:
            audit(db, *args, **kwargs)
            db.flush()
        raise RuntimeError('audit unavailable')
    monkeypatch.setattr(filament_settlement, 'log_audit', fail)
    with pytest.raises(RuntimeError, match='audit unavailable'):
        post(case, entries=[dict(spool_id=7099, grams=40)])
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099')[0][0] == 1000
    assert not query(case, 'SELECT * FROM spool_usage')
    assert not query(case, "SELECT key FROM system_config WHERE key LIKE 'quota_attempt:filament:%'")
    assert query(case, 'SELECT accounting_state FROM print_archives')[0][0] == 'unresolved'
    assert not query(case, "SELECT id FROM audit_logs WHERE action='archive.filament_settled'")


@pytest.mark.parametrize('body', [dict(entries=[]), dict(entries=[dict(spool_id=7099, grams=-1)]), dict(entries=[dict(spool_id=7099, grams=10001)]), dict(entries=[dict(spool_id=7099, grams=1)] * 2), dict(extra=True), dict(no_deduction='invalid'), dict(no_deduction='nothing_extruded', entries=[dict(spool_id=7099, grams=1)])])
def test_invalid_body_never_mutates(settlement_case, body):
    assert post(settlement_case, **body).status_code == 422
    assert not query(settlement_case, 'SELECT * FROM spool_usage')


@pytest.mark.parametrize('before,after', [(None, 900), (900, 900), (500, 900)])
def test_every_later_weigh_is_detected(settlement_case, before, after):
    from core.filament_ledger import record_weighed_spool
    from core.print_attempts import sqlalchemy_runner
    case = settlement_case
    write(case, 'UPDATE spools SET remaining_weight_g=:before WHERE id=7099', {'before': before})
    with case[1].begin() as db:
        record_weighed_spool(sqlalchemy_runner(db), 7099, after)
    assert preview(case).json()['balance_set_after_print'][0]['how'] == 'weighed'
    assert post(case, entries=[dict(spool_id=7099, grams=20)]).json()['detail']['code'] == 'balance_set_after_print'


def test_positive_fractional_remainder_is_deducted(settlement_case):
    case = settlement_case
    row = manual(case, 10)
    response = post(case, entries=[dict(spool_id=7099, grams=10.03)], counted_usage_ids=[row])
    assert response.status_code == 200
    assert response.json()['deductions'][0]['grams'] == pytest.approx(.03)


def test_settled_cost_uses_entry_instead_of_net(settlement_case):
    from modules.jobs.cost_actual import load_attempts, cost_actual
    from tests.test_contracts.test_cost_actual import PRICING
    case = settlement_case
    snapshot = json.loads(query(case, 'SELECT spool_snapshot FROM print_jobs WHERE id=7099')[0][0])
    snapshot['material_plan']['allocations'] = [dict(spool_id=7099, purchase_inputs=dict(price='20', net_weight_g='1000', state='recorded'))]
    write(case, 'UPDATE print_jobs SET spool_snapshot=:snapshot WHERE id=7099', {'snapshot': json.dumps(snapshot)})
    row = manual(case, 10)
    assert post(case, entries=[dict(spool_id=7099, grams=40)], counted_usage_ids=[row]).status_code == 200
    with Session(case[1]) as db:
        attempts = load_attempts(db, [7099], case[3])[7099]
        assert attempts[0]['usage'][0]['grams'] == 40
        assert cost_actual(attempts, PRICING)['material']['amount'] == .8


def test_no_job_and_snapshot_only_suggestions(settlement_case):
    case = settlement_case
    write(case, 'UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=7099')
    write(case, 'UPDATE filament_slots SET assigned_spool_id=7101 WHERE printer_id=9 AND slot_number=1')
    data = preview(case).json()
    assert data['job_id'] is None and data['manual_usage'] == []
    assert {r['spool_id'] for r in data['suggested']} == {7099, 7100}
    assert post(case, no_deduction='recorded_by_hand').status_code == 200


def test_hidden_spools_are_omitted_from_preview(settlement_case):
    case = settlement_case
    write(case, 'UPDATE spools SET org_id=2 WHERE id=7099')
    assert {r['spool_id'] for r in preview(case).json()['suggested']} == {7100}


def test_settle_then_real_monitor_complete_and_archive_replay(settlement_case, monkeypatch):
    from modules.printers.monitors.moonraker_monitor import MoonrakerMonitor
    from modules.jobs.routes import jobs_lifecycle
    from modules.archives import archive
    from types import SimpleNamespace
    case = settlement_case
    assert post(case, entries=[dict(spool_id=7099, grams=40)]).status_code == 200
    write(case, 'UPDATE print_files SET filaments_json=NULL WHERE id=7099')
    write(case, "UPDATE spools SET location_printer_id=9,status='active' WHERE id=7099")
    fake = SimpleNamespace(printer_id=9, _current_job_db_id=7099)
    with case[1].begin() as db:
        outcome = MoonrakerMonitor._auto_deduct_filament(fake, db, 7099, observed_grams=40)
        assert outcome.state == 'already_deducted'
    case[0].app.include_router(jobs_lifecycle.router)
    write(case, "UPDATE jobs SET status='failed',quantity=1,hold=0 WHERE id=7099")
    assert case[0].post('/jobs/7099/complete').status_code == 200
    assert archive.create_print_archive(7099, 9, True) == case[2]
    assert query(case, 'SELECT COUNT(*) FROM spool_usage')[0][0] == 1
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099')[0][0] == 960


def test_stale_tray_confirmation_cannot_overwrite_settlement(settlement_case, monkeypatch):
    from modules.inventory.routes.filament_slots import confirm_tray_identity, ConfirmTrayIdentity
    from modules.archives.filament_settlement import Settlement, settle
    case = settlement_case
    state_id = 'a' * 32
    write(case, 'UPDATE filament_slots SET tray_observation=:state WHERE printer_id=9 AND slot_number=1',
          {'state': json.dumps(dict(id=state_id, pending=True, hold_id=state_id, observed={'type': 'PLA'}))})
    write(case, "UPDATE spools SET status='active' WHERE id=7099")
    write(case, "UPDATE print_archives SET accounting_reason='spool_change_pending',consumption_json=:consumption",
          {'consumption': json.dumps([dict(spool_id=None, slot_number=1, identity_state=state_id, grams_used=40)])})
    with Session(case[1]) as db:
        execute = db.execute
        def interleave(statement, *args, **kwargs):
            if str(statement).startswith('SELECT a.id,a.print_job_id,a.job_id,a.status,a.consumption_json'):
                rows = execute(statement, *args, **kwargs).all()
                # Candidate is stale: the independent settlement has completed before
                # confirmation's archive lock. Same transaction gives a deterministic schedule.
                settle(db, case[2], Settlement(no_deduction='nothing_extruded'), case[3])
                class Result:
                    def all(self): return rows
                return Result()
            return execute(statement, *args, **kwargs)
        monkeypatch.setattr(db, 'execute', interleave)
        assert confirm_tray_identity(9, 1, ConfirmTrayIdentity(observed_state=state_id, decision='same'), current_user=case[3], db=db)['success']
    assert query(case, 'SELECT accounting_reason FROM print_archives')[0][0] == 'operator_settled_no_deduction'
    assert not query(case, 'SELECT * FROM spool_usage')
    assert json.loads(query(case, 'SELECT consumption_json FROM print_archives')[0][0])[0]['settled_at']


def test_busy_stock_rolls_back_and_gives_retry(settlement_case, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from modules.archives.routes import filament_settlement as route
    case = settlement_case
    def busy(db, *args):
        db.execute(text("UPDATE print_archives SET accounting_reason='temporary'"))
        cause = RuntimeError('busy')
        cause.sqlstate = '55P03'
        raise OperationalError('lock', {}, cause)
    monkeypatch.setattr(route, 'settle', busy)
    response = post(case, no_deduction='nothing_extruded')
    assert response.status_code == 409 and response.json()['detail']['code'] == 'busy' and 'Refresh and try again' in response.json()['detail']['message']
    assert query(case, 'SELECT accounting_reason FROM print_archives')[0][0] != 'temporary'
