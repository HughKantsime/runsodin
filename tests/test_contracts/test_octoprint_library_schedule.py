"""Scheduling a library file an OctoPrint printer cannot print is refused up front."""
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def test_bgcode_model_is_refused_for_an_octoprint_printer_at_schedule_time(creators, review_db, tmp_path):  # noqa: F811
    from modules.models_library.routes import models_crud
    client, auth, _, _ = creators
    client.app.include_router(models_crud.router, prefix='/api')
    path = tmp_path / 'part.bgcode'
    path.write_bytes(b'GCDE')
    review_db.execute(text("INSERT INTO models(id,name,org_id,print_file_id) VALUES(502,'Bgcode part',1,NULL)"))
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id) "
                           "VALUES(502,502,'part.bgcode','part.bgcode',:path,1)"), {'path': str(path)})
    review_db.execute(text("UPDATE printers SET api_type='octoprint' WHERE id=9"))
    review_db.commit()
    before = review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()
    response = client.post('/api/models/502/schedule?printer_id=9', headers=auth(), json={})
    assert response.status_code == 400, response.text
    assert 'incompatible' in response.text
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one() == before
