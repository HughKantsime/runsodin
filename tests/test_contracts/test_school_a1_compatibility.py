"""School candidate A1 matching through real upload, approval and scheduling."""
import pytest
from sqlalchemy import text
from core.errors import OdinError
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_education_upload_scheduler_integration import _upload_and_approve,_schedule


# BL-A001 is not a Bambu model id (Bambu Studio: A1 = N2S); it is no longer treated as an A1 alias.
@pytest.mark.parametrize('machine',['Bambu Lab A1','A1','N2S'])
def test_a1_file_approves_and_schedules_only_on_entitled_a1(review_db,tmp_path,monkeypatch,machine):
    review_db.execute(text("UPDATE printers SET name='A1-Pilot',model='Bambu Lab A1',machine_type='A1' WHERE id=9"))
    review_db.commit()
    uploaded=_upload_and_approve(review_db,tmp_path,machine)
    result=_schedule(review_db,monkeypatch)
    assert result.success and result.scheduled_count==1
    assert [assignment.printer_id for assignment in result.assignments]==[9]
    actual=review_db.execute(text('SELECT s.status,s.approved_printer_id,j.printer_id FROM education_submissions s JOIN jobs j ON j.id=s.job_id WHERE s.id=:id'),{'id':uploaded['id']}).one()
    assert tuple(actual)==('scheduled',9,9)


def test_a1_file_cannot_be_approved_on_p1s(review_db,tmp_path):
    with pytest.raises(OdinError):_upload_and_approve(review_db,tmp_path,'Bambu Lab A1')
    assert review_db.execute(text('SELECT COUNT(*) FROM education_submissions WHERE approved_printer_id IS NOT NULL')).scalar_one()==0
    assert review_db.execute(text("SELECT COUNT(*) FROM jobs WHERE status='scheduled'")).scalar_one()==0


@pytest.mark.parametrize('machine',['Bambu Lab A1 mini','N1'])
def test_a1_mini_file_cannot_be_approved_on_full_a1(review_db,tmp_path,machine):
    review_db.execute(text("UPDATE printers SET model='Bambu Lab A1',machine_type='A1' WHERE id=9"))
    review_db.commit()
    with pytest.raises(OdinError):_upload_and_approve(review_db,tmp_path,machine)
    assert review_db.execute(text('SELECT COUNT(*) FROM education_submissions WHERE approved_printer_id IS NOT NULL')).scalar_one()==0


@pytest.mark.parametrize('code,name',[('N2S','A1'),('N1','A1 Mini')])
def test_a1_firmware_names_are_not_swapped(code,name):
    from modules.models_library.threemf_parser import _friendly_printer_name
    assert _friendly_printer_name(code)==name


@pytest.mark.parametrize('name',['A1','N2S','N1','Bambu Lab A1','A1 Mini','X1E','C12','C13','X1C','P1S','P1P','H2D','H2S','H2C','BL-P001','BL-P002','BL-P003','BL-A001','BL-A003','Bambu Lab','X1 Carbon','C11','O1D'])
def test_bambu_api_inference_preserves_known_aliases(name):
    from modules.models_library.print_file_meta import _api_types_for_machine
    assert _api_types_for_machine(name)==['bambu']


@pytest.mark.parametrize('name',['K1','Creality K1','Unknown A1 clone','Unidentified N2S'])
def test_new_a1_aliases_do_not_infer_unrelated_api(name):
    from modules.models_library.print_file_meta import _api_types_for_machine
    assert _api_types_for_machine(name)==[]


@pytest.mark.parametrize('damage',['nozzle','bed','material'])
def test_a1_alias_does_not_bypass_physical_compatibility(review_db,tmp_path,damage):
    review_db.execute(text("UPDATE printers SET model='Bambu Lab A1',machine_type='A1' WHERE id=9"))
    if damage=='nozzle':review_db.execute(text('UPDATE printers SET nozzle_diameter=0.6 WHERE id=9'))
    elif damage=='bed':review_db.execute(text('UPDATE printers SET bed_x_mm=180 WHERE id=9'))
    else:review_db.execute(text("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=9"))
    review_db.commit()
    with pytest.raises(OdinError):_upload_and_approve(review_db,tmp_path,'N2S')
    assert review_db.execute(text('SELECT COUNT(*) FROM education_submissions WHERE approved_printer_id IS NOT NULL')).scalar_one()==0
