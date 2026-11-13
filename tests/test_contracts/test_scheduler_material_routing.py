"""Scheduler filament-material routing (spec: .claude/specs/filament-type-routing.md).

Unit tests drive modules.jobs.scheduler_materials directly; scheduler tests drive the real
Scheduler.run through the cert-D fleet fixture (printers 11 bambu/X1C and 12 moonraker/Voron);
the setting's RBAC, audit and end-to-end effect run through the real app (cert-G harness).
"""
import json

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_D_jobsB_scheduler import (  # noqa: F401
    add_job, colors, fleet, only_active, rows, run)
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_cert_G_harness import run_probe

from modules.jobs import scheduler_materials as sm


# ---------------- helpers ----------------

def load(db, printer, materials):
    db.execute(text('DELETE FROM filament_slots WHERE printer_id=:p'), {'p': printer})
    for slot, material in enumerate(materials, 1):
        db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(:p,:s,:m)"),
                   {'p': printer, 's': slot, 'm': material})
    db.commit()


def need(db, job, material):
    db.execute(text('UPDATE jobs SET filament_type=:m WHERE id=:id'), {'m': material, 'id': job})
    db.commit()


def file_needs(db, job, types):
    model = 800 + job
    db.execute(text("INSERT INTO models(id,name,org_id,build_time_hours) VALUES(:m,:n,1,1)"),
               {'m': model, 'n': f'File {job}'})
    db.execute(text("INSERT INTO print_files(filename,model_id,org_id,filaments_json) VALUES(:f,:m,1,:j)"),
               {'f': f'f{job}.3mf', 'm': model,
                'j': json.dumps([{'slot': i + 1, 'type': t} for i, t in enumerate(types)])})
    db.execute(text('UPDATE jobs SET model_id=:m WHERE id=:id'), {'m': model, 'id': job})
    db.commit()


def mode(db, value):
    sm.set_mode(db, value)
    db.commit()


def placed(db, job):
    return rows(db, [job])[job][:2]


# ---------------- derivation (unit) ----------------

def test_needed_from_file_filaments_distinct_case_insensitive():
    fj = json.dumps([{'slot': 1, 'type': 'PETG'}, {'slot': 2, 'type': 'pla'}, {'slot': 3, 'type': 'PETG'}])
    assert sm.needed_materials(fj) == ('PETG', 'PLA')


def test_needed_file_wins_over_job_field_and_job_field_is_the_fallback():
    fj = json.dumps([{'slot': 1, 'type': 'ABS'}])
    assert sm.needed_materials(fj, 'PETG') == ('ABS',)
    assert sm.needed_materials(None, 'PETG') == ('PETG',)
    assert sm.needed_materials('[]', 'PETG_CF') == ('PETG-CF',)


@pytest.mark.parametrize('fj,job_type', [(None, None), ('invalid', None), ('[]', None),
                                         (json.dumps([{'slot': 1, 'type': 'PLA Basic'}]), None),
                                         (None, 'OTHER'), (None, 'Unknown')])
def test_needed_unknown(fj, job_type):
    assert sm.needed_materials(fj, job_type) is None


def test_loaded_ignores_empty_slots_and_is_unknown_when_nothing_or_anything_unrecognised():
    assert sm.loaded_materials(['PLA', 'empty', None, 'PLA']) == ('PLA',)
    assert sm.loaded_materials(['PLA_CF', 'PETG']) == ('PLA-CF', 'PETG')
    assert sm.loaded_materials(['empty', 'empty']) is None
    assert sm.loaded_materials([]) is None
    assert sm.loaded_materials(['PLA', 'Unknown']) is None


def test_missing_materials_and_reason_text():
    assert sm.missing_materials(('PLA',), ('PLA', 'PETG')) == ('PETG',)
    assert sm.missing_materials(('PLA', 'PETG'), ('PETG',)) == ()
    assert sm.refusal_text(('PETG',)) == 'no printer has PETG loaded'
    assert sm.refusal_text(('PETG', 'TPU')) == 'no printer has PETG and TPU loaded'
    assert sm.refusal_text(('PETG',), 'Voron-12') == 'printer Voron-12 does not have PETG loaded'


# ---------------- one equivalence rule (the gate's) ----------------

@pytest.mark.parametrize('source,target', [('PLA', 'pla'), ('PLA', 'PETG'), ('PLA-CF', 'PLA_CF'), ('PLA', 'PLA-CF'),
                                           ('PLA Basic', 'PLA'), ('PLA', 'empty'), ('PLA', None)])
