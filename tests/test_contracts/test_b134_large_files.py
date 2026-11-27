"""Large sliced files use the same bounded readers and safety gates as smaller plates."""
import asyncio
import multiprocessing
import resource
import sys
import time
import tracemalloc
import zipfile

import httpx
import pytest
from core import print_file_limits as limits
from core.ams_source_inspection import inspect_ams_sources
from core.print_payload_snapshot import hash_regular_payload, stage_payload
from modules.models_library.mesh_isolation import TIMEOUT_SECONDS, analyze_blocking
from modules.printers.dispatch_capability_payload import inspect_payload
from ops.large_plate_benchmark import make_plate, peak_rss_bytes
from tests.test_contracts.test_print_file_upload_ownership_behavior import upload_client, token_client, review_db


@pytest.mark.slow
def test_large_sliced_plate_upload_and_dispatch_inspection(upload_client, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from modules.models_library import upload_stream
    monkeypatch.setattr(upload_stream.shutil, "disk_usage", lambda _: SimpleNamespace(free=8 * limits.GIB))
    client, issue, storage = upload_client
    plate = tmp_path / "large.3mf"
    make_plate(plate, 290)
    from modules.models_library.threemf_parser import extract_mesh_from_3mf

    # The same triangle previews at small size. Only XML comment padding changes.
    head = (b'<model><resources><object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/>'
            b'<vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>'
            b'<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>'
            b'<build><item objectid="1"/></build><!--')
    tail = b'--></model>'
    part = "3D/Objects/object_1.model"
    small = tmp_path / "small.3mf"
    with zipfile.ZipFile(small, "w") as package:
        package.writestr(part, head + tail)
    assert extract_mesh_from_3mf(small)["triangle_count"] == 1
    padding = b" ".join(str(i).encode() for i in range(8192))
    with zipfile.ZipFile(plate, "a", compression=zipfile.ZIP_DEFLATED) as package:
        with package.open(part, "w") as model:
            model.write(head)
            blocks, extra = divmod(370 * limits.MIB - len(head) - len(tail), len(padding))
            for _ in range(blocks):
                model.write(padding)
            model.write(padding[:extra] + tail)
    assert analyze_blocking(plate, mode="education").status == "too_large"
    async def upload():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as http:
            with plate.open("rb") as source:
                return await http.post("/print-files/upload", headers=issue(["write:models"]),
                                       files={"file": ("large.3mf", source, "application/octet-stream")})

    response = asyncio.run(upload())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_mesh"] is False
    assert body["quick_print_ready"] is True
    assert body["filaments"][0]["type"] == "PETG"
    digest = hash_regular_payload(plate)
    with stage_payload(next(storage.iterdir()), digest) as staged:
        assert hash_regular_payload(staged.path) == digest
        with open(staged.path, "rb") as source:
            assert len(inspect_ams_sources(source).sources) == 2


@pytest.mark.slow
def test_512_mib_gcode_inspection_budgets(tmp_path):
    plate = tmp_path / "limit.3mf"
    make_plate(plate, limits.MAX_GCODE_BYTES // limits.MIB)
    print(f"local test archive: {plate.stat().st_size} bytes", flush=True)

    # RSS high-water values cover the whole process lifetime. Isolate this test
    # from other pytest fixtures and use a fresh parent for the upload worker.
    worker = multiprocessing.get_context("spawn").Process(target=_inspect_at_limit, args=(plate,))
    worker.start()
    worker.join()
    assert worker.exitcode == 0


def _inspect_at_limit(plate):
    def measure(label, operation, timeout=None):
        # python -X tracemalloc -m pytest ... -s also reports Python allocations.
        if tracemalloc.is_tracing():
            tracemalloc.reset_peak()
        started = time.perf_counter()
        result = operation()
        elapsed = time.perf_counter() - started
        rss = peak_rss_bytes()
        print(f"{label}: {elapsed:.2f} s, process high water {rss / limits.MIB:.2f} MiB", flush=True)
        if tracemalloc.is_tracing():
            print(f"Python peak: {tracemalloc.get_traced_memory()[1] / limits.MIB:.2f} MiB", flush=True)
        assert rss <= 512 * limits.MIB
        if timeout is not None:
            assert elapsed <= timeout
        return result

    analysis = measure("isolated upload analysis", lambda: analyze_blocking(plate), TIMEOUT_SECONDS)
    worker_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    worker_rss *= 1 if sys.platform == "darwin" else 1024
    print(f"children high water: {worker_rss / limits.MIB:.2f} MiB", flush=True)
    assert worker_rss <= 512 * limits.MIB
    assert analysis.status == "ok" and analysis.plate1_gcode
    assert measure("capability inspection", lambda: inspect_payload(plate, 1, "H2D"))["mapping_refusal"] is None
    with plate.open("rb") as source:
        assert len(measure("AMS inspection", lambda: inspect_ams_sources(source)).sources) == 2


@pytest.mark.parametrize("low_volume", ["storage", "temporary", None])
def test_large_upload_disk_reserve_before_body(tmp_path, monkeypatch, low_volume, upload_client):
    from pathlib import Path
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from modules.models_library import upload_stream

    storage = tmp_path / "storage"
    storage.mkdir()
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    size = limits.DISK_CHECK_THRESHOLD + 1
    monkeypatch.setenv("PRINT_FILE_STORAGE_ROOT", str(storage))
    monkeypatch.setattr(upload_stream.tempfile, "gettempdir", lambda: str(temporary))
    monkeypatch.setattr(upload_stream.shutil, "disk_usage", lambda path: SimpleNamespace(
        free=4 * size - int(Path(path).name == low_volume)))
    client, issue, _ = upload_client
    app = client.app
    reads = []

    async def counted(scope, receive, send):
        async def counting_receive():
            reads.append(True)
            return await receive()
        await app(scope, counting_receive, send)

    response = TestClient(counted).post("/print-files/upload", content=b"invalid",
                                        headers={**issue(["write:models"]), "Content-Length": str(size), "Content-Type": "multipart/form-data; boundary=x"})
    if low_volume:
        assert response.status_code == 507
        assert response.json()["detail"] == "Not enough free disk space for this file"
        assert not reads
    else:
        assert reads and response.status_code == 400


@pytest.mark.parametrize("member_extra", [0, 1])
def test_zip_expansion_exact_boundary(tmp_path, monkeypatch, member_extra):
    from core import zip_bounds

    monkeypatch.setattr(zip_bounds, "MAX_MEMBER_BYTES", 1024)
    monkeypatch.setattr(zip_bounds, "MAX_TOTAL_BYTES", 2048)
    plate = tmp_path / "boundaries.3mf"
    with zipfile.ZipFile(plate, "w") as package:
        package.writestr("3D/first.model", bytes(1024))
        package.writestr("3D/second.model", bytes(1024 + member_extra))
    with zip_bounds.open_bounded(plate) as package:
        if member_extra:
            with pytest.raises(zip_bounds.ArchiveRefused, match="^member_too_large$"):
                zip_bounds.check_expansion(package)
            return
        zip_bounds.check_expansion(package)
    with zipfile.ZipFile(plate, "a") as package:
        package.writestr("extra.txt", b"x")
    with zip_bounds.open_bounded(plate) as package, pytest.raises(zip_bounds.ArchiveRefused, match="^too_large$"):
        zip_bounds.check_expansion(package)


def test_gcode_over_both_expansion_limits_reports_sliced_limit(tmp_path):
    from core import zip_bounds

    plate = tmp_path / "oversized.3mf"
    member = "Metadata/plate_1.gcode"
    with zipfile.ZipFile(plate, "w") as package:
        package.writestr(member, "G28")
    with zip_bounds.open_bounded(plate) as package:
        # Expansion checks use directory sizes, so no GiB-sized payload is needed.
        package.getinfo(member).file_size = limits.MAX_MEMBER_BYTES + 100 * limits.MIB
        with pytest.raises(zip_bounds.ArchiveRefused) as refusal:
            zip_bounds.check_expansion(package)
    assert limits.UPLOAD_EXPANSION_REFUSALS[refusal.value.reason] == (
        f"File rejected: a plate exceeds the {limits.MAX_GCODE_BYTES // limits.MIB} MiB sliced G-code limit."
    )


def test_snapshot_at_compressed_boundary_and_one_over(tmp_path, monkeypatch):
    from core import print_payload_snapshot
    monkeypatch.setattr(print_payload_snapshot, "MAX_PAYLOAD_BYTES", 1024)
    from core.print_payload_snapshot import PayloadUnavailable, open_regular_payload

    path = tmp_path / "sparse.3mf"
    with path.open("wb") as f:
        f.truncate(print_payload_snapshot.MAX_PAYLOAD_BYTES)
    with open_regular_payload(path) as (_, info):
        assert info.st_size == print_payload_snapshot.MAX_PAYLOAD_BYTES
    with path.open("ab") as f:
        f.write(b"x")
    with pytest.raises(PayloadUnavailable, match=f"limit {print_payload_snapshot.MAX_PAYLOAD_BYTES} bytes"):
        with open_regular_payload(path):
            pytest.fail("Oversized payload admitted")


@pytest.mark.parametrize("declared", [None, "1"])
@pytest.mark.parametrize("low_disk", [False, True])
def test_streamed_multipart_cuts_short_untrusted_length(upload_client, monkeypatch, declared, low_disk):
    import json
    from types import SimpleNamespace
    from modules.models_library import upload_stream

    client, issue, _ = upload_client
    monkeypatch.setattr(upload_stream, "MAX_UPLOAD_BYTES", 256)
    monkeypatch.setattr(upload_stream, "MULTIPART_FRAMING_BYTES", 128)
    monkeypatch.setattr(upload_stream, "DISK_CHECK_THRESHOLD", 100)
    monkeypatch.setattr(upload_stream.shutil, "disk_usage", lambda _: SimpleNamespace(free=0 if low_disk else 10**9))
    headers = dict(issue(["write:models"]))
    headers["Content-Type"] = "multipart/form-data; boundary=abc"
    if declared is not None:
        headers["Content-Length"] = declared
    scope = {"type": "http", "method": "POST", "scheme": "http", "path": "/print-files/upload",
             "query_string": b"", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
             "client": ("testclient", 123), "server": ("testserver", 80), "http_version": "1.1"}
    prefix = b'--abc\r\nContent-Disposition: form-data; name="file"; filename="large.gcode"\r\n\r\n'
    chunks = iter([prefix, b"x" * 200, b"x" * 200, b"never read"])
    replies = []

    async def receive():
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    async def send(message):
        replies.append(message)

    asyncio.run(client.app(scope, receive, send))
    assert replies[0]["status"] == (507 if low_disk else 413)
    assert "detail" in json.loads(replies[-1]["body"])
    assert next(chunks) == (b"x" * 200 if low_disk else b"never read")


def test_upload_accepts_exact_compressed_limit_and_refuses_next_byte(tmp_path, monkeypatch):
    from modules.models_library import upload_stream
    monkeypatch.setattr(upload_stream, "MAX_UPLOAD_BYTES", 1024)
    from fastapi import HTTPException
    from starlette.datastructures import UploadFile
    from modules.models_library.upload_stream import staged_upload

    monkeypatch.setenv("PRINT_FILE_STORAGE_ROOT", str(tmp_path))
    sparse = tmp_path / "boundary.gcode"

    async def check():
        with sparse.open("w+b") as source:
            source.truncate(upload_stream.MAX_UPLOAD_BYTES)
            upload = UploadFile(source, size=upload_stream.MAX_UPLOAD_BYTES)
            async with staged_upload(upload, ".gcode") as (path, digest):
                assert hash_regular_payload(path) == digest
            source.seek(0, 2)
            source.write(b"x")
            source.seek(0)
            upload = UploadFile(source, size=upload_stream.MAX_UPLOAD_BYTES + 1)
            with pytest.raises(HTTPException) as exc:
                async with staged_upload(upload, ".gcode"):
                    pytest.fail("Oversized upload admitted")
            assert exc.value.status_code == 413
            assert f"limit {upload_stream.MAX_UPLOAD_BYTES} bytes" in exc.value.detail

    asyncio.run(check())

def test_300_mib_gcode_source_scan(tmp_path):
    plate = tmp_path / "large.3mf"
    make_plate(plate, 300)
    with plate.open("rb") as source:
        assert len(inspect_ams_sources(source).sources) == 2
