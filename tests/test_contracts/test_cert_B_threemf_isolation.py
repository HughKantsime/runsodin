"""Cert B round 5: untrusted 3MF uploads are parsed only in the bounded child.

* the API process never parses the package (zip directory, XML, plate JSON, mesh);
* float32 overflow or non-finite geometry means no preview, never a 500;
* the pool is install-wide (lock files shared by every uvicorn worker), the wait
  queue is bounded (429 + Retry-After when full) and one deadline covers waiting
  and running;
* the child gets a minimal environment, CPU and memory limits, and dies with its
  parent on Linux; its output is decoded in a worker thread.
"""
import array
import os
import struct
import subprocess
import sys
import textwrap
import threading
import time
import zipfile
from pathlib import Path

import pytest

from tests.test_contracts.test_print_file_upload_ownership_behavior import upload, upload_client  # noqa: F401
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db  # noqa: F401

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
FIXTURE = Path("tests/fixtures/release_gate/candidate-calibration-cube.3mf")


@pytest.fixture(autouse=True)
def private_run_dir(tmp_path, monkeypatch):
    run = tmp_path / "run"
    monkeypatch.setenv("ODIN_RUN_DIR", str(run))
    return run


def _model(path, vertex):
    verts = f'<vertex {vertex}/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>'
    model = (f'<model xmlns="{CORE}"><resources><object id="1" type="model"><mesh><vertices>{verts}</vertices>'
             '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>'
             '<build><item objectid="1"/></build></model>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)
    return str(path)


def _hold_locks(run_dir, names, seconds):
    """Another process (another uvicorn worker) holds these lock files."""
    script = textwrap.dedent(f"""
        import fcntl, os, sys, time
        fds = []
        for name in {names!r}:
            fd = os.open(os.path.join({str(run_dir)!r}, name), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX); fds.append(fd)
        print("held", flush=True)
        time.sleep({seconds})
    """)
    os.makedirs(run_dir, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    return holder


# --------------------------------------------------------------------------- float32 / non-finite

def test_float32_overflow_means_no_preview_and_keeps_metadata(tmp_path):
    from modules.models_library import mesh_isolation
    with zipfile.ZipFile(FIXTURE) as source, zipfile.ZipFile(tmp_path / "big.3mf", "w") as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == "3D/3dmodel.model":
                data = data.replace(b'x="', b'x="1e39', 1) if b'x="' in data else data
            target.writestr(item, data)
    analysis = mesh_isolation.analyze_blocking(tmp_path / "big.3mf")
    assert analysis.status == "ok" and analysis.metadata and analysis.mesh is None


def test_encode_refuses_float32_overflow_and_decode_refuses_non_finite():
    from modules.models_library.mesh_isolation import decode_mesh, encode_mesh, _MAGIC
    with pytest.raises((OverflowError, ValueError)):
        encode_mesh({"vertices": [1e39, 0.0, 0.0], "triangles": [0, 0, 0]})
    verts = array.array("f", [float("inf"), 0.0, 0.0])
    tris = array.array("I", [0, 0, 0])
    blob = _MAGIC + struct.pack("<II", 3, 3) + verts.tobytes() + tris.tobytes()
    assert decode_mesh(blob) is None


def test_overflowing_mesh_from_the_worker_is_no_preview(tmp_path):
    from modules.models_library.mesh_isolation import analyze_blocking
    analysis = analyze_blocking(_model(tmp_path / "o.3mf", 'x="1e39" y="0" z="0"'), mode="mesh")
    assert analysis.mesh is None


# --------------------------------------------------------------------------- never parsed in the API process

@pytest.fixture
def parent_cannot_parse(monkeypatch):
    """Any 3MF parsing in this (API) process fails the test."""
    import zipfile as zipfile_module
    from defusedxml import ElementTree
    from modules.models_library import threemf_parser, print_file_meta

    def refuse(*a, **k):
        raise AssertionError("3MF parsed in the API process")
    for module, name in ((threemf_parser, "parse_3mf"), (threemf_parser, "extract_project_name"),
                         (threemf_parser, "extract_objects_from_plate"), (threemf_parser, "extract_mesh_from_3mf"),
                         (print_file_meta, "extract_print_file_meta"), (ElementTree, "fromstring"),
                         (ElementTree, "parse")):
        monkeypatch.setattr(module, name, refuse)
    real_zip = zipfile_module.ZipFile

    class GuardedZip(real_zip):
        def __init__(self, file, mode="r", *a, **k):
            if mode == "r" and str(file).endswith(".3mf"):
                refuse()
            super().__init__(file, mode, *a, **k)
    monkeypatch.setattr(zipfile_module, "ZipFile", GuardedZip)


def test_upload_route_parses_only_in_the_child(upload_client, review_db, parent_cannot_parse):
    client, issue, storage = upload_client
    response = upload(client, issue(["write:models"]), "Isolated.3mf", FIXTURE.read_bytes())
    assert response.status_code == 200, response.text
    from sqlalchemy import text
    row = review_db.execute(text("SELECT project_name,compatibility_facts_json,plate_count FROM print_files WHERE id=:id"),
                            {"id": response.json()["id"]}).one()
    assert row[0] and row[1] and row[2] >= 1


def test_education_metadata_parses_only_in_the_child(parent_cannot_parse):
    from modules.organizations.education_submission_service import _parse_3mf_isolated
    metadata = _parse_3mf_isolated(FIXTURE)
    assert metadata is not None and metadata.project_name == "ODIN Candidate Upload Cube"


@pytest.mark.parametrize("content,detail", [(b"not a zip", "bad zip"), (None, "500 MB")])
def test_container_refusals_come_from_the_child(upload_client, tmp_path, content, detail):
    client, issue, _ = upload_client
    if content is None:  # a zip bomb: declared expansion over 500 MB
        path = tmp_path / "bomb.3mf"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("3D/3dmodel.model", b"\0" * (501 * 2**20))
        content = path.read_bytes()
    response = upload(client, issue(["write:models"]), "refused.3mf", content)
    assert response.status_code == 400 and detail in response.text


# --------------------------------------------------------------------------- install-wide pool, bounded queue, one deadline

def test_run_slots_are_install_wide(private_run_dir):
    from modules.models_library import mesh_isolation
    holder = _hold_locks(private_run_dir, [f"threemf-run-{i}.lock" for i in range(mesh_isolation.MAX_WORKERS)], 1.5)
    try:
        started = time.monotonic()
        with pytest.raises(mesh_isolation.Busy):
            mesh_isolation.analyze_blocking(FIXTURE, timeout=0.5)  # other workers hold both slots
        assert time.monotonic() - started < 1.2
    finally:
        holder.wait()
    assert mesh_isolation.analyze_blocking(FIXTURE).status == "ok"  # slots freed


def test_full_wait_queue_is_429_with_retry_after(private_run_dir):
    import asyncio
    from fastapi import HTTPException
    from modules.models_library import mesh_isolation
    from modules.models_library.routes import print_files
    names = [f"threemf-run-{i}.lock" for i in range(mesh_isolation.MAX_WORKERS)]
    names += [f"threemf-wait-{i}.lock" for i in range(mesh_isolation.MAX_WAITERS)]
    holder = _hold_locks(private_run_dir, names, 3)
    try:
        started = time.monotonic()
        with pytest.raises(HTTPException) as refused:
            asyncio.run(print_files._analyze_3mf_upload(str(FIXTURE)))
        assert time.monotonic() - started < 0.5  # refused at once, not after the deadline
        assert refused.value.status_code == 429 and int(refused.value.headers["Retry-After"]) > 0
    finally:
        holder.kill(); holder.wait()


def test_one_deadline_covers_waiting_and_running(private_run_dir, monkeypatch):
    from modules.models_library import mesh_isolation
    holder = _hold_locks(private_run_dir, [f"threemf-run-{i}.lock" for i in range(mesh_isolation.MAX_WORKERS)], 0.6)
    monkeypatch.setenv("ODIN_MESH_WORKER_TEST_SLEEP", "5")
    try:
        started = time.monotonic()
        analysis = mesh_isolation.analyze_blocking(FIXTURE, timeout=1.2)
        elapsed = time.monotonic() - started
    finally:
        holder.wait()
    assert analysis.status == "failed" and elapsed < 1.8  # 0.6 s waiting + the rest of the same budget


# --------------------------------------------------------------------------- the child: environment, limits, decoding

def test_child_environment_carries_no_secrets(monkeypatch):
    from modules.models_library import mesh_isolation
    monkeypatch.setenv("JWT_SECRET_KEY", "parent-secret")
    monkeypatch.setenv("ENCRYPTION_KEY", "parent-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:parent-secret@db/odin")
    seen = []
    real = subprocess.Popen

    def recording(argv, **kwargs):
        seen.append(kwargs["env"])
        return real(argv, **kwargs)
    monkeypatch.setattr(mesh_isolation.subprocess, "Popen", recording)
    assert mesh_isolation.analyze_blocking(FIXTURE).status == "ok"  # works with nothing inherited
    assert seen and all("parent-secret" not in value for env in seen for value in env.values())
    assert set(seen[0]) <= {"PATH", "PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "LC_ALL", "LANG",
                            "ODIN_MESH_WORKER_TEST_SLEEP"}


def test_child_limits_cpu_and_dies_with_its_parent_on_linux(monkeypatch):
    import ctypes
    import resource
    import signal
    from modules.models_library import mesh_worker
    limits, prctl = [], []
    monkeypatch.setattr(resource, "setrlimit", lambda which, value: limits.append((which, value)))

    class FakeLibc:
        def prctl(self, option, value):
            prctl.append((option, value))
            return 0
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: FakeLibc())
    monkeypatch.setattr(mesh_worker.sys, "platform", "linux")
    mesh_worker._limit(2**30, 7)
    assert (resource.RLIMIT_CPU, (7, 8)) in limits and (resource.RLIMIT_AS, (2**30, 2**30)) in limits
    assert prctl == [(1, signal.SIGKILL)]  # PR_SET_PDEATHSIG


def test_output_is_decoded_off_the_event_loop(monkeypatch):
    import asyncio
    from modules.models_library import mesh_isolation
    threads = []
    real = mesh_isolation.decode_mesh

    def recording(blob):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(blob)
    monkeypatch.setattr(mesh_isolation, "decode_mesh", recording)
    analysis = asyncio.run(mesh_isolation.analyze_3mf_isolated(FIXTURE))
    assert analysis.status == "ok" and threads == [False]


def test_education_upload_is_429_when_the_queue_is_full(private_run_dir):
    from fastapi import HTTPException
    from modules.models_library import mesh_isolation
    from modules.organizations.education_submission_service import _parse_3mf_isolated
    names = [f"threemf-run-{i}.lock" for i in range(mesh_isolation.MAX_WORKERS)]
    names += [f"threemf-wait-{i}.lock" for i in range(mesh_isolation.MAX_WAITERS)]
    holder = _hold_locks(private_run_dir, names, 3)
    try:
        with pytest.raises(HTTPException) as refused:
            _parse_3mf_isolated(FIXTURE)
        assert refused.value.status_code == 429 and refused.value.headers["Retry-After"]
    finally:
        holder.kill(); holder.wait()
