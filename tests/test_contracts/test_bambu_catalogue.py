"""Synthetic file only: no Bambu catalogue data or printer connections."""
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from core.dependencies import get_current_user
from core.print_payload import material_match
from modules.inventory.catalogue import BAMBU_TYPES, base_material, parse_catalogue, MAX_BYTES
from modules.inventory.catalogue_slots import slot_catalogue
from modules.inventory.models import FilamentLibrary, Spool
from modules.printers.models import Printer, FilamentSlot
from tests.test_contracts.test_inventory_claim_acceptance import inventory_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/catalogue/invented.json'
ADMIN = {'id': 3, 'role': 'admin', 'group_id': None}
USER = {'id': 3, 'role': 'operator', 'group_id': 1}


def payload():
    return json.loads(FIXTURE.read_text())


def upload(client, data=None):
    client.app.dependency_overrides[get_current_user] = lambda: ADMIN
    return client.post('/api/filaments/import-bambu', json=payload() if data is None else data)


def test_alias_names_are_pinned():
    # Independent approved list, not derived from the implementation.
    names = '''PLA Basic|PLA Dynamic|PLA Galaxy|PLA Glow|PLA Lite|PLA Marble|PLA Matte|PLA Metal|PLA Palette|PLA Pure|PLA Silk|PLA Silk+|PLA Sparkle|PLA Tough|PLA Tough+|PLA Translucent|PLA Wood|Support for PLA|Support for PLA/PETG|PLA Aero|PLA-CF|PETG Basic|PETG HF|PETG Matte|PETG Translucent|PETG-CF|ABS|Support for ABS|ABS-GF|ASA|Support for PA/PET|PAHT-CF|PA6-GF|PA6-CF|PC|PC FR|PVA|TPU 75A (2.85mm)|TPU 85A|TPU 90A|TPU 95A|TPU 95A HF|ASA Aero|ASA-CF|PET-CF|PPA-CF|PPS-CF|TPU for AMS'''.split('|')
    assert len(names) == len(BAMBU_TYPES) == 48
    assert set(names) == set(BAMBU_TYPES)
    assert base_material(' Unlisted Test Polymer ') is None
    assert base_material('PLA Mat') is None


@pytest.mark.parametrize('name,base', [('PLA Matte','PLA'), ('PLA Silk+','PLA'), ('PLA Basic','PLA'),
    ('PETG HF','PETG'), ('TPU 95A','TPU'), ('PAHT-CF','PA-CF'), ('PA6-GF','PA-GF'), ('PLA Aero','PLA-AERO')])
def test_kobra_profile_agreement(name, base):
    assert base_material('  '+name.swapcase()+'  ') == base
    assert material_match(base_material(name), base) is True


def test_matte_uses_pla_without_relaxing_gates():
    assert material_match(base_material('PLA Matte'), 'PLA') is True
    for other in ('PLA-CF','PETG','PLA-MATTE','PLA-SILK','PLA Matte'):
        assert material_match(base_material('PLA Matte'), other) is not True


def test_import_repeat_unknown_multicolour_audit(inventory_client, review_db):
    response = upload(inventory_client)
    assert response.status_code == 200, response.text
    assert response.json() == dict(created=3, updated=0, preserved=0, unrecognised_types=['Unlisted Test Polymer'])
    rows = review_db.query(FilamentLibrary).filter(FilamentLibrary.catalogue_code.isnot(None)).order_by(FilamentLibrary.id).all()
    assert [r.material for r in rows] == ['PLA','PLA','OTHER']
    assert rows[1].color_hex == '1234AB' and rows[1].catalogue_colors == ['#1234ABFF','#6789CD80']
    assert len(rows[2].catalogue_colors) == 4
    data = payload(); data['data'][0]['fila_color_name']['en'] = 'Invented Revised'
    assert upload(inventory_client, data).json()['created'] == 0
    review_db.expire_all()
    assert rows[0].name == 'PLA Matte Invented Revised'
    assert review_db.query(FilamentLibrary).filter(FilamentLibrary.catalogue_code.isnot(None)).count() == 3
    assert review_db.execute(text("SELECT count(*) FROM audit_logs WHERE action='filament.catalogue_imported'")).scalar() == 2
    listing = inventory_client.get('/api/filaments').json()
    assert any(f['catalogue_code']=='ODIN-TEST-COMET' and f['material'] == 'OTHER' for f in listing)


