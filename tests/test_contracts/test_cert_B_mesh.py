"""Cert B: 3MF mesh extraction for the archive/model 3D preview (F02-2.8-30/33/34).

Bambu Studio / Orca project exports use the 3MF Production extension: 3D/3dmodel.model
holds only <components p:path="/3D/Objects/object_N.model"> and the real <mesh> lives
in the referenced part file (structure copied from the repo's real Bambu Studio
02.05 export tests/fixtures/certification/cert-B/a1-acceptance-owned-project.3mf).
"""
import zipfile

import pytest

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
CUBE_V = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0), (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
CUBE_T = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def _mesh():
    return ("<mesh><vertices>" + "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in CUBE_V) + "</vertices><triangles>"
            + "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in CUBE_T) + "</triangles></mesh>")


def test_plain_3mf_mesh_is_extracted(tmp_path):
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    path = tmp_path / "plain.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", f'<model xmlns="{CORE}"><resources><object id="1" type="model">{_mesh()}</object></resources>'
                                       '<build><item objectid="1"/></build></model>')
    mesh = extract_mesh_from_3mf(str(path))
    assert len(mesh["vertices"]) == 24 and len(mesh["triangles"]) == 36


def test_bambu_studio_production_extension_mesh_is_extracted(tmp_path):
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    path = tmp_path / "bambu-project.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model",
                   f'<model unit="millimeter" xmlns="{CORE}" xmlns:p="{PROD}" requiredextensions="p"><resources>'
                   '<object id="2" type="model"><components><component p:path="/3D/Objects/object_1.model" objectid="1" '
                   'transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object></resources><build><item objectid="2"/></build></model>')
        z.writestr("3D/_rels/3dmodel.model.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/Objects/object_1.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/Objects/object_1.model", f'<model unit="millimeter" xmlns="{CORE}"><resources><object id="1" type="model">{_mesh()}</object></resources></model>')
    mesh = extract_mesh_from_3mf(str(path))
    assert mesh is not None and len(mesh["vertices"]) == 24 and len(mesh["triangles"]) == 36


def test_component_transforms_place_production_parts(tmp_path):
    """Two build items referencing the same part file at different offsets yield two placed cubes."""
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    path = tmp_path / "two-instances.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model",
                   f'<model unit="millimeter" xmlns="{CORE}" xmlns:p="{PROD}" requiredextensions="p"><resources>'
                   '<object id="2" type="model"><components><component p:path="/3D/Objects/object_1.model" objectid="1" '
                   'transform="1 0 0 0 1 0 0 0 1 5 0 0"/></components></object></resources><build>'
                   '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 100 0 0"/><item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 50 0"/></build></model>')
        z.writestr("3D/Objects/object_1.model", f'<model unit="millimeter" xmlns="{CORE}"><resources><object id="1" type="model">{_mesh()}</object></resources></model>')
    mesh = extract_mesh_from_3mf(str(path))
    xs, ys = mesh["vertices"][0::3], mesh["vertices"][1::3]
    assert mesh["vertex_count"] == 16 and mesh["triangle_count"] == 24
    assert (min(xs[:8]), max(xs[:8])) == (105, 115) and (min(ys[:8]), max(ys[:8])) == (0, 10)  # component then item offset
    assert (min(xs[8:]), max(xs[8:])) == (5, 15) and (min(ys[8:]), max(ys[8:])) == (50, 60)
    assert max(mesh["triangles"]) == 15 and min(mesh["triangles"][36:]) == 8


def test_real_bambu_studio_project_export_has_previewable_mesh():
    """The committed Bambu Studio 02.05 project export (geometry in 3D/Objects/object_1.model)."""
    from pathlib import Path
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    source = Path(__file__).resolve().parents[1] / "fixtures/certification/cert-B/a1-acceptance-owned-project.3mf"
    with zipfile.ZipFile(source) as z:
        assert "<mesh" not in z.read("3D/3dmodel.model").decode() and "<mesh" in z.read("3D/Objects/object_1.model").decode()
    mesh = extract_mesh_from_3mf(str(source))
    assert (mesh["vertex_count"], mesh["triangle_count"]) == (8, 12)
