"""Pre-dispatch filament check: per-slot math, gate decisions, wiring and the superadmin setting."""
import inspect
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from modules.printers import filament_check as fc
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401  (fixture)

DDL = (
    'CREATE TABLE system_config(key TEXT PRIMARY KEY, value TEXT)',
    'CREATE TABLE models(id INTEGER PRIMARY KEY, color_requirements TEXT)',
    'CREATE TABLE jobs(id INTEGER PRIMARY KEY, quantity INTEGER, model_id INTEGER, charged_to_org_id INTEGER)',
    'CREATE TABLE printers(id INTEGER PRIMARY KEY, api_type TEXT, org_id INTEGER)',
    'CREATE TABLE spools(id INTEGER PRIMARY KEY, remaining_weight_g REAL)',
    'CREATE TABLE filament_slots(id INTEGER PRIMARY KEY, printer_id INTEGER, slot_number INTEGER, '
    'filament_type TEXT, color TEXT, tray_observation TEXT, assigned_spool_id INTEGER)',
)
BLACK_PLA = [{'slot': 1, 'type': 'PLA', 'color': '#000000', 'used_grams': 50}]


@pytest.fixture
def engine():
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    with engine.begin() as conn:
        for statement in DDL:
            conn.execute(text(statement))
    return engine


def seed(engine, *, quantity=1, model=None, slots=(), spools=None, api='bambu', mode=None, margin=None):
    with engine.begin() as conn:
        conn.execute(text('INSERT INTO models(id,color_requirements) VALUES(1,:r)'),
                     {'r': None if model is None else json.dumps(model)})
        conn.execute(text('INSERT INTO jobs(id,quantity,model_id) VALUES(1,:q,1)'), {'q': quantity})
        conn.execute(text('INSERT INTO printers(id,api_type) VALUES(1,:a)'), {'a': api})
        for number, kind, color, spool in slots:
            conn.execute(text('INSERT INTO filament_slots(printer_id,slot_number,filament_type,color,assigned_spool_id) '
                              'VALUES(1,:n,:t,:c,:s)'), {'n': number, 't': kind, 'c': color, 's': spool})
        for spool_id, remaining in (spools or {}).items():
            conn.execute(text('INSERT INTO spools(id,remaining_weight_g) VALUES(:i,:r)'), {'i': spool_id, 'r': remaining})
        for key, value in ((fc.MODE_KEY, mode), (fc.MARGIN_KEY, margin)):
            if value is not None:
                conn.execute(text('INSERT INTO system_config(key,value) VALUES(:k,:v)'), {'k': key, 'v': value})


def job(filaments=None, mapping=None, **extra):
    return {'id': 1, 'printer_id': 1, 'model_id': 1, 'ams_mapping': mapping,
            'filaments_json': None if filaments is None else json.dumps(filaments),
            'education_submission_id': None, 'quantity_on_bed': 12, **extra}


def evaluate(engine, the_job, **kwargs):
    with engine.connect() as conn:
        return fc.evaluate(conn, job=the_job, printer_id=1, **kwargs)


AMS = ((1, 'PLA', 'white', 1), (2, 'PLA', 'black', 2))


# ---------------------------------------------------------------- settings

def test_defaults_are_warn_and_ten_percent(engine):
    with engine.connect() as conn:
        assert fc.read_settings(conn) == {'mode': 'warn', 'margin_pct': 10.0}


@pytest.mark.parametrize('mode,margin', [('banana', 'abc'), ('', '250'), ('HOLD', '-1')])
def test_invalid_stored_values_fall_back_without_failing(engine, mode, margin):
    seed(engine, mode=mode, margin=margin)
    with engine.connect() as conn:
        settings = fc.read_settings(conn)
    assert settings['mode'] == ('hold' if mode == 'HOLD' else 'warn')
    assert settings['margin_pct'] == 10.0


# --------------------------------------------------------------------- math

@pytest.mark.parametrize('remaining,status', [(54.0, 'short'), (55.0, 'enough'), (56.0, 'enough')])
def test_needed_is_one_print_plus_margin_whatever_the_quantity_or_pieces_per_bed(engine, remaining, status):
    # A dispatch prints one plate; quantity (2, a repeat count) and quantity_on_bed (12) never multiply.
    seed(engine, quantity=2, slots=AMS, spools={1: 900, 2: remaining})
    result = evaluate(engine, job(BLACK_PLA, mapping=[1]))
    item = result['slots'][0]
    assert (item['slot'], item['print_g'], item['needed_g'], item['status']) == (2, 50.0, 55.0, status)
    assert result['prints'] == 1 and result['overall'] == status