def test_scheduler_rule_equals_the_ams_gate_rule(source, target):
    from fastapi import HTTPException
    from core.print_payload import material_match, validate_mapping
    try:
        validate_mapping([{'source_slot': 1, 'target_slot': 1}], json.dumps([{'slot': 1, 'type': source}]),
                         {1: target}, 'bambu')
        gate_accepts = True
    except HTTPException:
        gate_accepts = False
    assert (material_match(source, target) is True) == gate_accepts


def test_scheduler_comparison_goes_through_the_gate_helper(monkeypatch):
    from core import print_payload
    assert sm.missing_materials(('PLA',), ('PETG',)) == ('PETG',)
    monkeypatch.setattr(print_payload, 'material_match', lambda a, b: True)
    assert sm.missing_materials(('PLA',), ('PETG',)) == ()


# ---------------- mode setting (unit) ----------------

def test_mode_defaults_to_require_saves_and_ignores_garbage(review_db):
    assert sm.get_mode(review_db) == 'require'
    mode(review_db, 'prefer')
    assert sm.get_mode(review_db) == 'prefer'
    with pytest.raises(ValueError):
        sm.set_mode(review_db, 'always')
    review_db.execute(text("UPDATE system_config SET value='\"bogus\"' WHERE key='scheduler_material_mode'"))
    review_db.commit()
    assert sm.get_mode(review_db) == 'require'
    review_db.execute(text("UPDATE system_config SET value='{\"mode\": \"off\"}' WHERE key='scheduler_material_mode'"))
    review_db.commit()
    assert sm.get_mode(review_db) == 'off'


# ---------------- scheduler: require (default) ----------------

def test_require_is_the_default_and_excludes_a_known_mismatch(fleet):
    assert sm.get_mode(fleet) == 'require'
    only_active(fleet, [11, 12])
    load(fleet, 11, ['PLA'])
    load(fleet, 12, ['PETG'])
    add_job(fleet, 901)
    need(fleet, 901, 'PETG')
    run(fleet)
    assert placed(fleet, 901) == ('scheduled', 12)


def test_unknown_never_excludes(fleet):
    only_active(fleet, [11, 12])
    load(fleet, 11, [])            # nothing recorded: unknown
    load(fleet, 12, ['PLA'])
    add_job(fleet, 902)            # PETG job: only the unknown printer can take it
    need(fleet, 902, 'PETG')
    add_job(fleet, 903)            # job with no material: may go anywhere
    run(fleet)
    assert placed(fleet, 902) == ('scheduled', 11)
    assert placed(fleet, 903)[0] == 'scheduled'


def test_unrecognised_loaded_label_is_unknown(fleet):
    only_active(fleet, [11])
    load(fleet, 11, ['PLA', 'Unknown'])
    add_job(fleet, 904)
    need(fleet, 904, 'PETG')
    run(fleet)
    assert placed(fleet, 904) == ('scheduled', 11)


def test_file_materials_win_over_the_job_field(fleet):
    only_active(fleet, [11, 12])
    load(fleet, 11, ['PLA'])
    load(fleet, 12, ['PETG'])
    add_job(fleet, 905)
    need(fleet, 905, 'PLA')
    file_needs(fleet, 905, ['PETG'])
    run(fleet)
    assert placed(fleet, 905) == ('scheduled', 12)


def test_multi_material_job_needs_every_material_on_one_printer(fleet):
    only_active(fleet, [11, 12])
    load(fleet, 11, ['PETG', 'PLA'])
    load(fleet, 12, ['PETG', 'TPU'])
    add_job(fleet, 906)
    file_needs(fleet, 906, ['PETG', 'TPU'])
    run(fleet)
    assert placed(fleet, 906) == ('scheduled', 12)


def test_no_candidate_keeps_the_job_pending_and_records_why(fleet):
    only_active(fleet, [11, 12])
    load(fleet, 11, ['PETG', 'PLA'])
    load(fleet, 12, ['PLA'])
    add_job(fleet, 907)
    file_needs(fleet, 907, ['PETG', 'TPU'])
    result = run(fleet)
    assert placed(fleet, 907) == ('pending', None)
    assert result.skipped_count == 1
    assert any('Could not schedule job 907' in e and 'no printer has TPU loaded' in e for e in result.errors)
    fleet.expire_all()
    notes = fleet.execute(text('SELECT notes FROM scheduler_runs ORDER BY id DESC LIMIT 1')).scalar()
    assert 'no printer has TPU loaded' in notes
    alert = fleet.execute(text("SELECT metadata_json, message FROM alerts WHERE job_id=907 AND alert_type='queue_skipped'")).one()
    meta = json.loads(alert[0]) if isinstance(alert[0], str) else alert[0]
    assert meta['reason'] == 'material_mismatch' and 'filament' in alert[1]


