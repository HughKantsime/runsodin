"""Cert B round 6: 3MF isolation completed.

1. the Education upload never blocks the event loop on the 3MF analysis;
2. admission (429) happens on the calling thread, before any executor hand-off; 3MF work
   has its own executor and never queues behind the default one;
3. absurd archives are refused at upload (members, per-member and total expansion); stored
   packages are opened once per request, bounded before zipfile lists their directory, and
   off the event loop in async routes;
4. a hostile child cannot fail the parent: its analysis is schema-validated;
5. the child's whole process group dies at cleanup; it gets no path and an empty cwd;
6. without file locks the pool falls back to a per-process FIFO semaphore; waiting is FIFO;
7. the child refuses to run when its parent is not the one that started it.
"""
import asyncio
import io
import json
import logging
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_B_threemf_isolation import FIXTURE, SLICED, _hold_locks
from tests.test_contracts.test_print_file_upload_ownership_behavior import upload, upload_client  # noqa: F401
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db  # noqa: F401
from tests.test_contracts.test_education_submission_upload import submission_db  # noqa: F401


@pytest.fixture(autouse=True)
def private_run_dir(tmp_path, monkeypatch):
    run = tmp_path / "run"
    monkeypatch.setenv("ODIN_RUN_DIR", str(run))
    return run


def _hold_run_slots(run_dir, seconds):
    from modules.models_library import mesh_isolation
    return _hold_locks(run_dir, [f"threemf-run-{i}.lock" for i in range(mesh_isolation.MAX_WORKERS)], seconds)


# --------------------------------------------------------------------------- 1. Education keeps the loop free