def test_short_message_is_plain_words_with_the_numbers(engine):
    seed(engine, slots=AMS, spools={1: 900, 2: 40})
    item = evaluate(engine, job(BLACK_PLA, mapping=[1]))['slots'][0]
    assert item['message'] == 'Slot 2 (black PLA) has about 40 g left; this print needs about 55 g'


@pytest.mark.parametrize('remaining,status,message', [
    (40, 'short', 'Slot 2 (black PLA) does not have enough filament; this print needs about 55 g'),
    (900, 'enough', 'Slot 2 (black PLA) has enough filament for this print'),
])
def test_shared_printer_callers_get_the_verdict_without_spool_id_or_weight(engine, remaining, status, message):
    seed(engine, slots=AMS, spools={1: 900, 2: remaining})
    full = evaluate(engine, job(BLACK_PLA, mapping=[1]))
    hidden = fc.hide_spool_details(full)
    item = hidden['slots'][0]
    assert (item['status'], item['spool_id'], item['remaining_g'], item['message']) == (status, None, None, message)
    assert hidden['overall'] == full['overall'] and hidden['spool_details_hidden'] is True
    assert hidden['warnings'] == ([message] if status == 'short' else [])
    assert str(remaining) not in json.dumps(hidden)
    assert full['slots'][0]['spool_id'] == 2  # the owner's result is untouched


def test_the_job_route_hides_spool_details_unless_the_caller_owns_the_printer():
    from modules.printers import routes_filament_check
    source = inspect.getsource(routes_filament_check.get_job_filament_check)
    assert "owner = printer is not None and check_org_access(current_user, printer['org_id'])" in source
    assert 'return result if owner else filament_check.hide_spool_details(result)' in source


def test_margin_setting_applies(engine):
    seed(engine, slots=AMS, spools={1: 900, 2: 52}, margin='0')
    item = evaluate(engine, job(BLACK_PLA, mapping=[1]))['slots'][0]
    assert item['needed_g'] == 50.0 and item['status'] == 'enough'


def test_two_sources_on_one_slot_are_summed(engine):
    seed(engine, slots=AMS, spools={1: 900, 2: 54})
    two = [{'slot': 1, 'type': 'PLA', 'used_grams': 30}, {'slot': 2, 'type': 'PLA', 'used_grams': 20}]
    result = evaluate(engine, job(two, mapping=[1, 1]))
    assert len(result['slots']) == 1
    assert (result['slots'][0]['source_slots'], result['slots'][0]['needed_g'], result['slots'][0]['status']) == ([1, 2], 55.0, 'short')


def test_unmapped_sources_of_a_multi_filament_file_are_not_checked(engine):
    seed(engine, slots=AMS, spools={1: 900, 2: 900})
    result = evaluate(engine, job(BLACK_PLA + [{'slot': 2, 'type': 'PLA', 'used_grams': 5}], mapping=[-1, 0]))
    assert [(s['slot'], s['source_slots']) for s in result['slots']] == [(1, [2])]


# ----------------------------------------------------------------- unknown

@pytest.mark.parametrize('case,reason', [
    ('no_estimate', 'no_estimate'), ('no_spool', 'no_spool'),
    ('no_remaining', 'no_remaining_weight'), ('quantity', 'quantity_unknown'),
])
def test_unknown_is_never_guessed(engine, case, reason):
    slots = ((1, 'PLA', 'white', None), (2, 'PLA', 'black', None if case == 'no_spool' else 2))
    seed(engine, quantity=None if case == 'quantity' else 1, slots=slots,
         spools={} if case == 'no_spool' else {2: None if case == 'no_remaining' else 5})
    filaments = [{'slot': 1, 'type': 'PLA'}] if case == 'no_estimate' else BLACK_PLA
    result = evaluate(engine, job(filaments, mapping=[1]))
    item = result['slots'][0]
    assert (item['status'], item['reason']) == ('unknown', reason) and result['overall'] == 'unknown'
    assert "can't check" in item['message']


