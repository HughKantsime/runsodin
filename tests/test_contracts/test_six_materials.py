"""Exact six-material identities; existing readers and restrictive Education policy."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text

from core.base import FilamentType, slot_filament_value
from core.material_identity import _MATERIALS
from core.print_payload import material_match, validate_mapping
from modules.printers.bambu_integration import map_bambu_filament_type
from modules.printers.kobra_s1 import verify_mapping, S1Refusal
from modules.printers.schemas import FilamentSlotUpdate
from tests.test_contracts.test_education_review_workflow import review_db

SIX = ('ASA-AERO', 'ASA-CF', 'PET-CF', 'PPA-CF', 'PPS-CF', 'TPU-AMS')
PAIRS = [('ASA-AERO', 'ASA'), ('ASA-CF', 'ASA'), ('PET-CF', 'PETG-CF'),
         ('PPA-CF', 'PA-CF'), ('PPA-CF', 'NYLON_CF'), ('TPU-AMS', 'TPU'), ('PPS-CF', 'PPS')]
PAIRS += [('PPS-CF', other) for other in sorted(_MATERIALS - {'PPS-CF'})]
NEGATIVES = PAIRS + [(b, a) for a, b in PAIRS]


@pytest.mark.parametrize('source,target,accepted', [(m, m, True) for m in SIX] + [(a,b,False) for a,b in NEGATIVES])
def test_exact_mapping_and_kobra(source, target, accepted):
    assert (material_match(source, target) is True) is accepted
    mapping = lambda: validate_mapping([{'source_slot': 0, 'target_slot': 1}],
        json.dumps([{'slot': 1, 'type': source}]), {1: target}, 'bambu')
    kobra = lambda: verify_mapping([{'type': source}],
        [{'type': target, 'usable': True, 'color': '123456'}], [0])
    if accepted:
        assert mapping() == kobra() == [0]
    else:
        with pytest.raises(HTTPException): mapping()
        with pytest.raises(S1Refusal, match='s1_material_mismatch'): kobra()


@pytest.mark.parametrize('material', SIX)
def test_converters_and_input_boundary(material):
    value = material.replace('-', '_')
    assert FilamentType.from_bambu_code(material).value == value
    assert slot_filament_value(material) == value
    assert map_bambu_filament_type(material) == value
    from modules.printers.telemetry.bambu.ams_evidence import parse_raw_ams_facts
    fact, = parse_raw_ams_facts({'ams': {'ams': [{'id':'0', 'tray':[{'id':'0', 'tray_type':material}]}]}})
    assert fact.material == material and fact.material_state == 'reported'
    assert FilamentSlotUpdate(filament_type=material).filament_type.value == value
    assert FilamentSlotUpdate(filament_type=value).filament_type.value == value
    from modules.inventory.receiving import ReceiveRolls
    assert ReceiveRolls(brand='Fixture', product_line='Fixture', material=material,
        color_name='Blue', quantity=1, storage_location='Shelf').material == material


@pytest.mark.parametrize('raw', ['ASA Aero', 'TPU for AMS', 'Future polymer', 'PET-CF-other', True])
def test_manual_input_stays_closed(raw):
    with pytest.raises(ValidationError): FilamentSlotUpdate(filament_type=raw)


@pytest.mark.parametrize('material', SIX)
def test_education_preserves_abrasive_refusal(material):
    from modules.printers.education_compatibility import evaluate_education_compatibility, canonical_material
    from tests.test_contracts.test_education_dispatch_policy import _facts, _printer
    facts = _facts(); facts['materials']['value'] = [material]
    result = evaluate_education_compatibility(facts, _printer(active_materials=[material.replace('-', '_')]))
    assert canonical_material(material) == material.replace('-', '_')
    assert not any(r['code'] == 'material_mismatch' for r in result['reasons'])
    if material.endswith('-CF'):
        assert any(r['code'] == 'compatibility_unknown' and r.get('materials') == [material.replace('-', '_')]
                   for r in result['reasons'])
    else:
        parent = 'ASA' if material == 'ASA-AERO' else 'TPU'
        facts['materials']['value'] = [parent]
        baseline = evaluate_education_compatibility(facts, _printer(active_materials=[parent]))
        assert result['reasons'] == baseline['reasons'] == []


def test_startup_repairs_six_free_text_values_but_never_real_families(review_db):
    from modules.inventory.slot_material_repair import repair_slot_materials
    labels = SIX + ('ASA', 'TPU', 'PETG_CF', 'NYLON_CF')
    review_db.execute(text('DELETE FROM filament_slots WHERE printer_id=9'))
    for slot, material in enumerate(labels, 1):
        review_db.execute(text('INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,:slot,:material)'),
                          dict(slot=slot, material=material))
    repair_slot_materials(review_db.connection())
    repair_slot_materials(review_db.connection())
    actual = review_db.execute(text('SELECT filament_type FROM filament_slots WHERE printer_id=9 ORDER BY slot_number')).scalars().all()
    assert actual == [m.replace('-', '_') for m in labels]
    audits = review_db.execute(text("SELECT details FROM audit_logs WHERE action='slot-material.normalize'")).scalars().all()
    assert len(audits) == 6


@pytest.mark.parametrize('writer', ['manual', 'scan', 'moonraker', 'anycubic', 'bambu_background', 'bambu_sync'])
def test_slot_writers_store_exact_six(review_db, monkeypatch, writer):
    from modules.printers.models import FilamentSlot
    from modules.printers import routes_crud, routes_ams
    from modules.inventory.routes import spool_ops
    from modules.inventory.routes._helpers import ScanAssignRequest
    from modules.printers.monitors import moonraker_monitor, anycubic_lan_monitor
    from modules.printers.telemetry.bambu import session, broker_policy
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    from modules.printers.telemetry import feature_flag
    from modules.printers.monitors.ams_inventory import sync_ams_inventory
    engine = review_db.get_bind()
    user = dict(id=3, role='admin', group_id=1)
    review_db.execute(text("UPDATE printers SET slot_count=4,api_type='bambu',api_host='fixture.local',api_key='fixture' WHERE id=9"))
    review_db.commit()
    monkeypatch.setattr(routes_crud, 'ensure_local_filaments', lambda _: None)
    monkeypatch.setattr(routes_ams, 'ensure_local_filaments', lambda _: None)
    monkeypatch.setattr(routes_ams, 'get_spoolman_url', lambda _: None)
    monkeypatch.setattr(routes_ams.crypto, 'decrypt', lambda _: 'fixture|fixture')
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: True)
    monkeypatch.setattr(broker_policy, 'resolve_bambu_broker_config', lambda *a, **k: SimpleNamespace(host='127.0.0.1', port=1883, use_tls=False))
    monkeypatch.setattr(spool_ops, 'ensure_local_filament_management', lambda _: None)
    monkeypatch.setattr(moonraker_monitor, 'engine', engine)
    monkeypatch.setattr(anycubic_lan_monitor, 'engine', engine)
    for material in SIX:
        # Exercise creation as well as replacement without stale ORM identities.
        for existing in (False, True):
            if not existing:
                review_db.execute(text('DELETE FROM filament_slots WHERE printer_id=9'))
                if writer in ('manual',):
                    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,1,'ASA')"))
            review_db.commit(); review_db.expunge_all()
            if writer == 'manual':
                routes_crud.update_filament_slot(9, 1, FilamentSlotUpdate(filament_type=material), current_user=user, db=review_db)
            elif writer == 'scan':
                from modules.inventory.models import FilamentLibrary, Spool
                filament = FilamentLibrary(brand='Fixture', name=material, material=material)
                review_db.add(filament); review_db.flush()
                spool = Spool(filament_id=filament.id, qr_code=f'six-{filament.id}', org_id=1)
                review_db.add(spool); review_db.commit()
                assert spool_ops.scan_assign_spool(ScanAssignRequest(qr_code=spool.qr_code, printer_id=9, slot=1), current_user=user, db=review_db).success
            elif writer == 'moonraker':
                monitor = object.__new__(moonraker_monitor.MoonrakerMonitor)
                monitor.printer_id=9; monitor.name='Fixture'
                monitor._sync_filament_slots([SimpleNamespace(gate=0, material=material, color_hex=None, name='')])
            elif writer == 'anycubic':
                anycubic_lan_monitor.sync_slots(9, [{'id':0, 'slots':[{'index':0, 'type':material}]}])
            else:
                units = [{'id':'0', 'tray':[{'id':'0', 'tray_type':material, 'tray_color':'123456FF'}]}]
                if writer == 'bambu_background':
                    with engine.begin() as c: sync_ams_inventory(c.connection, 9, units)
                else:
                    section = BambuPrintSection.model_validate({'ams':{'ams':units}})
                    monkeypatch.setattr(session, 'read_status_once', lambda *a, **k: SimpleNamespace(success=True, section=section))
                    routes_ams.sync_ams_state(9, current_user=user, db=review_db)
            review_db.expire_all()
            slot = review_db.query(FilamentSlot).filter_by(printer_id=9, slot_number=1).one()
            assert slot.filament_type.value == material.replace('-', '_'), (writer, material, existing)
            review_db.commit()


@pytest.mark.parametrize('label,stored', [
    ('PLA','PLA'), ('PLA-S','PLA_SUPPORT'), ('PLA-CF','PLA_CF'),
    ('PLA-SILK','PLA'), ('PLA-MATTE','PLA'), ('PLA-AERO','PLA'),
    ('PETG','PETG'), ('PETG-CF','PETG_CF'), ('ABS','ABS'), ('ABS-GF','OTHER'),
    ('ASA','ASA'), ('TPU','TPU'), ('TPU-95A','OTHER'), ('TPU-83A','OTHER'),
    ('PA','PA'), ('PA-CF','NYLON_CF'), ('PA-GF','NYLON_GF'), ('PA6','PA'),
    ('PA6-CF','NYLON_CF'), ('PA6-GF','NYLON_GF'), ('PA12-CF','NYLON_CF'),
    ('PC','PC'), ('PC-ABS','PC_ABS'), ('PC-CF','PC_CF'), ('PVA','PVA'),
    ('HIPS','HIPS'), ('SUPPORT','SUPPORT'), ('SUPPORT-W','PVA'), ('SUPPORT-G','HIPS'),
])
def test_existing_labels_keep_their_exact_identity_and_slot_conversion(label, stored):
    from core.material_identity import canonical_reported_material
    assert canonical_reported_material(' '+label.lower().replace('-', '_')+' ') == label
    assert material_match(label, label) is True
    assert slot_filament_value(label) == stored