def test_capacity_not_material_is_blamed_when_a_matching_printer_is_full(fleet):
    only_active(fleet, [11, 12])
    load(fleet, 11, ['PLA'])
    load(fleet, 12, ['PETG'])
    add_job(fleet, 908, hours=24 * 3)      # beyond the 2-day horizon: no printer has room
    need(fleet, 908, 'PETG')
    result = run(fleet)
    assert placed(fleet, 908) == ('pending', None)
    assert not any('no printer has PETG loaded' in e for e in result.errors)


def test_pinned_job_stays_pending_with_the_reason_and_is_never_moved(fleet):
    only_active(fleet, [11, 12])
    load(fleet, 11, ['PLA'])
    load(fleet, 12, ['PETG'])
    add_job(fleet, 909, target_type='specific', target_filter='11')
    need(fleet, 909, 'PETG')
    result = run(fleet)
    assert placed(fleet, 909) == ('pending', None)
    assert any('printer X1C-11 does not have PETG loaded' in e for e in result.errors)
    mode(fleet, 'off')
    run(fleet)
    assert placed(fleet, 909) == ('scheduled', 11)


# ---------------- scheduler: prefer / off ----------------

@pytest.mark.parametrize('value,expected', [('off', 11), ('prefer', 12), ('require', 12)])
def test_material_outranks_colour_in_prefer_and_require_but_not_off(fleet, value, expected):
    only_active(fleet, [11, 12])
    load(fleet, 11, ['PLA'])
    load(fleet, 12, ['PETG'])
    colors(fleet, {11: 'red', 12: 'white'})
    add_job(fleet, 910, colors_required='red')
    need(fleet, 910, 'PETG')
    mode(fleet, value)
    run(fleet)
    assert placed(fleet, 910) == ('scheduled', expected)


def test_prefer_never_excludes(fleet):
    only_active(fleet, [11])
    load(fleet, 11, ['PLA'])
    add_job(fleet, 911)
    need(fleet, 911, 'PETG')
    mode(fleet, 'prefer')
    run(fleet)
    assert placed(fleet, 911) == ('scheduled', 11)


def test_off_is_todays_behaviour(fleet):
    only_active(fleet, [11])
    load(fleet, 11, ['PLA'])
    add_job(fleet, 912)
    need(fleet, 912, 'PETG')
    mode(fleet, 'off')
    result = run(fleet)
    assert placed(fleet, 912) == ('scheduled', 11)
    assert not result.errors


# ---------------- query counts stay flat ----------------

def test_material_queries_do_not_grow_with_jobs_or_printers(fleet):
    from sqlalchemy import event
    only_active(fleet, [11, 12])
    load(fleet, 11, ['PLA'])
    load(fleet, 12, ['PETG'])
    engine = fleet.get_bind()

    def counts(job_ids):
        seen = []

        def spy(conn, cursor, statement, *args):
            seen.append(statement)
        event.listen(engine, 'before_cursor_execute', spy)
        try:
            for job in job_ids:
                add_job(fleet, job)
                need(fleet, job, 'PETG')
            run(fleet)
        finally:
            event.remove(engine, 'before_cursor_execute', spy)
        return (sum('SELECT printer_id, filament_type FROM filament_slots' in s for s in seen),
                sum('scheduler material plan' in s for s in seen))

    assert counts([920]) == (1, 1)
    assert counts([921, 922, 923, 924, 925, 926]) == (1, 1)


# ---------------- the setting through the real app ----------------

CLOCK = r'''
from datetime import datetime, timezone
NOON = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOON if tz is None else NOON.astimezone(tz)
from modules.jobs import scheduler as SCHED
SCHED.datetime = FixedClock
from modules.jobs import scheduler_planning
scheduler_planning.datetime = FixedClock
'''