def test_reimport_preserves_used_identity_and_price(inventory_client, review_db):
    upload(inventory_client)
    row = review_db.query(FilamentLibrary).filter_by(catalogue_code='ODIN-TEST-ORBIT').one()
    sid = inventory_client.post('/api/spools', json={'filament_id':row.id}).json()['id']
    row.cost_per_gram = 0.123; review_db.commit()
    data = payload(); data['data'][0].update(fila_type='PETG HF', fila_color=['#789ABC80'])
    response = upload(inventory_client, data)
    assert response.json()['preserved'] == 1
    review_db.expire_all()
    assert row.material == 'PLA' and row.catalogue_type == 'PLA Matte' and row.cost_per_gram == 0.123
    assert row.color_hex == '789ABC' and row.catalogue_colors == ['#789ABC80']
    assert review_db.get(Spool,sid).filament_id == row.id


@pytest.mark.parametrize('mutation', ['json','total','duplicate','alpha','colors','english','deep'])
def test_malformed_refused_whole(inventory_client, review_db, mutation):
    data=payload()
    if mutation == 'total': data['total']=4
    if mutation == 'duplicate': data['data'][1]['fila_color_code']=data['data'][0]['fila_color_code']
    if mutation == 'alpha': data['data'][2]['fila_color']=['#123456']
    if mutation == 'colors': data['data'][2]['fila_color']=['#123456FF']*3
    if mutation == 'english': del data['data'][2]['fila_color_name']['en']
    inventory_client.app.dependency_overrides[get_current_user]=lambda: ADMIN
    body = b'{' if mutation=='json' else b'['*1200 if mutation=='deep' else json.dumps(data).encode()
    response=inventory_client.post('/api/filaments/import-bambu',content=body,headers={'Content-Type':'application/json'})
    assert response.status_code == 400
    assert review_db.query(FilamentLibrary).filter(FilamentLibrary.catalogue_code.isnot(None)).count()==0
    assert review_db.execute(text("SELECT count(*) FROM audit_logs WHERE action='filament.catalogue_imported'")).scalar()==0


def test_oversize_refused(inventory_client):
    inventory_client.app.dependency_overrides[get_current_user]=lambda: ADMIN
    assert inventory_client.post('/api/filaments/import-bambu',content=b' '* (MAX_BYTES+1),headers={'Content-Type':'application/json'}).status_code==413


@pytest.mark.parametrize('role,org', [('viewer',None),('operator',None),('admin',1),('admin',2)])
def test_only_installation_admin_can_import(inventory_client, role, org):
    inventory_client.app.dependency_overrides[get_current_user]=lambda: {**ADMIN,'role':role,'group_id':org}
    assert inventory_client.post('/api/filaments/import-bambu',json=payload()).status_code==403


def slot_fixture(review_db, api='bambu', material='PLA', color='1234AB'):
    printer=review_db.query(Printer).first()
    printer.api_type=api; printer.org_id=1
    printer.ace_status=json.dumps({'slots':[{'index':0,'type':material,'color_hex':color,'status':'ready'}]}) if api=='anycubic_lan' else None
    slot=review_db.query(FilamentSlot).filter_by(printer_id=printer.id,slot_number=1).first()
    if slot is None:
        slot=FilamentSlot(printer_id=printer.id,slot_number=1); review_db.add(slot)
    from core.base import FilamentType
    slot.filament_type=FilamentType(material); slot.color_hex=color
    slot.tray_observation=json.dumps({'observed':{'type':material,'color':color,'exists':True}}) if api=='bambu' else None
    review_db.commit()
    return printer,slot


