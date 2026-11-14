"""Second-review regressions, using only invented catalogue entries."""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from modules.inventory.catalogue_slots import enrich_catalogue_slots, select_spool_filament
from modules.inventory.slot_assignments import assign_slot
from tests.test_contracts.test_bambu_catalogue import ADMIN, payload, upload
from tests.test_contracts.test_bambu_catalogue_review import setup_choice
from tests.test_contracts.test_inventory_claim_acceptance import inventory_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


@pytest.mark.parametrize('material,color', [
    ('PLA', '#1234ab'), ('PLA', '1234ab'), ('PLA', '#1234ab80'), (' pla ', '1234AB'),
])
def test_offered_choice_uses_same_matching_rule(inventory_client, review_db, material, color):
    printer, slot, spool, target = setup_choice(inventory_client, review_db)
    target.material, target.color_hex = material, color
    review_db.commit()
    enrich_catalogue_slots(review_db, [printer], ADMIN)
    assert target.id in [c['id'] for c in slot.catalogue_choices]
    response = inventory_client.post(f'/api/printers/{printer.id}/slots/1/catalogue',
        json={'spool_id': spool.id, 'filament_id': target.id})
    assert response.status_code == 200, response.text
    review_db.refresh(spool)
    assert spool.filament_id == target.id


@pytest.mark.parametrize('route', ['patch', 'choice'])
@pytest.mark.parametrize('raw', [
    json.dumps({'pending': True}), '{', '[]', 'null', json.dumps({'pending': 'false'}),
])
def test_relabel_refuses_pending_or_unknown_state(inventory_client, review_db, route, raw):
    printer, slot, spool, target = setup_choice(inventory_client, review_db)
    slot.tray_observation = raw
    review_db.commit()
    if route == 'patch':
        response = inventory_client.patch(f'/api/spools/{spool.id}',
            json={'filament_id': target.id, 'notes': 'must roll back'})
    else:
        response = inventory_client.post(f'/api/printers/{printer.id}/slots/1/catalogue',
            json={'spool_id': spool.id, 'filament_id': target.id})
    assert response.status_code == 409, response.text
    assert ('spool-change state' if raw != json.dumps({'pending': True}) else 'confirm') in response.json()['detail']
    review_db.refresh(spool)
    assert spool.filament_id == 990 and spool.notes is None
    assert review_db.execute(text("SELECT count(*) FROM audit_logs WHERE action='spool.catalogue_selected'")).scalar() == 0


def test_relabel_serializes_concurrent_reassignment(inventory_client, review_db):
    _, slot, spool, target = setup_choice(inventory_client, review_db)
    # Observe the selection before it flushes: the existing graph lock must already
    # stop a second writer from detaching this spool, rather than relying on UPDATE.
    select_spool_filament(review_db, spool, target.id, ADMIN)
    with Session(review_db.bind) as other:
        other.execute(text('PRAGMA busy_timeout=0'))
        other_slot = other.get(type(slot), slot.id)
        with pytest.raises(HTTPException) as exc:
            assign_slot(other, other_slot, None, ADMIN)
        assert exc.value.status_code == 409
    review_db.commit()
    review_db.refresh(slot)
    assert slot.assigned_spool_id == spool.id


def test_reimport_changed_material_skips_entire_row(inventory_client, review_db):
    _, _, spool, target = setup_choice(inventory_client, review_db)
    spool.filament_id = target.id
    target.cost_per_gram = 0.123
    review_db.commit()
    before = {c.name: getattr(target, c.name) for c in target.__table__.columns}
    data = payload()
    data['data'][0].update(fila_type='PETG HF', fila_color=['#789ABC80'])
    response = upload(inventory_client, data)
    assert response.status_code == 200, response.text
    review_db.refresh(target)
    assert {c.name: getattr(target, c.name) for c in target.__table__.columns} == before
    assert response.json()['updated'] == 2
    assert response.json()['skipped'] == [{'catalogue_code': target.catalogue_code, 'reason': 'type_changed'}]


def test_the_spool_picker_knows_exactly_the_materials_the_backend_recognises():
    """The picker filters by material in the browser; its list must be the backend's list."""
    import re
    from core import material_identity
    source = (Path(__file__).resolve().parents[2] / 'frontend/src/components/inventory/SpoolEditModals.tsx').read_text()
    listed = re.search(r"const catalogueMaterials = new Set\('([^']+)'\.split\(' '\)\)", source)
    assert listed, 'catalogueMaterials is no longer a space-separated literal: update this test with it'
    assert set(listed.group(1).split(' ')) == set(material_identity._MATERIALS)


def test_a_respelled_stored_material_is_not_a_type_change(client_db=None):
    from modules.inventory import catalogue
    from core.material_identity import canonical_reported_material
    assert canonical_reported_material(' pla ') == canonical_reported_material('PLA') == 'PLA'
    source = Path(catalogue.__file__).read_text()
    assert "canonical_reported_material(row.material) != canonical_reported_material(entry['material'])" in source
