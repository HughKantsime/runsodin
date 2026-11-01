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


@pytest.mark.parametrize('filament', ['', '<filament id="1" type="PLA" used_g="0" used_m="0"/>'])
def test_empty_or_zero_usage_entries_refused(importer, review_db, filament):
    metadata = ('<config><plate><metadata key="index" value="1"/>' + filament + '</plate>'
                '<plate><metadata key="index" value="10"/><filament id="1" type="PLA"/></plate></config>')
    before = snapshot(review_db)
    response = upload(importer, slice_package(metadata))
    assert response.status_code == 400, response.text
    assert snapshot(review_db) == before
    assert not list(importer[2].iterdir())


def test_one_filament_with_missing_usage_preserves_missing_values(importer):
    metadata = ('<config><plate><metadata key="index" value="1"/><filament id="1" type="PLA"/></plate>'
                '<plate><metadata key="index" value="10"/><filament id="1" type="PLA"/></plate></config>')
    response = upload(importer, slice_package(metadata))
    assert response.status_code == 200, response.text
    for item in response.json()['files']:
        assert len(item['filaments']) == 1
        assert item['filaments'][0]['used_grams'] is None
        assert item['print_time_seconds'] is None
        assert item['total_weight_grams'] is None


@pytest.mark.parametrize('slot', ['0', '17', '64', '01', '-1', '1.0', None])
def test_unsupported_source_filament_slots_refused(importer, review_db, slot):
    slot_attribute = '' if slot is None else f' id="{slot}"'
    metadata = ('<config><plate><metadata key="index" value="1"/>'
                f'<filament{slot_attribute} type="PLA" used_g="1"/></plate>'
                '<plate><metadata key="index" value="10"/><filament id="1" type="PLA"/></plate></config>')
    before = snapshot(review_db)
    response = upload(importer, slice_package(metadata))
    assert response.status_code == 400, response.text
    assert snapshot(review_db) == before
    assert not list(importer[2].iterdir())


@pytest.mark.parametrize('slot', [1, 16])
def test_supported_slot_endpoints_survive_import(importer, slot):
    metadata = ('<config><plate><metadata key="index" value="1"/>'
                f'<filament id="{slot}" type="PLA" used_g="1"/></plate>'
                '<plate><metadata key="index" value="10"/><filament id="1" type="PLA"/></plate></config>')
    response = upload(importer, slice_package(metadata))
    assert response.status_code == 200, response.text
    assert response.json()['files'][0]['filaments'][0]['slot'] == slot


def test_absent_target_nozzle_allows_import(importer, review_db):
    review_db.execute(text('UPDATE printers SET nozzle_diameter=NULL WHERE id=9'))
    review_db.commit()
    response = upload(importer, package(numbers=(1, 10)))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('shortfall', [1, 0])
def test_declared_extraction_space_checked_before_writing(tmp_path, monkeypatch, shortfall):
    from types import SimpleNamespace
    from modules.models_library import plate_import
    archive_path = tmp_path / 'source.3mf'
    archive_path.write_bytes(package(numbers=(1, 10)))
    destination = tmp_path / 'staged'
    destination.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        declared = sum(archive.getinfo(f'Metadata/plate_{number}.gcode').file_size for number in (1, 10))
    required = declared + 128 * 2**20
    checked = []
    def disk_usage(path):
        checked.append(path)
        assert not list(destination.iterdir())
        return SimpleNamespace(free=required - shortfall)
    monkeypatch.setattr(plate_import.shutil, 'disk_usage', disk_usage)
    if shortfall:
        with pytest.raises(ValueError, match='Insufficient storage'):
            plate_import.extract_to(archive_path, destination)
        assert not list(destination.iterdir())
    else:
        result = plate_import.extract_to(archive_path, destination)
        assert result['status'] == 'ok'
        assert len(list(destination.iterdir())) == 2
    assert checked == [destination]