MODE_PROBE = CLOCK + r'''
install_license("pro")
admin_id = seed_user("mat-admin", "admin")
seed_user("mat-operator", "operator")
seed_user("mat-viewer", "viewer")
admin, operator, viewer = login("mat-admin"), login("mat-operator"), login("mat-viewer")
sql("INSERT INTO printers(id,name,model,machine_type,is_active,api_type) VALUES (21,'Mat-21','Bambu Lab P1S','P1S',1,'bambu')")
sql("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES (21,1,'PLA')")
sql("INSERT INTO jobs(id,item_name,status,quantity,duration_hours,priority,hold,charged_to_user_id,target_type,filament_type,created_at) "
    "VALUES (41,'Needs PETG','pending',1,1,3,0,:u,'specific','PETG','2026-10-01 00:00:00')", u=admin_id)
OUT["default"] = body(viewer.get("/api/config")).get("scheduler_material_mode")
OUT["run1_status"] = admin.post("/api/scheduler/run").status_code
OUT["require_run"] = sql("SELECT status, printer_id FROM jobs WHERE id=41")[0]
OUT["operator_put"] = operator.put("/api/config", json={"scheduler_material_mode": "off"}).status_code
OUT["after_refused"] = body(viewer.get("/api/config")).get("scheduler_material_mode")
OUT["invalid_put"] = admin.put("/api/config", json={"scheduler_material_mode": "always"}).status_code
OUT["put_off"] = admin.put("/api/config", json={"scheduler_material_mode": "off"}).status_code
OUT["after_off"] = body(viewer.get("/api/config")).get("scheduler_material_mode")
OUT["run2_status"] = admin.post("/api/scheduler/run").status_code
OUT["off_run"] = sql("SELECT status, printer_id FROM jobs WHERE id=41")[0]
OUT["audit"] = [r[0] for r in sql("SELECT details FROM audit_logs WHERE action='config.updated' ORDER BY id")]
OUT["notes"] = [r[0] for r in sql("SELECT notes FROM scheduler_runs ORDER BY id")]
'''


@pytest.fixture(scope="module")
def mode_probe(tmp_path_factory):
    root = tmp_path_factory.mktemp("material-mode")
    return run_probe("material-mode", MODE_PROBE, root, {"ENV_FILE_PATH": str(root / "data.env")})


def test_setting_defaults_to_require_and_is_readable_by_a_viewer(mode_probe):
    assert mode_probe["default"] == "require"


def test_setting_write_is_admin_only_and_validated(mode_probe):
    assert mode_probe["operator_put"] == 403
    assert mode_probe["after_refused"] == "require"
    assert mode_probe["invalid_put"] == 422
    assert mode_probe["put_off"] == 200 and mode_probe["after_off"] == "off"


def test_setting_change_is_audited_with_old_and_new_value(mode_probe):
    details = [json.loads(d) if isinstance(d, str) else d for d in mode_probe["audit"]]
    changes = [d for d in details if 'scheduler_material_mode' in d]
    assert changes and changes[-1]['scheduler_material_mode'] == {'from': 'require', 'to': 'off'}
    assert 'scheduler_material_mode' in changes[-1]['keys']


def test_default_holds_the_job_off_releases_it_and_the_run_says_why(mode_probe):
    assert mode_probe["run1_status"] == 200 and mode_probe["run2_status"] == 200
    assert mode_probe["require_run"] == ["pending", None]
    assert mode_probe["off_run"][0] == "scheduled" and mode_probe["off_run"][1] == 21
    assert any(n and "no printer has PETG loaded" in n for n in mode_probe["notes"])


# ---------------- Codex sign-off 11-12 ----------------
def test_an_unrecognised_file_material_is_unknown_even_when_the_job_field_is_set():
    """The job field stood in for a file that said "PLA Basic": a known PETG then excluded a
    PLA printer, although unknown must never exclude."""
    basic = json.dumps([{'slot': 1, 'type': 'PLA Basic'}])
    assert sm.needed_materials(basic, 'PETG') is None
    assert sm.needed_materials(json.dumps([{'slot': 1, 'type': 'PLA'}, {'slot': 2, 'type': 'PLA Basic'}]), 'PETG') is None
    assert sm.needed_materials(json.dumps([{'slot': 1, 'type': ''}]), 'PETG') == ('PETG',)   # the file names none


def test_an_unrecognised_file_material_never_excludes_a_printer(fleet):
    only_active(fleet, [11])
    load(fleet, 11, ['PLA'])
    add_job(fleet, 931)
    need(fleet, 931, 'PETG')
    file_needs(fleet, 931, ['PLA Basic'])
    run(fleet)
    assert placed(fleet, 931) == ('scheduled', 11)


def test_the_reason_is_about_the_set_when_each_printer_lacks_a_different_material(fleet):
    """PLA on one printer and PETG on the other: "no printer has PETG loaded" was false."""
    only_active(fleet, [11, 12])
    load(fleet, 11, ['PLA'])
    load(fleet, 12, ['PETG'])
    add_job(fleet, 932)
    file_needs(fleet, 932, ['PLA', 'PETG'])
    result = run(fleet)
    assert placed(fleet, 932) == ('pending', None)
    assert any('no printer has PLA and PETG loaded together' in e or 'no printer has PETG and PLA loaded together' in e
               for e in result.errors), result.errors
    assert sm.refusal_text(('PLA', 'PETG'), together=True) == 'no printer has PLA and PETG loaded together'
