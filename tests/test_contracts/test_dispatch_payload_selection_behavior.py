"""Actual dispatch selection must not depend on unspecified SQL row order."""
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch

def test_model_primary_file_is_selected_over_other_model_files(review_db,tmp_path,monkeypatch):
    from modules.printers import dispatch
    engine, _ = configure_dispatch(review_db,tmp_path,monkeypatch)
    alternate=tmp_path/'primary.gcode';alternate.write_bytes(b'G91\n')
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(55,51,'primary.gcode',:path,1)"), {'path':str(alternate)})
        conn.execute(text('UPDATE models SET print_file_id=55 WHERE id=51'))
    job=dispatch._load_job(53)
    assert job['resolved_print_file_id']==55
    assert job['stored_path']==str(alternate)

import pytest

@pytest.mark.parametrize('branch,expected', [('explicit',52),('primary',55),('latest',55),('missing_primary',None),('missing_explicit',None),('wrong_primary_model',None),('wrong_explicit_model',None)])
def test_canonical_selection_and_missing_reference_refusal(review_db,tmp_path,monkeypatch,branch,expected):
    from modules.printers import dispatch
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO models(id,name,org_id) VALUES(56,'Other synthetic model',1)"))
        conn.execute(text("INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(55,51,'alternate.gcode','/owned/alternate.gcode',1)"))
        if branch in ('primary','explicit'):
            conn.execute(text('UPDATE models SET print_file_id=55 WHERE id=51'))
        if branch=='explicit':conn.execute(text('UPDATE jobs SET print_file_id=52 WHERE id=53'))
        if branch=='missing_primary':conn.execute(text('UPDATE models SET print_file_id=999 WHERE id=51'))
        if branch=='missing_explicit':conn.execute(text('UPDATE jobs SET print_file_id=999 WHERE id=53'))
        if branch.startswith('wrong_'):
            conn.execute(text('UPDATE print_files SET model_id=56 WHERE id=55'))
            conn.execute(text('UPDATE models SET print_file_id=55 WHERE id=51') if branch=='wrong_primary_model' else text('UPDATE jobs SET print_file_id=55 WHERE id=53'))
    loaded=dispatch._load_job(53)
    assert (loaded['resolved_print_file_id'] if loaded else None)==expected
    automatic=dispatch._get_next_scheduled_job(59)
    if expected is not None:
        assert automatic and automatic['stored_path']==loaded['stored_path']
    elif automatic:
        assert automatic['id']!=53

@pytest.mark.parametrize('corruption',['none','explicit_conflict','file_model_conflict'])
def test_education_submission_selection_refuses_contradictory_graph(review_db,monkeypatch,corruption):
    from core import db
    from modules.printers import dispatch
    engine=review_db.get_bind()
    monkeypatch.setattr(db,'engine',engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='scheduled',printer_id=9 WHERE id=13"))
        if corruption=='explicit_conflict':
            conn.execute(text("INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(55,12,'alternate.3mf','/owned/alternate.3mf',1)"))
            conn.execute(text('UPDATE jobs SET print_file_id=55 WHERE id=13'))
        elif corruption=='file_model_conflict':
            conn.execute(text("INSERT INTO models(id,name,org_id) VALUES(56,'Other synthetic model',1)"))
            conn.execute(text('UPDATE print_files SET model_id=56 WHERE id=11'))
    loaded=dispatch._load_job(13)
    automatic=dispatch._get_next_scheduled_job(9)
    if corruption=='none':
        assert loaded['resolved_print_file_id']==11
        assert automatic['stored_path']==loaded['stored_path']
    else:
        assert loaded is None and automatic is None