def test_model_grams_are_a_fallback_only_for_one_mapped_source(engine):
    model = {'color1': {'color': 'black', 'grams': 50}}
    seed(engine, model=model, slots=AMS, spools={1: 900, 2: 40})
    assert evaluate(engine, job(None, mapping=[1]))['slots'][0]['status'] == 'short'
    two = {'color1': {'grams': 10}, 'color2': {'grams': 10}}
    with engine.begin() as conn:
        conn.execute(text('UPDATE models SET color_requirements=:r'), {'r': json.dumps(two)})
    result = evaluate(engine, job(None, mapping=[0, 1]))
    assert {s['reason'] for s in result['slots']} == {'no_estimate'}  # ambiguous: refuse to guess


def test_pending_spool_change_is_unknown(engine):
    seed(engine, slots=AMS, spools={1: 900, 2: 5})
    with engine.begin() as conn:
        conn.execute(text("UPDATE filament_slots SET tray_observation='{\"pending\": true}' WHERE slot_number=2"))
    assert evaluate(engine, job(BLACK_PLA, mapping=[1]))['slots'][0]['reason'] == 'spool_change_pending'


# ------------------------------------------------- mapping-less printers

def test_single_material_printer_uses_its_one_loaded_slot(engine):
    seed(engine, api='moonraker', slots=((1, 'PLA', 'black', 1),), spools={1: 30})
    result = evaluate(engine, job([{'slot': 1, 'type': 'PLA', 'used_grams': 40}]))
    assert (result['slots'][0]['slot'], result['slots'][0]['status']) == (1, 'short')


def test_single_source_bambu_file_uses_the_derived_mapping(engine):
    seed(engine, slots=AMS, spools={1: 900, 2: 900})
    result = evaluate(engine, job([{'slot': 2, 'type': 'PLA', 'used_grams': 10}]))  # source index 1 -> tray index 1 -> slot 2
    assert (result['slots'][0]['slot'], result['overall']) == (2, 'enough')


def test_several_filaments_without_a_mapping_are_unknown(engine):
    seed(engine, api='moonraker', slots=((1, 'PLA', 'black', 1),), spools={1: 1}, mode='hold')
    result = evaluate(engine, job(BLACK_PLA + [{'slot': 2, 'type': 'PLA', 'used_grams': 5}]))
    assert result['slots'][0]['reason'] == 'no_mapping'
    assert fc.dispatch_gate(engine, job(BLACK_PLA + [{'slot': 2, 'type': 'PLA', 'used_grams': 5}])).refusal is None


# ---------------------------------------------------------------- the gate

@pytest.fixture
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(fc, '_record_audit', lambda engine, action, the_job, result: calls.append(('audit', action)))
    monkeypatch.setattr(fc, '_report_hold', lambda the_job, refusal, detail: calls.append(('hold', refusal)))
    monkeypatch.setattr(fc, '_raise_alert', lambda engine, the_job, result, message: calls.append(('alert', message)))
    return calls


def test_warn_proceeds_with_a_warning_and_announces_it_after_admission(engine, recorded):
    seed(engine, slots=AMS, spools={1: 900, 2: 40}, mode='warn')
    outcome = fc.dispatch_gate(engine, job(BLACK_PLA, mapping=[1]), [1])
    assert outcome.refusal is None
    assert outcome.warning == 'Filament warning: Slot 2 (black PLA) has about 40 g left; this print needs about 55 g'
    assert recorded == []  # nothing is raised until admission has succeeded
    outcome.announce(engine, job(BLACK_PLA, mapping=[1]))
    assert [kind for kind, _ in recorded] == ['audit', 'alert'] and recorded[0][1] == 'job.filament_warning'


def test_hold_refuses_with_filament_short_numbers_and_is_audited(engine, recorded):
    seed(engine, slots=AMS, spools={1: 900, 2: 40}, mode='hold')
    outcome = fc.dispatch_gate(engine, job(BLACK_PLA, mapping=[1]), [1])
    assert outcome.warning is None
    assert outcome.refusal.startswith('filament_short: Slot 2 (black PLA) has about 40 g left; this print needs about 55 g')
    assert [kind for kind, _ in recorded] == ['audit', 'hold'] and recorded[0][1] == 'job.filament_hold'