@pytest.mark.parametrize('api', ['bambu','anycubic_lan','moonraker'])
def test_slot_one_many_none_and_remembered_on_spool(inventory_client, review_db, api, monkeypatch):
    from modules.printers import kobra_s1
    monkeypatch.setattr(kobra_s1, 'build_payload', lambda *a, **k: pytest.fail('Catalogue sent a printer command'))
    upload(inventory_client)
    printer,slot=slot_fixture(review_db,api)
    many=slot_catalogue(review_db,slot,USER)
    assert many['catalogue_name'] is None and len(many['catalogue_choices'])==2
    choice=many['catalogue_choices'][1]
    inventory_client.app.dependency_overrides[get_current_user]=lambda:USER
    sid=inventory_client.post('/api/spools',json={'filament_id':990}).json()['id']
    slot.assigned_spool_id=sid; review_db.commit()
    before=(slot.filament_type,slot.color_hex,slot.tray_observation,printer.ace_status)
    response=inventory_client.post(f'/api/printers/{printer.id}/slots/1/catalogue',json={'spool_id':sid,'filament_id':choice['id']})
    assert response.status_code==200,response.text
    review_db.expire_all()
    assert review_db.get(Spool,sid).filament_id==choice['id']
    assert slot_catalogue(review_db,slot,USER)['catalogue_name']==choice['name']
    assert (slot.filament_type,slot.color_hex,slot.tray_observation,printer.ace_status)==before
    review_db.query(FilamentLibrary).filter_by(catalogue_code='ODIN-TEST-ORBIT').delete(); review_db.commit()
    assert slot_catalogue(review_db,slot,USER)['catalogue_name']==choice['name']
    slot.assigned_spool_id=None; review_db.commit()
    assert slot_catalogue(review_db,slot,USER)['catalogue_name']==choice['name']
    printer,slot=slot_fixture(review_db,api,color='FED321')
    assert slot_catalogue(review_db,slot,USER)==dict(catalogue_name=None,catalogue_choices=[])


def test_foreign_spool_choice_refused(inventory_client, review_db):
    upload(inventory_client)
    printer,slot=slot_fixture(review_db)
    candidate=slot_catalogue(review_db,slot,USER)['catalogue_choices'][0]['id']
    inventory_client.app.dependency_overrides[get_current_user]=lambda:{**USER,'group_id':2}
    sid=inventory_client.post('/api/spools',json={'filament_id':990}).json()['id']
    spool=review_db.get(Spool,sid); slot.assigned_spool_id=sid; review_db.commit()
    inventory_client.app.dependency_overrides[get_current_user]=lambda:USER
    assert inventory_client.patch(f'/api/spools/{sid}',json={'filament_id':candidate}).status_code==404
    assert inventory_client.post(f'/api/printers/{printer.id}/slots/1/catalogue',json={'spool_id':sid,'filament_id':candidate}).status_code==404
    review_db.expire_all(); assert spool.filament_id==990


def test_fixture_is_handmade_and_no_catalogue_file_is_bundled():
    root=Path(__file__).resolve().parents[2]
    assert all(e['fila_color_code'].startswith('ODIN-TEST-') for e in payload()['data'])
    # File names are checked without opening any real customer catalogue.
    for directory in ('backend','frontend/src','docker','tests/fixtures'):
        assert not list((root/directory).rglob('filaments_color_codes.json'))


def test_import_audit_failure_rolls_back(inventory_client, review_db, monkeypatch):
    from modules.inventory.routes import catalogue
    monkeypatch.setattr(catalogue,'log_audit',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('synthetic audit failure')))
    with pytest.raises(RuntimeError): upload(inventory_client)
    review_db.rollback()
    assert review_db.query(FilamentLibrary).filter(FilamentLibrary.catalogue_code.isnot(None)).count()==0


@pytest.mark.parametrize('name', ['ASA Aero','ASA-CF','PET-CF','PPA-CF','PPS-CF','TPU for AMS'])
def test_part_b_types_stay_unrecognised(name):
    from core.material_identity import canonical_reported_material
    assert base_material(name) is None
    assert canonical_reported_material(BAMBU_TYPES[name]) is None
    entry = payload(); entry['data'][0]['fila_type'] = name
    assert parse_catalogue(json.dumps(entry))[0]['material'] == 'OTHER'
    assert material_match('OTHER', BAMBU_TYPES[name]) is None


@pytest.mark.parametrize('content_type', ['text/plain','application/x-www-form-urlencoded','multipart/form-data',''])
def test_import_requires_json_content_type(inventory_client, content_type):
    inventory_client.app.dependency_overrides[get_current_user] = lambda: ADMIN
    response = inventory_client.post('/api/filaments/import-bambu', content=json.dumps(payload()),
                                     headers={'Content-Type':content_type})
    assert response.status_code == 415


@pytest.mark.parametrize('role,org', [('operator',None),('admin',1)])
def test_imported_edit_delete_share_installation_admin_policy(inventory_client, review_db, role, org):
    upload(inventory_client)
    row = review_db.query(FilamentLibrary).filter_by(catalogue_code='ODIN-TEST-COMET').one()
    inventory_client.app.dependency_overrides[get_current_user] = lambda: {**ADMIN,'role':role,'group_id':org}
    assert inventory_client.patch(f'/api/filaments/{row.id}',json={'name':'Changed'}).status_code == 403
    assert inventory_client.delete(f'/api/filaments/{row.id}').status_code == 403
    review_db.refresh(row)
    assert row.material == 'OTHER' and row.name == 'Unlisted Test Polymer Invented Comet'


