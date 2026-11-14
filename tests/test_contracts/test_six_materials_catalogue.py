"""Startup catalogue backfill is narrow, audited, atomic and repeatable."""
import json
import pytest
from sqlalchemy import text, event
from core.schema import bootstrap_database
from core.print_payload import material_match
from modules.inventory.catalogue import backfill_catalogue_materials, import_catalogue, parse_catalogue
from modules.inventory.catalogue_slots import slot_catalogue
from modules.inventory.models import FilamentLibrary, Spool
from modules.printers.models import FilamentSlot
from core.base import FilamentType
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_six_materials import SIX

NAMES = ('ASA Aero','ASA-CF','PET-CF','PPA-CF','PPS-CF','TPU for AMS')


def seed(db):
    rows = []
    for n, name in enumerate(NAMES):
        row = FilamentLibrary(brand='Bambu Lab', name=name, material='OTHER', is_custom=False,
            catalogue_type=' '+name.swapcase()+' ', catalogue_code=f'SYNTHETIC-{n}', color_hex='123456')
        db.add(row); db.flush(); rows.append(row.id)
        db.add(Spool(filament_id=row.id, org_id=1))
    for name, material, custom, code, kind in [
        ('Custom','OTHER',True,'custom-code','ASA-CF'),
        ('No code','OTHER',False,None,'ASA-CF'),
        ('Chosen material','ASA',False,'chosen','ASA-CF'),
        ('Unknown','OTHER',False,'unknown','Future material'),
        ('Old type','OTHER',False,'old','PLA Basic'),
    ]:
        db.add(FilamentLibrary(brand='Fixture',name=name,material=material,is_custom=custom,
            catalogue_code=code,catalogue_type=kind))
    db.commit()
    return rows


def test_startup_backfill_links_exact_slot_and_reimport_retains_identity(review_db):
    ids = seed(review_db)
    engine = review_db.get_bind()
    bootstrap_database(engine)
    bootstrap_database(engine)
    review_db.expire_all()
    for row_id, material, name in zip(ids, SIX, NAMES):
        row = review_db.get(FilamentLibrary, row_id)
        spool = review_db.query(Spool).filter_by(filament_id=row_id).one()
        assert material_match(spool.filament.material, material) is True
        slot = review_db.query(FilamentSlot).filter_by(printer_id=9, slot_number=1).first()
        if slot is None:
            slot = FilamentSlot(printer_id=9, slot_number=1); review_db.add(slot)
        slot.filament_type=FilamentType(material.replace('-','_')); slot.color_hex='123456'
        slot.assigned_spool_id=spool.id; slot.tray_observation=None
        review_db.flush()
        assert slot_catalogue(review_db, slot, {'id':3,'role':'admin','group_id':1})['catalogue_name'] == name
        entry = dict(total=1, data=[dict(fila_type=name, fila_color_name={'en':'Reimported'},
            fila_color_code=row.catalogue_code, fila_color=['#123456FF'])])
        assert import_catalogue(review_db, parse_catalogue(json.dumps(entry)))['updated'] == 1
        assert row.material == material and spool.filament_id == row_id
    untouched = review_db.query(FilamentLibrary).filter(FilamentLibrary.brand=='Fixture').all()
    assert {r.name:r.material for r in untouched} == {
        'Custom':'OTHER','No code':'OTHER','Chosen material':'ASA','Unknown':'OTHER','Old type':'OTHER'}
    audits = review_db.execute(text("SELECT entity_id,details FROM audit_logs WHERE action='filament.catalogue_material_recognized' ORDER BY entity_id")).all()
    assert [(row_id,json.loads(detail)) for row_id,detail in audits] == [
        (row_id,{'before':'OTHER','after':material}) for row_id,material in zip(ids,SIX)]


def test_backfill_audit_failure_rolls_back_material_change(review_db):
    ids = seed(review_db)
    engine = review_db.get_bind()
    def refuse(conn, cursor, statement, *args):
        if statement.startswith('INSERT INTO audit_logs'):
            raise RuntimeError('Synthetic audit failure')
    event.listen(engine, 'before_cursor_execute', refuse)
    try:
        with pytest.raises(RuntimeError, match='Synthetic audit failure'):
            with engine.begin() as c: backfill_catalogue_materials(c)
    finally: event.remove(engine, 'before_cursor_execute', refuse)
    review_db.expire_all()
    assert all(review_db.get(FilamentLibrary, i).material=='OTHER' for i in ids)