@pytest.mark.parametrize('mode', ['hold', 'warn'])
def test_a_job_on_another_organizations_shared_printer_gets_no_spool_weight_in_the_gate_text(engine, monkeypatch, mode):
    seed(engine, slots=AMS, spools={1: 900, 2: 40}, mode=mode)
    with engine.begin() as conn:
        conn.execute(text('UPDATE printers SET org_id = 7'))
        conn.execute(text('UPDATE jobs SET charged_to_org_id = 9'))
    seen = []
    monkeypatch.setattr(fc, '_record_audit', lambda engine, action, the_job, result: seen.append(result))
    monkeypatch.setattr(fc, '_report_hold', lambda the_job, refusal, detail: seen.append(detail))
    monkeypatch.setattr(fc, '_raise_alert', lambda engine, the_job, result, message: seen.extend([result, message]))
    outcome = fc.dispatch_gate(engine, job(BLACK_PLA, mapping=[1]), [1])
    outcome.announce(engine, job(BLACK_PLA, mapping=[1]))
    said = outcome.refusal or outcome.warning
    assert 'Slot 2 (black PLA) does not have enough filament; this print needs about 55 g' in said
    assert seen and '40' not in json.dumps([said, seen]) and '"spool_id": 2' not in json.dumps(seen)
    assert (outcome.refusal is not None) == (mode == 'hold')


def test_a_job_in_the_printers_own_organization_keeps_the_numbers(engine, recorded):
    seed(engine, slots=AMS, spools={1: 900, 2: 40}, mode='hold')
    with engine.begin() as conn:
        conn.execute(text('UPDATE printers SET org_id = 7'))
        conn.execute(text('UPDATE jobs SET charged_to_org_id = 7'))
    assert 'has about 40 g left' in fc.dispatch_gate(engine, job(BLACK_PLA, mapping=[1]), [1]).refusal


def test_an_unreadable_owner_counts_as_not_the_owner(engine):
    assert fc._job_owns_printer(engine, {'id': 1, 'printer_id': 1}) is False  # no such job row yet


def test_a_dispatch_with_no_ams_mapping_is_never_held_against_an_ams_slot(engine, recorded):
    # Sent from the external spool: the nearly empty AMS tray is not what feeds this print.
    seed(engine, slots=AMS, spools={1: 5, 2: 5}, mode='hold')
    outcome = fc.dispatch_gate(engine, job([{'slot': 2, 'type': 'PLA', 'used_grams': 50}]), None)
    assert outcome.refusal is None and outcome.warning is None and recorded == []


def test_unknown_never_holds_and_raises_nothing(engine, recorded):
    seed(engine, slots=((2, 'PLA', 'black', None),), mode='hold')
    outcome = fc.dispatch_gate(engine, job(BLACK_PLA, mapping=[1]), [1])
    assert (outcome.refusal, outcome.warning) == (None, None) and recorded == []


def test_enough_is_silent(engine, recorded):
    seed(engine, slots=AMS, spools={1: 900, 2: 500}, mode='hold')
    outcome = fc.dispatch_gate(engine, job(BLACK_PLA, mapping=[1]), [1])
    assert (outcome.refusal, outcome.warning) == (None, None) and recorded == []


def test_off_skips_the_check(engine, recorded):
    seed(engine, slots=AMS, spools={1: 900, 2: 1}, mode='off')
    outcome = fc.dispatch_gate(engine, job(BLACK_PLA, mapping=[1]), [1])
    assert outcome.result is None and outcome.refusal is None and recorded == []


