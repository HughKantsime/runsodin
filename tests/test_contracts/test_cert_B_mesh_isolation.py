"""Cert B: 3MF preview extraction is bounded in triangles, time and memory (F02-2.8-30 hardening).

The diamond fan-out package: one mesh with many triangles over 3 vertices, referenced
through levels of two components each, so instances double per level while vertices
stay small. A ~1.6 KB zip expands to hundreds of millions of triangles.
"""
import asyncio
import os
import resource
import threading
import time
import zipfile

import pytest

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def diamond(path, levels=13, triangles=20000):
    tris = '<triangle v1="0" v2="1" v3="2"/>' * triangles
    objects = [f'<object id="1" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
               f'<vertex x="0" y="1" z="0"/></vertices><triangles>{tris}</triangles></mesh></object>']
    for level in range(2, levels + 2):
        objects.append(f'<object id="{level}" type="model"><components><component objectid="{level - 1}"/>'
                       f'<component objectid="{level - 1}"/></components></object>')
    model = f'<model xmlns="{CORE}"><resources>{"".join(objects)}</resources><build><item objectid="{levels + 1}"/></build></model>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("3D/3dmodel.model", model)
    return str(path)


def cube(path):
    v = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0), (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
    t = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    mesh = ("<mesh><vertices>" + "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in v) + "</vertices><triangles>"
            + "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in t) + "</triangles></mesh>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", f'<model xmlns="{CORE}"><resources><object id="1" type="model">{mesh}</object></resources><build><item objectid="1"/></build></model>')
    return str(path)


def test_diamond_fan_out_is_small_on_disk(tmp_path):
    assert os.path.getsize(diamond(tmp_path / "d.3mf")) < 4096


def test_parser_counts_triangles(tmp_path):
    """In-process parser refuses on the triangle budget (this would take > 6 GB unbounded)."""
    from modules.models_library import threemf_parser
    path = diamond(tmp_path / "d.3mf", levels=8, triangles=20000)  # 256 instances x 20k = 5.1M triangles, 768 vertices
    started = time.perf_counter()
    assert threemf_parser.extract_mesh_from_3mf(path) is None
    assert time.perf_counter() - started < 20


def _isolated(path, **kw):
    from modules.models_library.mesh_isolation import extract_mesh_isolated
    return asyncio.run(extract_mesh_isolated(path, **kw))


def test_isolated_extraction_returns_plain_data(tmp_path):
    mesh = _isolated(cube(tmp_path / "c.3mf"))
    assert mesh["vertex_count"] == 8 and mesh["triangle_count"] == 12
    assert type(mesh["vertices"]) is list and all(type(v) is float for v in mesh["vertices"])
    assert type(mesh["triangles"]) is list and all(type(i) is int for i in mesh["triangles"])
    assert max(mesh["vertices"]) == 10.0 and mesh["triangles"][:3] == [0, 2, 1]


def test_diamond_is_refused_with_bounded_memory_and_no_leftovers(tmp_path):
    before_threads = threading.active_count()
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert _isolated(diamond(tmp_path / "d.3mf")) is None
    after_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    scale = 1 if os.uname().sysname == "Darwin" else 1024  # ru_maxrss: bytes on macOS, KiB on Linux
    assert (after_rss - before_rss) * scale < 200 * 2**20  # the parent never held the geometry
    assert child_peak * scale < 1.5 * 2**30
    assert threading.active_count() == before_threads


def test_timeout_kills_the_worker_process(tmp_path, monkeypatch):
    import subprocess
    from modules.models_library import mesh_isolation
    monkeypatch.setenv("ODIN_MESH_WORKER_TEST_SLEEP", "30")  # the worker sleeps before parsing
    started = time.perf_counter()
    assert _isolated(cube(tmp_path / "c.3mf"), timeout=0.5) is None
    assert time.perf_counter() - started < 5
    assert mesh_isolation.LAST_WORKER_PID is not None
    with pytest.raises(ProcessLookupError):
        os.kill(mesh_isolation.LAST_WORKER_PID, 0)  # killed and reaped


def test_workers_are_a_small_pool(tmp_path, monkeypatch):
    from modules.models_library import mesh_isolation
    monkeypatch.setenv("ODIN_MESH_WORKER_TEST_SLEEP", "0.4")
    path = cube(tmp_path / "c.3mf")

    async def run():
        peak = 0
        tasks = [asyncio.create_task(mesh_isolation.extract_mesh_isolated(path)) for _ in range(6)]
        while not all(t.done() for t in tasks):
            peak = max(peak, mesh_isolation.running_workers())
            await asyncio.sleep(0.05)
        return peak, [t.result() for t in tasks]
    peak, results = asyncio.run(run())
    assert peak <= mesh_isolation.MAX_WORKERS == 2
    assert all(r and r["triangle_count"] == 12 for r in results)
