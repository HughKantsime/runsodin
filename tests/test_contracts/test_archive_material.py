import csv
import io
import json
import pytest
from sqlalchemy import event, text
from tests.test_contracts.test_cert_E_notif_postgres import pg_engine
from tests.test_contracts.test_archive_reprint_behavior import reprint_client, review_db


def seed(db):
    db.execute(text("INSERT INTO filament_library(id,name,brand,material) VALUES(501,'One','Test','pla'),(502,'Two','Test','PETG_CF')"))
    db.execute(text("INSERT INTO spools(id,org_id,filament_id) VALUES(501,1,501),(502,1,502),(503,2,501)"))
    db.execute(text("UPDATE jobs SET filament_type='ABS' WHERE id=101"))
    db.execute(text("UPDATE print_files SET filaments_json=:f WHERE id=101"), {'f':json.dumps([{'type':'PETG-CF','used_grams':5}, {'type':'Unknown'}])})
    db.commit()


@pytest.mark.parametrize('case,source,expected', [
    ('single','spool',[('PLA',3)]), ('several','spool',[('PLA',3),('PETG-CF',4)]),
    ('foreign','file',[('PETG-CF',5)]), ('file','file',[('PETG-CF',5)]),
    ('job','job',[('ABS',None)]), ('custom','file',[('PEEK',None)]), ('long','file',[('S'*128,None)]),
    ('control','job',[('ABS',None)]), ('none',None,[])])
def test_material_resolves_consistently(reprint_client, review_db, monkeypatch, case, source, expected):
    from modules.archives.routes import archives_crud
    monkeypatch.setattr(archives_crud, 'require_feature', lambda *args: None)
    client, _, _ = reprint_client
    seed(review_db)
    usage = [{'spool_id':501, 'grams_used':1},{'spool_id':501,'grams_used':2}] if case in ('single','several') else []
    if case == 'several':
        usage.append({'spool_id':502,'grams_used':4})
    if case == 'foreign':
        usage.append({'spool_id':503,'grams_used':99})
    if case == 'control':
        labels = ['PE\nEK', 'PLA\t', 'PETG\x85', 'X' * 128 + '\x00']
        review_db.execute(text("UPDATE print_files SET filaments_json=:f WHERE id=101"),
                          {'f': json.dumps([{'type': label} for label in labels])})
    if case in ('custom', 'long'):
        label = ' peek ' if case == 'custom' else 'ß' * 200
        review_db.execute(text("UPDATE print_files SET filaments_json=:f WHERE id=101"),
                          {'f': json.dumps([{'type':label},{'type':'Empty'}])})
    if case in ('job','none'):
        review_db.execute(text("UPDATE print_archives SET print_file_id=NULL WHERE id=101"))
    if case == 'none':
        review_db.execute(text("UPDATE print_archives SET job_id=NULL WHERE id=101"))
    review_db.execute(text("UPDATE print_archives SET consumption_json=:u WHERE id=101"), {'u':json.dumps(usage)})
    review_db.commit()
    material = [{'material':m,'grams':g} for m,g in expected]
    detail = client.get('/archives/101').json()
    compare = client.get('/archives/compare?a=101&b=101').json()
    for row in [detail, compare['a'], compare['b'], *[next(r for r in client.get(path).json()['items'] if r['id']==101)
               for path in ('/archives','/archives/log')]]:
        assert (row['materials'],row['material_source'],row['material']) == (material,source,expected[0][0] if len(expected)==1 else None)
    exported = next(r for r in csv.DictReader(io.StringIO(client.get('/archives/log/export').text)) if r['Print Name']=='Original print')
    assert json.loads(exported['Materials']) == material and exported['Material source'] == (source or '')


def test_page_has_bounded_material_queries(reprint_client, review_db):
    from modules.archives.materials import add_materials
    seed(review_db)
    rows = [dict(id=i, org_id=1, consumption_json='[{"spool_id":501,"grams_used":2}]') for i in range(501, 701)]
    review_db.execute(text("INSERT INTO print_archives(id,org_id,job_id,print_name,status,consumption_json) "
                           "VALUES(:id,:org_id,101,'Test','completed',:consumption_json)"), rows)
    queries = []
    def count(*args):
        queries.append(args[2])
    engine = review_db.get_bind()
    event.listen(engine, 'before_cursor_execute', count)
    try:
        add_materials(review_db, rows, {'id':3,'role':'admin','group_id':1})
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    assert len(queries) <= 3
    assert all(row['material'] == 'PLA' for row in rows)


def test_archive_browser_material_projection(reprint_client, review_db):
    from pathlib import Path
    from tests.test_contracts.cert_D_capture import publish
    fixture = Path(__file__).parents[1] / 'accessibility' / 'fixtures' / 'fjord.json'
    key = 'GET /api/archives'
    scenario = json.loads(fixture.read_text())[key]
    item, = scenario['items']
    # Keep the browser scenario's print, date and printer. Capture the material evidence from its real route.
    response = reprint_client[0].get('/archives')
    assert response.status_code == 200
    row = next(r for r in response.json()['items'] if r['id'] == 101)
    publish(fixture, {**scenario, 'items': [{**item, **{k: row[k] for k in ('material','materials','material_source')}}]}, key=key)


@pytest.mark.postgresql
def test_archive_material_on_postgresql(pg_engine):
    from sqlalchemy.orm import Session
    from modules.archives.materials import add_materials
    with Session(pg_engine) as db:
        db.execute(text("INSERT INTO jobs(id,item_name,status,filament_type,charged_to_org_id) VALUES(101,'Sample','completed','ABS',1)"))
        db.execute(text("INSERT INTO print_archives(id,job_id,org_id,print_name,status) VALUES(101,101,1,'Sample','completed')"))
        seed(db)
        rows = [dict(id=101,org_id=1,consumption_json='[{"spool_id":501,"grams_used":3}]')]
        assert add_materials(db, rows, {'id':3,'role':'admin','group_id':1})[0]['material'] == 'PLA'
