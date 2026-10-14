"""Real 3MF geometry extraction through the owned mesh HTTP boundary."""
import json
import zipfile

from sqlalchemy import text
from tests.test_contracts.test_model_preview_authorization_behavior import preview_client, token_client, review_db


def test_extracted_triangle_survives_persistence_and_mesh_http(preview_client, review_db, tmp_path):
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    path = tmp_path / 'owned-triangle.3mf'
    model = '''<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources><object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>'''
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('3D/3dmodel.model', model)
    geometry = extract_mesh_from_3mf(str(path))
    assert geometry['vertices'] == [0, 0, 0, 10, 0, 0, 0, 10, 0]
    assert geometry['triangles'] == [0, 1, 2]
    review_db.execute(text('UPDATE print_files SET mesh_data=:mesh WHERE id=21'), {'mesh': json.dumps(geometry)})
    review_db.commit()
    client, issue = preview_client
    for route in ['/print-files/21/mesh', '/models/31/mesh']:
        response = client.get(route, headers=issue(['read:models']))
        assert response.status_code == 200
        assert response.json() == geometry


def test_meshless_3mf_has_no_fabricated_geometry(tmp_path):
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    path = tmp_path / 'owned-empty.3mf'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('3D/3dmodel.model', '<model><resources/></model>')
    assert extract_mesh_from_3mf(str(path)) is None