def test_unknown_type_assign_edit_cost_ordering(inventory_client, review_db):
    from core.base import FilamentType
    from modules.printers.routes_crud import update_filament_slot
    from modules.printers.schemas import FilamentSlotUpdate
    from modules.models_library.services import material_cost_per_gram
    upload(inventory_client)
    row = review_db.query(FilamentLibrary).filter_by(catalogue_code='ODIN-TEST-COMET').one()
    sid = inventory_client.post('/api/spools',json={'filament_id':row.id}).json()['id']
    # The existing slot editor supplies the selected filament's material/colour.
    printer, slot = slot_fixture(review_db, api='moonraker')
    update_filament_slot(printer.id, 1, FilamentSlotUpdate(assigned_spool_id=sid,
        filament_type=row.material, color_hex=row.color_hex, color=row.name), ADMIN, review_db)
    assert slot.filament_type == FilamentType.OTHER and slot.color_hex == row.color_hex
    assert slot.assigned_spool_id == sid and review_db.get(Spool,sid).location_printer_id == printer.id
    assert inventory_client.patch(f'/api/filaments/{row.id}',json={'name':'Invented Rename','material':'OTHER','cost_per_gram':0.12}).status_code == 200
    assert inventory_client.patch(f'/api/filaments/{row.id}',json={'material':None}).status_code == 409
    assert inventory_client.patch(f'/api/spools/{sid}',json={'notes':'Invented note'}).status_code == 200
    later = FilamentLibrary(brand='Synthetic',name='Later other',material='OTHER',cost_per_gram=0.01)
    review_db.add(later); review_db.commit()
    assert material_cost_per_gram(review_db,'OTHER') == 0.12  # lowest id, not lowest rate
    assert material_cost_per_gram(review_db,'PLA') is None
    assert inventory_client.delete(f'/api/filaments/{row.id}').status_code == 409


@pytest.mark.parametrize('api,field,bad', [
    ('bambu','tray_observation','{'), ('bambu','tray_observation','[]'),
    ('bambu','tray_observation','null'), ('bambu','tray_observation','{"observed":[]}'),
    ('bambu','tray_observation','{"observed":null}'),
    ('bambu','tray_observation','{"observed":{"type":[]}}'),
    ('anycubic_lan','ace_status','{'), ('anycubic_lan','ace_status','[]'),
    ('anycubic_lan','ace_status','{"slots":null}'), ('anycubic_lan','ace_status','{"slots":[null]}'),
    ('anycubic_lan','ace_status','{"slots":{"index":0}}'),
])
def test_bad_stored_json_cannot_break_get_printers(inventory_client, review_db, api, field, bad):
    from modules.printers.routes_crud import router
    inventory_client.app.include_router(router,prefix='/api')
    upload(inventory_client)
    printer, slot = slot_fixture(review_db,api)
    for row in review_db.query(Printer).all():
        row.slot_count=4; row.tags=[]; row.timelapse_enabled=False; row.shared=False
    setattr(slot if field=='tray_observation' else printer,field,bad)
    review_db.commit()
    response = inventory_client.get('/api/printers')
    assert response.status_code == 200, response.text
    actual = next(p for p in response.json() if p['id']==printer.id)['filament_slots'][0]
    assert actual['catalogue_name'] is None and actual['catalogue_choices'] == []


@pytest.mark.parametrize('api', ['bambu','moonraker'])
def test_no_observation_uses_stored_slot_only(inventory_client, review_db, api):
    upload(inventory_client)
    printer, slot = slot_fixture(review_db,api)
    slot.tray_observation = None
    assert printer.ace_status is None
    review_db.commit()
    assert len(slot_catalogue(review_db,slot,USER)['catalogue_choices']) == 2


