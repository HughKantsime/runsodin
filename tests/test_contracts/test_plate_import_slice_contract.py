"""Sliced imports need per-plate material evidence and supported slot identities."""
import io
import zipfile

import pytest
from sqlalchemy import text

from tests.test_contracts.test_plate_import import (  # noqa: F401
    importer, review_db, token_client, package, snapshot, upload,
)


def slice_package(metadata):
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package(numbers=(1, 10)))) as source, zipfile.ZipFile(stream, 'w') as output:
        for name in source.namelist():
            if name == 'Metadata/slice_info.config':
                if metadata is not None:
                    output.writestr(name, metadata)
            else:
                output.writestr(name, source.read(name))
    return stream.getvalue()


@pytest.mark.parametrize('metadata', [None, '<config/>', '<config><plate><metadata key="index" value="1"/></plate></config>'])
def test_missing_slice_entry_refused_without_persistence(importer, review_db, metadata):
    before = snapshot(review_db)
    response = upload(importer, slice_package(metadata))
    assert response.status_code == 400, response.text
    assert snapshot(review_db) == before
    assert not list(importer[2].iterdir())


def test_present_empty_entries_preserve_missing_values(importer):
    response = upload(importer, slice_package('<config><plate><metadata key="index" value="1"/></plate>'
                                             '<plate><metadata key="index" value="10"/></plate></config>'))
    assert response.status_code == 200, response.text
    for item in response.json()['files']:
        assert item['filaments'] == []
        assert item['print_time_seconds'] is None
        assert item['total_weight_grams'] is None


@pytest.mark.parametrize('slot', ['0', '17', '64', '01', '-1', '1.0', None])
def test_unsupported_source_filament_slots_refused(importer, review_db, slot):
    slot_attribute = '' if slot is None else f' id="{slot}"'
    metadata = ('<config><plate><metadata key="index" value="1"/>'
                f'<filament{slot_attribute} type="PLA" used_g="1"/></plate>'
                '<plate><metadata key="index" value="10"/></plate></config>')
    before = snapshot(review_db)
    response = upload(importer, slice_package(metadata))
    assert response.status_code == 400, response.text
    assert snapshot(review_db) == before
    assert not list(importer[2].iterdir())


@pytest.mark.parametrize('slot', [1, 16])
def test_supported_slot_endpoints_survive_import(importer, slot):
    metadata = ('<config><plate><metadata key="index" value="1"/>'
                f'<filament id="{slot}" type="PLA" used_g="1"/></plate>'
                '<plate><metadata key="index" value="10"/></plate></config>')
    response = upload(importer, slice_package(metadata))
    assert response.status_code == 200, response.text
    assert response.json()['files'][0]['filaments'][0]['slot'] == slot


def test_absent_target_nozzle_allows_import(importer, review_db):
    review_db.execute(text('UPDATE printers SET nozzle_diameter=NULL WHERE id=9'))
    review_db.commit()
    response = upload(importer, package(numbers=(1, 10)))
    assert response.status_code == 200, response.text
