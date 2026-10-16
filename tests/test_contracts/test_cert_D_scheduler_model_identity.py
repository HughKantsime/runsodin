"""cert-D F04-4.2-06: scheduler model matching uses the single Bambu model table and fails closed.

Every Bambu Studio model id in printer_models.BAMBU_MODEL_IDS is driven end to end through the
real Scheduler.run against a fleet with one printer per known model (friendly names as MQTT
auto-detect stores them). The file must land only on its own model; unknown/unsourced codes
never land on any printer with a known model.
"""
import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_D_jobsB_scheduler import fleet, run, rows  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

from modules.printers.printer_models import BAMBU_MODEL_IDS as _IDS  # noqa: E402

# One printer per distinct model in the single Bambu table (friendly names, as MQTT auto-detect stores them).
FLEET = {31 + i: name for i, name in enumerate(sorted(set(_IDS.values())))}


def _fleet(db, models=FLEET):
    db.execute(text('UPDATE printers SET is_active=0'))
    for pid, model in models.items():
        db.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,org_id) "
                        "VALUES(:id,:n,:m,:m,1,'bambu',1)"), {'id': pid, 'n': f'{model}-{pid}', 'm': model})
        db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(:p,1,'PLA')"), {'p': pid})
    db.commit()


def _job_for_file(db, code, job_id=900, model_id=900):
    db.execute(text("INSERT INTO models(id,name,org_id,build_time_hours) VALUES(:m,:n,1,1)"), {'m': model_id, 'n': f'File {code}'})
    db.execute(text("INSERT INTO print_files(filename,model_id,printer_model,org_id) VALUES(:f,:m,:c,1)"),
               {'f': f'{code}.3mf', 'm': model_id, 'c': code})
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id,quantity,duration_hours,hold,"
                    "priority,model_id,target_type) VALUES(:id,:n,'pending',1,1,1,1,0,3,:m,'specific')"),
               {'id': job_id, 'n': f'File {code}', 'm': model_id})
    db.commit()
    return job_id


@pytest.mark.parametrize('code', sorted(__import__('modules.printers.printer_models', fromlist=['x']).BAMBU_MODEL_IDS))
def test_each_bambu_model_id_schedules_only_on_its_own_model(fleet, code):
    from modules.printers.printer_models import BAMBU_MODEL_IDS
    _fleet(fleet)
    job = _job_for_file(fleet, code)
    run(fleet)
    status, printer, *_ = rows(fleet, [job])[job]
    assert status == 'scheduled' and FLEET[printer] == BAMBU_MODEL_IDS[code], (code, printer)


def test_c11_file_is_not_placed_on_x1c_when_only_x1c_and_p1s_are_available(fleet):
    _fleet(fleet, {31: 'X1C', 35: 'P1S'})
    job = _job_for_file(fleet, 'C11')  # Bambu Studio: C11 = P1P
    run(fleet)
    assert rows(fleet, [job])[job][:2] == ('pending', None)


@pytest.mark.parametrize('code', ['BL-A001', 'BL-P003', 'BL-H001', 'BL-P00501', 'Unknown', 'ZZ9'])
def test_unknown_or_unsourced_codes_fail_closed(fleet, code):
    _fleet(fleet)
    job = _job_for_file(fleet, code)
    run(fleet)
    assert rows(fleet, [job])[job][:2] == ('pending', None)


def test_product_name_and_code_resolve_to_the_same_model(fleet):
    _fleet(fleet, {35: 'Bambu Lab P1S', 31: 'Bambu Lab X1 Carbon'})
    job = _job_for_file(fleet, 'C12')
    run(fleet)
    assert rows(fleet, [job])[job][:2] == ('scheduled', 35)


def test_auto_detected_machine_type_wins_over_a_stale_user_model(fleet):
    fleet.execute(text('UPDATE printers SET is_active=0')); fleet.commit()
    # User once typed "P1S" into model; MQTT auto-detect reports the machine is an X1C.
    fleet.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,org_id) "
                       "VALUES(41,'Relabelled','P1S','X1C',1,'bambu',1),(42,'Model only','P1S','',1,'bambu',1)"))
    fleet.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(41,1,'PLA'),(42,1,'PLA')"))
    fleet.commit()
    p1s = _job_for_file(fleet, 'C12', job_id=950, model_id=950)
    x1c = _job_for_file(fleet, 'BL-P001', job_id=951, model_id=951)
    run(fleet)
    got = rows(fleet, [p1s, x1c])
    assert got[p1s][:2] == ('scheduled', 42)   # empty machine_type -> model 'P1S' used
    assert got[x1c][:2] == ('scheduled', 41)   # machine_type X1C wins over stale model 'P1S'