def test_education_route_never_blocks_the_event_loop(submission_db, private_run_dir, monkeypatch):
    from functools import partial
    from starlette.requests import Request
    from tests.test_contracts import test_education_submission_upload as edu
    from modules.organizations import routes_education_submissions as routes
    db, upload_root = submission_db
    edu._ample_disk(monkeypatch)
    monkeypatch.setattr(routes, "_DurableMultipartParser", partial(routes._DurableMultipartParser, storage_root=upload_root))
    boundary = "ticker-check"
    token = str(uuid.uuid4())
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"cost_center_id\"\r\n\r\n7\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"submission_token\"\r\n\r\n{token}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"pilot.gcode.3mf\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + edu._sliced_3mf() + f"\r\n--{boundary}--\r\n".encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    request = Request({"type": "http", "method": "POST", "path": "/education/submissions",
                       "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())]}, receive)
    holder = _hold_run_slots(private_run_dir, 1.2)  # the analysis must wait ~1.2 s for a run slot

    async def run():
        ticks = []

        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.1)
        ticking = asyncio.create_task(ticker())
        started = time.monotonic()
        result = await routes.create_education_submission(request, edu._principal(), db)
        ticking.cancel()
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        return result, time.monotonic() - started, max(gaps)
    try:
        result, elapsed, worst_gap = asyncio.run(run())
    finally:
        holder.wait()
    assert elapsed >= 1.0  # it really waited for the held slots...
    assert worst_gap < 0.35, worst_gap  # ...while the 0.1 s ticker kept ticking
    assert result["status"] and db.execute(text("SELECT COUNT(*) FROM education_submissions")).scalar_one() == 1


# --------------------------------------------------------------------------- 2. admission and executor

def test_sixteen_concurrent_calls_are_admitted_or_refused_at_once_and_all_meet_the_deadline(monkeypatch):
    from modules.models_library import mesh_isolation
    monkeypatch.setenv("ODIN_MESH_WORKER_TEST_SLEEP", "0.8")

    async def one():
        started = time.monotonic()
        try:
            analysis = await mesh_isolation.analyze_3mf_isolated(FIXTURE, timeout=3)
            return "done:" + analysis.status, time.monotonic() - started
        except mesh_isolation.Busy:
            return "busy", time.monotonic() - started

    async def run():
        from concurrent.futures import ThreadPoolExecutor
        # A small shared default pool (busy with other services in production) must not
        # delay admission, the 429 or the deadline.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=4))
        return await asyncio.gather(*(one() for _ in range(16)))
    started = time.monotonic()
    results = asyncio.run(run())
    total = time.monotonic() - started
    immediate = [elapsed for outcome, elapsed in results if outcome == "busy" and elapsed < 0.25]
    capacity = mesh_isolation.MAX_WORKERS + mesh_isolation.MAX_WAITERS
    assert len(immediate) >= 16 - capacity, results  # beyond capacity: 429 at once
    assert all(elapsed <= 3.5 for _, elapsed in results), results  # every call within its deadline
    assert any(outcome == "done:ok" for outcome, _ in results)
    assert total < 4.0


def test_3mf_work_never_queues_behind_the_default_executor():
    from concurrent.futures import ThreadPoolExecutor
    from modules.models_library import mesh_isolation

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        blocker = loop.run_in_executor(None, time.sleep, 3)  # e.g. an Education AMS read holding the pool
        started = time.monotonic()
        analysis = await mesh_isolation.analyze_3mf_isolated(FIXTURE, timeout=10)
        elapsed = time.monotonic() - started
        await blocker
        return analysis, elapsed
    analysis, elapsed = asyncio.run(run())
    assert analysis.status == "ok" and elapsed < 2.5


# --------------------------------------------------------------------------- 3. bounded archives and reopen sites

def _many_members(path, count):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        for index in range(count):
            z.writestr(f"Metadata/m{index}.txt", "")
    return path


def test_upload_refuses_too_many_members_and_oversized_members(upload_client, tmp_path):
    client, issue, _ = upload_client
    response = upload(client, issue(["write:models"]), "many.3mf", _many_members(tmp_path / "many.3mf", 600).read_bytes())
    assert response.status_code == 400 and "too many archive members" in response.text
    big = tmp_path / "member.3mf"
    with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Metadata/plate_1.gcode", b"\0" * (401 * 2**20))  # one member over 400 MB, total under 500 MB
    response = upload(client, issue(["write:models"]), "member.3mf", big.read_bytes())
    assert response.status_code == 400 and "400 MB per member" in response.text


# --------------------------------------------------------------------------- 4. hostile child output

def _analysis_frame(payload) -> bytes:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return b"A" + struct.pack("<I", len(raw)) + raw


GOOD = None


def _good():
    from modules.models_library import mesh_isolation
    global GOOD
    if GOOD is None:
        ticket = mesh_isolation.reserve(30)
        analysis = mesh_isolation.run_admitted(SLICED, ticket)
        GOOD = {"status": "ok", "metadata": analysis.metadata, "plate_objects": analysis.plate_objects,
                "plate_count": analysis.plate_count, "file_meta": analysis.file_meta}
    return json.loads(json.dumps(GOOD))


def _mutations():
    def change(path, value):
        def apply(payload):
            target = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return payload
        return apply
    return {
        "plate_count zero": change(("plate_count",), 0),
        "plate_count bool": change(("plate_count",), True),
        "plate_count huge": change(("plate_count",), 10**9),
        "plate objects not a list": change(("plate_objects",), {"a": 1}),
        "plate object shape": change(("plate_objects",), [{"name": "x"}]),
        "metadata not a dict": change(("metadata",), ["x"]),
        "metadata nested where scalar": change(("metadata", "project_name"), {"a": "b"}),
        "metadata negative time": change(("metadata", "print_time_seconds"), -5),
        "metadata non-finite": change(("metadata", "total_weight_grams"), float("nan")),
        "metadata extra key": change(("metadata", "extra"), 1),
        "filament shape": change(("metadata", "filaments"), [{"slot": "1"}]),
        "file meta shape": change(("file_meta",), {"bed_x_mm": 1}),
        "unknown status": change(("status",), "pwned"),
    }


@pytest.mark.parametrize("name", list(_mutations()))
def test_schema_violations_mean_no_metadata(name):
    from modules.models_library.mesh_isolation import validate_analysis
    assert validate_analysis(_good()).status == "ok"
    assert validate_analysis(_mutations()[name](_good())).status == "failed"


@pytest.mark.parametrize("blob", [_analysis_frame(b"[" * 200000), _analysis_frame(b"\xff\xfe"), b"A\xff\xff\xff\x7f",
                                  _analysis_frame(b"{\"status\": \"ok\"}") + b"M" + struct.pack("<I", 3) + b"abc"])
def test_hostile_frames_never_raise(blob):
    from modules.models_library.mesh_isolation import _decode
    assert _decode(blob, "upload", True).status == "failed"


def test_upload_with_a_hostile_child_is_400_not_500(upload_client, monkeypatch):
    from modules.models_library import mesh_isolation
    client, issue, _ = upload_client
    hostile = _analysis_frame({**_good(), "plate_count": -1})
    real = subprocess.Popen

    def fake(argv, **kwargs):
        script = f"import sys; sys.stdout.buffer.write({hostile!r})"
        return real([sys.executable, "-c", script], **kwargs)
    monkeypatch.setattr(mesh_isolation.subprocess, "Popen", fake)
    response = upload(client, issue(["write:models"]), "hostile.3mf", SLICED.read_bytes())
    assert response.status_code == 400 and "Failed to parse" in response.text


# --------------------------------------------------------------------------- 5. process group, no path, empty cwd

def test_grandchildren_die_with_the_worker(tmp_path, monkeypatch):
    from modules.models_library import mesh_isolation
    marker = tmp_path / "grandchild.pid"
    monkeypatch.setenv("ODIN_MESH_WORKER_TEST_GRANDCHILD", str(marker))
    assert mesh_isolation.analyze_blocking(FIXTURE).status == "ok"
    pid = int(marker.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail("grandchild survived its worker")


def test_child_gets_no_path_and_an_empty_temporary_cwd(monkeypatch):
    from modules.models_library import mesh_isolation
    calls = []
    real = subprocess.Popen

    def recording(argv, **kwargs):
        calls.append((list(argv), kwargs["cwd"], os.listdir(kwargs["cwd"]), kwargs["stdin"]))
        return real(argv, **kwargs)
    monkeypatch.setattr(mesh_isolation.subprocess, "Popen", recording)
    assert mesh_isolation.analyze_blocking(SLICED).status == "ok"
    argv, cwd, listing, stdin = calls[0]
    assert not any(str(SLICED.resolve().parent) in part or str(SLICED) == part for part in argv)
    assert listing == [] and Path(cwd).name.startswith("odin-3mf-") and not Path(cwd).exists()
    assert hasattr(stdin, "fileno")  # the package is handed over as a descriptor


# --------------------------------------------------------------------------- 6. fallback and FIFO

def test_without_file_locks_the_pool_falls_back_per_process(monkeypatch, caplog):
    import errno
    import fcntl
    from modules.models_library import mesh_isolation

    def no_locks(*a, **k):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(fcntl, "flock", no_locks)
    monkeypatch.setattr(mesh_isolation, "_warned", [])
    with caplog.at_level(logging.WARNING, logger="odin.api"):
        assert mesh_isolation.analyze_blocking(FIXTURE).status == "ok"
    assert any("file locks unavailable" in r.getMessage() for r in caplog.records)


def test_an_unusable_run_directory_falls_back_too(tmp_path, monkeypatch):
    from modules.models_library import mesh_isolation
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("ODIN_RUN_DIR", str(blocker / "run"))
    monkeypatch.setattr(mesh_isolation, "_warned", [])
    assert mesh_isolation.analyze_blocking(FIXTURE).status == "ok"


def test_run_slots_are_handed_out_oldest_ticket_first(private_run_dir, monkeypatch):
    from modules.models_library import mesh_isolation
    monkeypatch.setattr(mesh_isolation, "MAX_WORKERS", 1)
    holder = _hold_locks(private_run_dir, ["threemf-run-0.lock"], 0.8)
    order, threads = [], []
    real = mesh_isolation._acquire_run_slot

    def recording(ticket):
        slot = real(ticket)
        order.append(ticket.order)
        return slot
    monkeypatch.setattr(mesh_isolation, "_acquire_run_slot", recording)
    tickets = []
    for _ in range(4):
        ticket = mesh_isolation.reserve(20)
        tickets.append(ticket.order)
        thread = threading.Thread(target=mesh_isolation.run_admitted, args=(FIXTURE, ticket), kwargs={"mode": "metadata"})
        threads.append(thread)
        time.sleep(0.01)
    for thread in reversed(threads):  # the youngest starts polling first
        thread.start()
    for thread in threads:
        thread.join(30)
    holder.wait()
    assert order == sorted(tickets) == tickets


def test_local_fallback_is_fifo():
    from modules.models_library.mesh_isolation import _LocalPool
    import modules.models_library.mesh_isolation as mesh_isolation
    pool = _LocalPool()
    pool.running = mesh_isolation.MAX_WORKERS  # every run slot busy
    tickets = [pool.reserve() for _ in range(4)]
    order = []

    def wait(ticket):
        if pool.acquire(ticket, time.monotonic() + 10):
            order.append(tickets.index(ticket))
            pool.release_wait(ticket)
            time.sleep(0.02)
            pool.release_run()
    threads = [threading.Thread(target=wait, args=(t,)) for t in reversed(tickets)]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    for _ in range(mesh_isolation.MAX_WORKERS):
        pool.release_run()
    for thread in threads:
        thread.join(10)
    assert order == [0, 1, 2, 3]


# --------------------------------------------------------------------------- 7. parent pid

def test_worker_refuses_to_run_under_a_different_parent():
    backend = Path(__file__).resolve().parents[2] / "backend"
    with open(FIXTURE, "rb") as package:
        result = subprocess.run([sys.executable, "-m", "modules.models_library.mesh_worker", str(2**30), "5", "metadata",
                                 str(os.getpid() + 999999), "x.3mf"], cwd=backend, stdin=package, capture_output=True,
                                timeout=30, env={**os.environ, "PYTHONPATH": str(backend)})
    assert result.returncode == 4 and result.stdout == b""
