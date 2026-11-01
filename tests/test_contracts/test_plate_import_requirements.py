"""Imported plate models retain the facts used by scheduling and pricing."""
import io
import json
import zipfile

import pytest
from sqlalchemy import text

from tests.test_contracts.test_plate_import import (  # noqa: F401
    importer, package, review_db, token_client, upload,
)


def _slice_info(content):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package())) as source, zipfile.ZipFile(output, 'w') as target:
        for name in source.namelist():
            target.writestr(name, content if name == 'Metadata/slice_info.config' else source.read(name))
    return output.getvalue()


def _scheduling(importer, monkeypatch):
    from core.registry import registry
    from modules.jobs import register_providers
    from modules.organizations.services import EducationPolicyService
    from modules.models_library.routes import models_crud
    monkeypatch.setattr(registry, '_providers', dict(registry._providers))
    register_providers(registry)
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    importer[0].app.include_router(models_crud.router)


def test_imported_sparse_plate_models_schedule_own_colors_and_petg_cost(importer, review_db, monkeypatch):
    from modules.models_library.models import Model
    from modules.models_library.services import calculate_job_cost
    _scheduling(importer, monkeypatch)
    # Distinct material rates make a PLA fallback observable in real pricing.
    review_db.execute(text("INSERT INTO filament_library(brand,name,material,cost_per_gram) VALUES "
                           "('Fixture','PLA','PLA',0.1),('Fixture','PETG','PETG',0.8)"))
    review_db.commit()
    response = upload(importer)
    assert response.status_code == 200, response.text
    files = response.json()['files']
    expected = [({'slot1': {'color': '#ff0000', 'grams': 3.5}}, 'PLA', 3.5, 120 / 3600),
                ({'slot1': {'color': ''}}, None, 0, None),
                ({'slot1': {'color': '#00ff00', 'grams': 7}}, 'PETG', 7, 600 / 3600)]
    client, issue, _ = importer
    for file, (requirements, material, grams, hours) in zip(files, expected):
        model = review_db.get(Model, file['model_id'])
        assert model.color_requirements == requirements
        assert (model.default_filament_type.value if model.default_filament_type else None) == material
        assert model.total_filament_grams == grams
        assert model.build_time_hours == hours
        cost = calculate_job_cost(review_db, model_id=model.id)[0]
        if material == 'PETG':
            # Explicit quantities and material must produce the same cost as the imported model.
            assert cost == calculate_job_cost(review_db, filament_grams=7, print_hours=600 / 3600,
                                             material_type='PETG')[0]
            assert cost > calculate_job_cost(review_db, filament_grams=7, print_hours=600 / 3600,
                                            material_type='PLA')[0]
        scheduled = client.post(f"/models/{model.id}/schedule?printer_id=9",
                                headers=issue(['write:jobs']), json={})
        assert scheduled.status_code == 200, scheduled.text
        job = review_db.execute(text('SELECT colors_required,estimated_cost,print_file_id FROM jobs WHERE id=:id'),
                                {'id': scheduled.json()['job_id']}).one()
        assert job == (','.join(model.required_colors), cost, file['id'])


@pytest.mark.parametrize('filament,expected_requirements,expected_type', [
    ('<filament id="3" type="PETG" color="#123456"/>',
     {'slot3': {'color': '#123456'}}, 'PETG'),
    ('<filament id="3" used_g="7"/>', {'slot3': {'color': '', 'grams': 7}}, None),
    ('<filament id="3" type="PLA+" color="#123456" used_g="7"/>',
     {'slot3': {'color': '#123456', 'grams': 7}}, 'PLA'),
    ('<filament id="3" type="PETG-CF" color="#123456" used_g="7"/>',
     {'slot3': {'color': '#123456', 'grams': 7}}, 'PETG_CF'),
    ('<filament id="3" type="MysteryBlend" color="#123456" used_g="7"/>',
     {'slot3': {'color': '#123456', 'grams': 7}}, None),
])
def test_missing_and_unsupported_facts_stay_missing_and_models_remain_schedulable(
    importer, review_db, monkeypatch, filament, expected_requirements, expected_type,
):
    from modules.models_library.models import Model
    _scheduling(importer, monkeypatch)
    content = _slice_info(f'<config><plate><metadata key="index" value="1"/><filament id="1"/></plate><plate><metadata key="index" value="4"/><filament id="1"/></plate><plate><metadata key="index" value="10"/>{filament}</plate></config>')
    response = upload(importer, content)
    assert response.status_code == 200, response.text
    files = response.json()['files']
    assert [item['source_plate'] for item in files] == [1, 4, 10]
    for item in files[:2]:
        model = review_db.get(Model, item['model_id'])
        assert model.color_requirements == {'slot1': {'color': ''}} and model.default_filament_type is None
        assert item['total_weight_grams'] is None
    model = review_db.get(Model, files[2]['model_id'])
    assert model.color_requirements == expected_requirements
    assert (model.default_filament_type.value if model.default_filament_type else None) == expected_type
    assert model.build_time_hours is None
    row = review_db.execute(text('SELECT filaments_json FROM print_files WHERE id=:id'),
                            {'id': files[2]['id']}).scalar_one()
    assert json.loads(row) == files[2]['filaments']
    if 'used_g=' not in filament:
        assert files[2]['total_weight_grams'] is None
        assert 'grams' not in model.color_requirements['slot3']
    client, issue, _ = importer
    for path in ('/models', f'/models/{model.id}'):
        listed = client.get(path, headers=issue(['read:models']))
        assert listed.status_code == 200, listed.text
    scheduled = client.post(f'/models/{model.id}/schedule?printer_id=9',
                            headers=issue(['write:jobs']), json={})
    assert scheduled.status_code == 200, scheduled.text