def test_a_failing_check_never_blocks_dispatch(engine, recorded, monkeypatch):
    seed(engine, mode='hold')
    monkeypatch.setattr(fc, 'evaluate', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    assert fc.dispatch_gate(engine, job(BLACK_PLA, mapping=[1]), [1]).refusal is None


def test_education_submissions_follow_the_same_rule(engine, recorded):
    seed(engine, slots=AMS, spools={1: 900, 2: 40}, mode='hold')
    generic = fc.dispatch_gate(engine, job(BLACK_PLA, mapping=[1]), [1])
    education = fc.dispatch_gate(engine, job(BLACK_PLA, mapping=[1], education_submission_id=7,
                                             education_lifecycle_revision=3), [1])
    assert generic.refusal == education.refusal and generic.refusal.startswith('filament_short:')


def test_the_gate_runs_before_education_reservation_and_admission():
    from modules.printers import dispatch_orchestration
    source = inspect.getsource(dispatch_orchestration._dispatch_claimed)
    gate, reserve, admit_call, announce = (source.index(marker) for marker in (
        'dispatch_gate(engine, job', '_reserve_education_hardware_action', 'admission=admit(', 'filament.announce('))
    assert gate < reserve < admit_call < announce


# ------------------------------------------------------- setting route + RBAC

@pytest.fixture
def app(review_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.printers.routes_filament_check import router
    application = FastAPI()
    application.include_router(router, prefix='/api')

    def database():
        with Session(review_db.get_bind()) as db:
            yield db
    application.user = {'id': 3, 'role': 'admin', 'is_active': True, 'group_id': None}
    application.dependency_overrides[get_db] = database
    application.dependency_overrides[get_current_user] = lambda: application.user
    return application


def count(review_db, sql):
    with Session(review_db.get_bind()) as db:
        return db.execute(text(sql)).scalar_one()


def test_superadmin_reads_defaults_updates_and_the_change_is_audited(app, review_db):
    with TestClient(app) as client:
        assert client.get('/api/config/filament-check').json() == {'mode': 'warn', 'margin_pct': 10.0}
        response = client.put('/api/config/filament-check', json={'mode': 'hold', 'margin_pct': 15})
        assert response.status_code == 200 and response.json() == {'mode': 'hold', 'margin_pct': 15.0}
        assert client.get('/api/config/filament-check').json() == {'mode': 'hold', 'margin_pct': 15.0}
    assert count(review_db, "SELECT COUNT(*) FROM audit_logs WHERE action='filament_check.updated'") == 1


@pytest.mark.parametrize('body,status', [
    ({'mode': 'maybe'}, 422), ({'margin_pct': 101}, 422), ({'margin_pct': -1}, 422),
    ({'extra': 1}, 422), ({}, 400),
])
def test_invalid_updates_are_refused_without_a_write(app, review_db, body, status):
    with TestClient(app) as client:
        assert client.put('/api/config/filament-check', json=body).status_code == status
    assert count(review_db, "SELECT COUNT(*) FROM system_config WHERE key LIKE 'filament_check_%'") == 0
    assert count(review_db, "SELECT COUNT(*) FROM audit_logs WHERE action='filament_check.updated'") == 0


@pytest.mark.parametrize('user', [
    {'id': 5, 'role': 'admin', 'is_active': True, 'group_id': 9},      # organization admin
    {'id': 6, 'role': 'operator', 'is_active': True, 'group_id': None},
    {'id': 7, 'role': 'viewer', 'is_active': True, 'group_id': None},
])
def test_only_the_platform_superadmin_may_read_or_change_the_setting(app, review_db, user):
    app.user = user
    with TestClient(app) as client:
        assert client.get('/api/config/filament-check').status_code == 403
        assert client.put('/api/config/filament-check', json={'mode': 'off'}).status_code == 403
    assert count(review_db, "SELECT COUNT(*) FROM system_config WHERE key LIKE 'filament_check_%'") == 0


# ---------------------------------------------------------------- Codex sign-off 11-12
@pytest.mark.parametrize('plate_index, overall', [(None, 'short'), (0, 'short'), (1, 'unknown'), (3, 'unknown')])
def test_a_job_on_a_later_plate_is_never_judged_with_the_first_plates_grams(engine, recorded, plate_index, overall):
    """The stored estimate is the first plate's: plate 2 could need ten times as much, so a job
    that prints another plate is unknown (and never held), not 'enough' or 'short'."""
    seed(engine, slots=AMS, spools={1: 900, 2: 40}, mode='hold')
    the_job = job(BLACK_PLA, mapping=[1], plate_index=plate_index)
    result = evaluate(engine, the_job)
    assert result['overall'] == overall
    outcome = fc.dispatch_gate(engine, the_job, [1])
    if overall == 'unknown':
        assert result['slots'][0]['reason'] == 'other_plate' and 'first plate' in result['slots'][0]['message']
        assert outcome.refusal is None and outcome.warning is None
    else:
        assert outcome.refusal.startswith('filament_short')
