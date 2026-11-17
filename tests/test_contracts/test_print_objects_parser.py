"""Synthetic two-plate fixture plus real stored fixtures; no slicer or printer required."""
import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from modules.models_library import mesh_isolation, threemf_parser


def synthetic_members(ids=(15, 300, 79, 90000), labels='true'):
    plates, members = [], {}
    for index in (1, 2):
        objects = ''.join(f'<object identify_id="{i}" name="copy.stl" skipped="false"/>' for i in ids)
        plates.append(f'<plate><metadata key="index" value="{index}"/><metadata key="label_object_enabled" value="{labels}"/>{objects}</plate>')
        image = Image.new('RGBA', (100, 80))
        for n, identifier in enumerate(ids[:4]):
            if type(identifier) is not int:
                continue
            for x in range(5 + n * 20, 15 + n * 20):
                for y in range(10 * index, 10 * index + 8):
                    image.putpixel((x, y), (identifier & 255, identifier >> 8 & 255, identifier >> 16 & 255, 255))
        out = io.BytesIO(); image.save(out, 'PNG')
        members[f'Metadata/pick_{index}.png'] = out.getvalue()
        members[f'Metadata/plate_{index}.json'] = '{"bbox_objects":[{"id":999,"name":"wrong-id"}]}'
    members['Metadata/slice_info.config'] = '<config>' + ''.join(plates) + '</config>'
    return members


def package(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_STORED) as zf:
        for name, value in members.items():
            zf.writestr(name, value)
    return out.getvalue()


def parse(members):
    with zipfile.ZipFile(io.BytesIO(package(members))) as zf:
        return threemf_parser.plate_objects_from_archive(zf)


def test_synthetic_two_plate_child(tmp_path):
    path = tmp_path / 'synthetic.3mf'; path.write_bytes(package(synthetic_members()))
    result = mesh_isolation.plate_objects_blocking(path)
    assert result.status == 'ok'
    assert [p['index'] for p in result.objects['plates']] == [1, 2]
    for index, plate in enumerate(result.objects['plates'], 1):
        assert plate['label_object_enabled'] is True
        assert [o['identify_id'] for o in plate['objects']] == [15, 300, 79, 90000]
        assert all(o['name'] == 'copy.stl' and o['skipped_at_slice'] is False for o in plate['objects'])
        assert plate['objects'][1]['bounds'] == dict(x=.25, y=index/8, w=.1, h=.1)
        assert plate['aspect'] == 1.25


@pytest.mark.parametrize('file,identifier', [('telemetry/x1c-pla-source-sanitized.3mf', 15),
    ('certification/cert-B/a1-acceptance-coupon.gcode.3mf', 79)])
def test_existing_fixtures_in_child(file, identifier):
    result = mesh_isolation.plate_objects_blocking(Path(__file__).parents[1] / 'fixtures' / file)
    assert result.status == 'ok'
    plate = result.objects['plates'][0]
    assert plate['label_object_enabled'] is False
    [obj] = plate['objects']
    assert obj['identify_id'] == identifier and obj['name'] == 'a1-acceptance-coupon-ascii.stl'
    assert obj['bounds'] == dict(x=246/512, y=246/512, w=20/512, h=20/512)


@pytest.mark.parametrize('ids', [(1, 1), (1, 'bad'), (0, 2), (16777216, 2), tuple(range(1, 301))])
def test_invalid_ids_never_produce_a_partial_plate(ids):
    assert all(p['objects'] == [] for p in parse(synthetic_members(ids))['plates'])


@pytest.mark.parametrize('picture', [None, b'not PNG', b'x' * 262145, 'oversize_dimensions'])
def test_missing_or_refused_picture_preserves_all_names_without_bounds(picture):
    members = synthetic_members()
    if picture == 'oversize_dimensions':
        out = io.BytesIO(); Image.new('RGBA', (1025, 1)).save(out, 'PNG'); picture = out.getvalue()
    for index in (1, 2):
        members.pop(f'Metadata/pick_{index}.png')
        if picture is not None:
            members[f'Metadata/pick_{index}.png'] = picture
    for plate in parse(members)['plates']:
        assert len(plate['objects']) == 4 and all(o['bounds'] is None for o in plate['objects'])


@pytest.mark.parametrize('content', [None, '<broken>', '<config>' + ' ' * (4*2**20+1) + '</config>'])
def test_missing_or_oversized_xml(content):
    assert parse({} if content is None else {'Metadata/slice_info.config': content}) == {'plates': []}


def test_hidden_objects_and_skipped_at_slice_are_omitted():
    members = synthetic_members((15, 300, 79, 90000, 33))
    members['Metadata/slice_info.config'] = members['Metadata/slice_info.config'].replace('identify_id="300" name="copy.stl" skipped="false"', 'identify_id="300" name="copy.stl" skipped="true"')
    assert [o['identify_id'] for o in parse(members)['plates'][0]['objects']] == [15, 79, 90000]


@pytest.mark.parametrize('kind', ['oversize_xml', 'duplicate_ids', 'missing', 'bad_zip'])
def test_hostile_packages_through_bounded_child(tmp_path, kind):
    members = synthetic_members((15,15) if kind == 'duplicate_ids' else (15,300,79,90000))
    if kind == 'oversize_xml': members['Metadata/slice_info.config'] = ' ' * (4*2**20+1)
    if kind == 'missing': members = {}
    path=tmp_path/'hostile.3mf'
    path.write_bytes(b'not zip' if kind == 'bad_zip' else package(members))
    result=mesh_isolation.plate_objects_blocking(path)
    if kind == 'bad_zip':
        assert result.status=='bad_zip'
    else:
        assert result.status=='ok'
        assert all(p['objects']==[] for p in result.objects['plates'])
