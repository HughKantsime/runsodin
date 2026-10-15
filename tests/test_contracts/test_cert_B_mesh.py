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


def _write(path, model, parts=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        for name, body in (parts or {}).items():
            z.writestr(name, body)
    return str(path)


def _bounded(fn, seconds=5):
    """Run fn in a thread; fail if it does not finish within `seconds` (no hang, no OOM)."""
    import threading
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("value", fn()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "mesh extraction did not finish in time"
    return out.get("value")


def test_self_referencing_component_cycle_is_refused(tmp_path):
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    model = (f'<model xmlns="{CORE}"><resources><object id="1" type="model">{_mesh()}'
             '<components><component objectid="1"/><component objectid="1"/></components></object></resources>'
             '<build><item objectid="1"/></build></model>')
    assert _bounded(lambda: extract_mesh_from_3mf(_write(tmp_path / "cycle.3mf", model))) is None


def test_cross_part_cycle_is_refused(tmp_path):
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    root = (f'<model xmlns="{CORE}" xmlns:p="{PROD}"><resources><object id="1" type="model"><components>'
            '<component p:path="/3D/Objects/a.model" objectid="2"/></components></object></resources><build><item objectid="1"/></build></model>')
    part = (f'<model xmlns="{CORE}" xmlns:p="{PROD}"><resources><object id="2" type="model">{_mesh()}<components>'
            '<component p:path="/3D/3dmodel.model" objectid="1"/></components></object></resources></model>')
    assert _bounded(lambda: extract_mesh_from_3mf(_write(tmp_path / "cycle2.3mf", root, {"3D/Objects/a.model": part}))) is None


def test_component_fan_out_amplification_has_a_budget(tmp_path):
    """A few hundred bytes that would expand to 10**8 cube instances: no preview, quickly."""
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    fan = "".join('<component objectid="{child}"/>' for _ in range(10))
    objects = [f'<object id="1" type="model">{_mesh()}</object>']
    for level in range(2, 10):
        objects.append(f'<object id="{level}" type="model"><components>{fan.format(child=level - 1)}</components></object>')
    model = f'<model xmlns="{CORE}"><resources>{"".join(objects)}</resources><build><item objectid="9"/></build></model>'
    path = _write(tmp_path / "fan.3mf", model)
    import os
    assert os.path.getsize(path) < 4000  # stored uncompressed; deflated it is a few hundred bytes
    assert _bounded(lambda: extract_mesh_from_3mf(path)) is None


@pytest.mark.parametrize("vertex,transform", [('x="inf" y="0" z="0"', None), ('x="nan" y="0" z="0"', None),
                                              ('x="1e400" y="0" z="0"', None),
                                              ('x="0" y="0" z="0"', "1e308 0 0 0 1 0 0 0 1 1e308 0 0"),
                                              ('x="0" y="0" z="0"', "nan 0 0 0 1 0 0 0 1 0 0 0")])
def test_non_finite_geometry_never_reaches_mesh_data(tmp_path, vertex, transform):
    import json, math
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in CUBE_V[1:])
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in CUBE_T)
    item = f'<item objectid="1" transform="{transform}"/>' if transform else '<item objectid="1"/>'
    model = (f'<model xmlns="{CORE}"><resources><object id="1" type="model"><mesh><vertices><vertex {vertex}/>{verts}</vertices>'
             f'<triangles>{tris}</triangles></mesh></object></resources><build>{item}</build></model>')
    mesh = extract_mesh_from_3mf(_write(tmp_path / "nonfinite.3mf", model))
    if mesh is not None:
        assert all(math.isfinite(v) for v in mesh["vertices"])
        json.dumps(mesh, allow_nan=False)


def test_upload_extracts_mesh_off_the_event_loop_with_a_deadline(monkeypatch):
    import asyncio, threading, time
    from modules.models_library.routes import print_files
    seen = {}

    def slow(path):
        seen["thread"] = threading.current_thread()
        time.sleep(0.3)
        return {"vertices": [0.0], "triangles": []}
    monkeypatch.setattr(print_files, "extract_mesh_from_3mf", slow)

    async def run():
        ticks = 0
        task = asyncio.create_task(print_files._extract_preview_mesh("x.3mf"))
        while not task.done():  # the loop keeps serving other work meanwhile
            ticks += 1
            await asyncio.sleep(0.01)
        return await task, ticks
    result, ticks = asyncio.run(run())
    assert result == {"vertices": [0.0], "triangles": []} and seen["thread"] is not threading.main_thread() and ticks > 5
    monkeypatch.setattr(print_files, "MESH_EXTRACTION_TIMEOUT_SECONDS", 0.05)
    assert asyncio.run(print_files._extract_preview_mesh("x.3mf")) is None