def test_response_resolution_has_constant_query_count_40_by_16(inventory_client, review_db):
    from sqlalchemy import event
    from modules.inventory.catalogue_slots import enrich_catalogue_slots
    from modules.printers.schemas import PrinterResponse
    from core.base import FilamentType
    upload(inventory_client)
    filament = review_db.query(FilamentLibrary).filter_by(catalogue_code='ODIN-TEST-ORBIT').one()
    fid, name = filament.id, filament.name
    for i in range(40):
        printer = Printer(name=f'Synthetic display {i}',api_type='moonraker',slot_count=16)
        review_db.add(printer); review_db.flush()
        for n in range(1,17):
            review_db.add(FilamentSlot(printer_id=printer.id,slot_number=n,
                filament_type=FilamentType.PLA,color_hex='1234AB',
                tray_observation=json.dumps({'observed':{'type':'PLA','color':'1234AB'}}) if n % 2 else None,
                assigned_spool=Spool(filament_id=fid,initial_weight_g=1000,remaining_weight_g=400)))
    review_db.commit(); review_db.expunge_all()
    printers = review_db.query(Printer).filter(Printer.name.like('Synthetic display %')).all()
    statements = []
    def count(conn,cursor,sql,*args): statements.append(sql)
    event.listen(review_db.bind,'before_cursor_execute',count)
    try:
        enrich_catalogue_slots(review_db,printers,ADMIN)
        result = [PrinterResponse.model_validate(p).model_dump() for p in printers]
    finally:
        event.remove(review_db.bind,'before_cursor_execute',count)
    assert len(statements) == 2
    assert len(result) == 40 and sum(len(p['filament_slots']) for p in result) == 640
    assert all(s['catalogue_name']==name and s['catalogue_choices']==[] for p in result for s in p['filament_slots'])


def test_spool_patch_preserves_link_then_financial_lock_order(inventory_client, review_db, monkeypatch):
    from modules.inventory.routes import spools
    from modules.inventory import catalogue_slots
    upload(inventory_client)
    fid = review_db.query(FilamentLibrary).filter_by(catalogue_code='ODIN-TEST-COMET').one().id
    sid = inventory_client.post('/api/spools',json={'filament_id':990}).json()['id']
    order = []
    for module, name in [(spools,'_serialize_spoolman_link'),(spools,'require_financial_owner'),
                         (catalogue_slots,'select_spool_filament')]:
        original = getattr(module,name)
        def spy(*a,_name=name,_original=original,**kw):
            order.append(_name)
            return _original(*a,**kw)
        monkeypatch.setattr(module,name,spy)
    response = inventory_client.patch(f'/api/spools/{sid}',json={
        'filament_id':fid,'spoolman_spool_id':12345,'price':12})
    assert response.status_code == 200,response.text
    assert order == ['_serialize_spoolman_link','require_financial_owner','select_spool_filament']


def test_no_catalogue_data_in_source_tree():
    import re
    import subprocess
    root = Path(__file__).resolve().parents[2]
    files = subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).split(b'\0')
    identifiers = [b'GFA'+b'00', b'GFA'+b'01', b'GFB'+b'00']
    colour_list = re.compile(rb'["\']fila_color["\']\s*:\s*\[\s*["\']#[0-9a-fA-F]{6,8}["\']')
    for filename in files:
        if not filename:
            continue
        path = root / filename.decode()
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            assert not (sum(code in data for code in identifiers) >= 2 and colour_list.search(data)), str(path.relative_to(root))


def test_library_is_global_but_spools_stay_in_their_tenant(inventory_client, review_db):
    upload(inventory_client)
    fid = review_db.query(FilamentLibrary).filter_by(catalogue_code='ODIN-TEST-ORBIT').one().id
    inventory_client.app.dependency_overrides[get_current_user] = lambda: USER
    sid = inventory_client.post('/api/spools',json={'filament_id':fid}).json()['id']
    inventory_client.app.dependency_overrides[get_current_user] = lambda: {**USER,'group_id':2}
    assert any(f['id']==f'lib_{fid}' for f in inventory_client.get('/api/filaments').json())
    assert inventory_client.get(f'/api/spools/{sid}').status_code == 404
    assert all(s['id']!=sid for s in inventory_client.get('/api/spools').json())


def test_import_checks_authentication_and_token_write_scope(inventory_client):
    inventory_client.app.dependency_overrides[get_current_user] = lambda: None
    assert inventory_client.post('/api/filaments/import-bambu',json=payload()).status_code == 401
    inventory_client.app.dependency_overrides[get_current_user] = lambda: {**ADMIN,'_auth_kind':'user_api_token','_token_scopes':['read:spools']}
    assert inventory_client.post('/api/filaments/import-bambu',json=payload()).status_code == 403


def test_non_text_colour_facts_do_not_resolve(review_db):
    from modules.inventory.catalogue import matching_entries
    assert matching_entries(review_db,'PLA',123456) == []
