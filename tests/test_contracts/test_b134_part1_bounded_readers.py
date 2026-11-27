"""Part 1 contract tests for B134 (bounded readers, precheck, streaming adapters, dispatch caching, H3C threading, B138)."""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import socketserver
import threading
import tracemalloc
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_print_file_upload_ownership_behavior import upload_client
from tests.test_contracts.test_h3c_send import target
from tests.test_contracts.test_project_behavior import project_client
from tests.test_contracts.test_api_token_delegation_behavior import token_client


# --------------------------------------------------------------------------- 1. bounded 3MF & safety metadata readers


def _zip_bytes(members: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_optional_oversized_project_name_and_thumbnail_are_skipped(monkeypatch):
    from modules.models_library import threemf_parser as tp

    monkeypatch.setattr(tp, "MAX_PROJECT_MODEL_BYTES", 64)
    monkeypatch.setattr(tp, "MAX_THUMBNAIL_BYTES", 32)
    raw = _zip_bytes({
        "3D/3dmodel.model": b'<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                            b'<metadata name="Title">Huge</metadata>' + b" " * 100 + b"</model>",
        "Metadata/plate_1.png": b"\x89PNG" + b"\x00" * 64,
    })
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        assert tp.extract_project_name(zf) is None
        assert tp.extract_thumbnail(zf) is None


def test_extract_project_name_does_not_hide_memory_error(monkeypatch):
    from modules.models_library import threemf_parser as tp

    def oom(*args, **kwargs):
        raise MemoryError("synthetic oom")

    monkeypatch.setattr(tp, "_member_bytes", oom)
    with zipfile.ZipFile(io.BytesIO(_zip_bytes({"3D/3dmodel.model": b"<model/>"}))) as zf:
        with pytest.raises(MemoryError):
            tp.extract_project_name(zf)


def test_parse_unsliced_3mf_bounds_project_settings_read(tmp_path, monkeypatch):
    from modules.models_library import print_file_meta as pfm
    from modules.models_library import threemf_parser as tp

    monkeypatch.setattr(tp, "MAX_PROJECT_SETTINGS_BYTES", 64)
    path = tmp_path / "unsliced.3mf"
    path.write_bytes(_zip_bytes({
        "Metadata/project_settings.config": json.dumps({"filament_type": ["PLA"], "pad": "x" * 128}).encode(),
    }))
    # Safety facts refuse oversized project_settings.config.
    with zipfile.ZipFile(path) as zf:
        with pytest.raises(tp.MemberTooLargeError, match="file is too large to check"):
            tp.parse_unsliced_3mf(zf, str(path))

    # Even if safety facts were bypassed, parse_unsliced_3mf itself uses bounded _member_bytes.
    empty_facts = {k: pfm._missing_fact() for k in ("api_types", "machine", "bed", "nozzle", "materials")}
    monkeypatch.setattr(pfm, "extract_print_file_meta", lambda *a, **kw: {"safety_facts": empty_facts})
    with zipfile.ZipFile(path) as zf:
        meta = tp.parse_unsliced_3mf(zf, str(path), name="unsliced.3mf")
    assert meta is not None
    assert meta.filaments[0].type == "Unknown"


@pytest.mark.parametrize("member,const_name,extra_members", [
    ("Metadata/project_settings.config", "MAX_PROJECT_SETTINGS_BYTES", {}),
    ("Metadata/slice_info.config", "MAX_SLICE_INFO_BYTES", {}),
    ("Metadata/plate_1.json", "MAX_PLATE_JSON_BYTES", {}),
    ("Metadata/model_settings.config", "MAX_MODEL_SETTINGS_BYTES", {}),
])
def test_oversized_safety_member_is_refused(tmp_path, monkeypatch, member, const_name, extra_members):
    from modules.models_library import mesh_worker, print_file_meta as pfm, threemf_parser as tp

    monkeypatch.setattr(tp, const_name, 64)
    path = tmp_path / "oversized-safety.3mf"
    members = {**extra_members, member: b"x" * 128}
    path.write_bytes(_zip_bytes(members))

    with pytest.raises(tp.MemberTooLargeError, match="file is too large to check"):
        pfm._extract_3mf_safety_facts(str(path))
    with pytest.raises(tp.MemberTooLargeError, match="file is too large to check"):
        pfm.extract_print_file_meta(str(path), ".3mf")

    result = mesh_worker._analysis(str(path), "oversized-safety.3mf", "upload")
    expected_status = "plate_json_too_large" if member == "Metadata/plate_1.json" else "safety_member_too_large"
    assert result["status"] == expected_status


def test_unused_max_uncompressed_bytes_removed_from_education_service():
    from modules.organizations import education_submission_service as svc

    assert not hasattr(svc, "MAX_UNCOMPRESSED_BYTES")


# --------------------------------------------------------------------------- 2. Content-Length precheck before reading body


def test_upload_content_length_precheck_refuses_before_reading_body(monkeypatch, upload_client):
    from core.print_upload_limits import MULTIPART_FRAMING_BYTES
    from modules.models_library import upload_stream

    monkeypatch.setattr(upload_stream, "MAX_UPLOAD_BYTES", 1024)
    client, issue, _ = upload_client
    app = client.app
    auth = issue(["write:models"])

    body_bytes_read = 0

    class CountingMiddleware:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            nonlocal body_bytes_read

            async def counting_receive():
                nonlocal body_bytes_read
                msg = await receive()
                if msg.get("type") == "http.request":
                    body_bytes_read += len(msg.get("body", b""))
                return msg

            await self.inner(scope, counting_receive, send)

    wrapped = CountingMiddleware(app)
    client = TestClient(wrapped)
    over = 1024 + MULTIPART_FRAMING_BYTES + 1
    resp = client.post(
        "/print-files/upload",
        content=b"a" * 4096,
        headers={
            **auth,
            "Content-Type": "multipart/form-data; boundary=testbound",
            "Content-Length": str(over),
        },
    )
    assert resp.status_code == 413, resp.text
    assert "Multipart upload too large" in resp.json()["detail"]
    assert body_bytes_read == 0

    bad = client.post(
        "/print-files/upload",
        content=b"a" * 16,
        headers={
            **auth,
            "Content-Type": "multipart/form-data; boundary=testbound",
            "Content-Length": "not-a-number",
        },
    )
    assert bad.status_code == 400
    assert body_bytes_read == 0


# --------------------------------------------------------------------------- 3. Streaming multipart printer adapters


class _CaptureServer:
    def __init__(self, *, require_digest=False, status=201, response_body=b'{"done": true}'):
        self.received_body = bytearray()
        self.received_headers = {}
        self.received_path = None
        self.request_count = 0
        self.require_digest = require_digest
        self.status = status
        self.response_body = response_body
        self._server = None
        self._thread = None

    def __enter__(self):
        capture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                capture.request_count += 1
                capture.received_path = self.path
                length = int(self.headers.get("Content-Length", "0"))
                # Drain body in small chunks so the server itself stays O(1) memory too.
                remaining = length
                capture.received_body.clear()
                capture.received_headers = dict(self.headers.items())
                record = length <= 1024 * 1024
                drained = 0
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    drained += len(chunk)
                    remaining -= len(chunk)
                    if record:
                        capture.received_body.extend(chunk)
                capture.last_drained = drained
                if capture.require_digest and "Authorization" not in self.headers:
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        'Digest realm="PrusaLink", nonce="abcdef0123456789", qop="auth"',
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(capture.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(capture.response_body)))
                self.end_headers()
                self.wfile.write(capture.response_body)

            def log_message(self, *args):
                pass

        class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self._server = ThreadedServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)


def _allow_loopback_device(monkeypatch):
    from contextlib import contextmanager
    from core import itar, webhook_utils

    @contextmanager
    def _noop_pin(url):
        yield ["127.0.0.1"]

    monkeypatch.setattr(webhook_utils, "pinned_device_destination", _noop_pin)
    monkeypatch.setattr(itar, "enforce_request_destination", lambda url: None)


@pytest.mark.parametrize("adapter_kind", ["moonraker", "octoprint", "prusalink_key", "prusalink_digest"])
def test_adapters_send_byte_identical_multipart_body(tmp_path, monkeypatch, adapter_kind):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    from modules.printers.adapters.octoprint import OctoPrintPrinter
    from modules.printers.adapters.prusalink import PrusaLinkPrinter

    _allow_loopback_device(monkeypatch)
    sample = tmp_path / "tiny.gcode"
    sample_bytes = b"; synthetic test\nG28\nG1 X10 Y20 E0.5\n"
    sample.write_bytes(sample_bytes)

    require_digest = adapter_kind == "prusalink_digest"
    with _CaptureServer(require_digest=require_digest) as server:
        if adapter_kind == "moonraker":
            ok = MoonrakerPrinter("127.0.0.1", port=server.port, api_key="k").upload_file(str(sample), "tiny.gcode")
            expected_fields = {"root": "gcodes"}
        elif adapter_kind == "octoprint":
            ok = OctoPrintPrinter("127.0.0.1", port=server.port, api_key="k").upload_and_print(str(sample), "tiny.gcode")
            expected_fields = {"select": "true", "print": "true"}
        elif adapter_kind == "prusalink_key":
            ok = PrusaLinkPrinter("127.0.0.1", port=server.port, api_key="k").upload_and_print(str(sample), "tiny.gcode")
            expected_fields = {"select": "true", "print": "true"}
        else:
            ok = PrusaLinkPrinter("127.0.0.1", port=server.port, username="maker", password="secret").upload_and_print(
                str(sample), "tiny.gcode"
            )
            expected_fields = {"select": "true", "print": "true"}

        assert ok is True
        if require_digest:
            assert server.request_count == 2
        ct = server.received_headers["Content-Type"]
        assert ct.startswith("multipart/form-data; boundary=")
        boundary = ct.split("boundary=", 1)[1]

        # Compare byte-for-byte with requests' legacy files= encoding using the same boundary.
        monkeypatch.setattr("urllib3.filepost.choose_boundary", lambda: boundary)
        with sample.open("rb") as handle:
            legacy = requests.Request("POST", "http://localhost/", data=expected_fields,
                                      files={"file": (sample.name, handle, "application/octet-stream")}).prepare()
        assert ct == legacy.headers["Content-Type"]
        assert bytes(server.received_body) == legacy.body
        assert server.received_headers["Content-Length"] == legacy.headers["Content-Length"]


@pytest.mark.parametrize("adapter_kind", ["moonraker", "octoprint", "prusalink"])
def test_adapters_stream_64mib_with_bounded_peak_memory(tmp_path, monkeypatch, adapter_kind):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    from modules.printers.adapters.octoprint import OctoPrintPrinter
    from modules.printers.adapters.prusalink import PrusaLinkPrinter

    _allow_loopback_device(monkeypatch)
    big = tmp_path / "big64.gcode"
    chunk = b"G1 X1 Y2 E0.1\n" * 4096
    target = 64 * 1024 * 1024
    with big.open("wb") as fh:
        written = 0
        while written < target:
            part = chunk[: min(len(chunk), target - written)]
            fh.write(part)
            written += len(part)

    with _CaptureServer() as server:
        tracemalloc.start()
        try:
            if adapter_kind == "moonraker":
                ok = MoonrakerPrinter("127.0.0.1", port=server.port, api_key="k").upload_file(str(big), "big64.gcode")
            elif adapter_kind == "octoprint":
                ok = OctoPrintPrinter("127.0.0.1", port=server.port, api_key="k").upload_and_print(str(big), "big64.gcode")
            else:
                ok = PrusaLinkPrinter("127.0.0.1", port=server.port, api_key="k").upload_and_print(str(big), "big64.gcode")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert ok is True
        assert server.last_drained > target
        # Streaming must stay far below the 64 MiB file size (typically < 2 MiB).
        assert peak < 16 * 1024 * 1024, f"{adapter_kind} peak memory too high: {peak / (1024 * 1024):.1f} MiB"


# --------------------------------------------------------------------------- 4. Dispatch G-code open count & single staging


def test_dispatch_opens_gcode_at_most_twice_and_stages_once(review_db, tmp_path, monkeypatch):
    pytest.importorskip("paho.mqtt.client")
    from tests.test_contracts.test_h2d_ams_dispatch import setup_h2d
    from tests.test_contracts.test_large_plate_dispatch import hardware
    from modules.printers import dispatch, dispatch_orchestration as orch

    engine, _ = setup_h2d(review_db, tmp_path, monkeypatch, mapping=[-1, 1])
    hardware(monkeypatch)

    from modules.printers import dispatch_capability_gate as capability
    inspections = []
    original_inspect = capability.inspect_payload

    def inspect_verified_stream(stream, *args):
        # Bind cached facts to the opened object that supplied the digest.
        assert hasattr(stream, "fileno")
        inspections.append(stream.fileno())
        return original_inspect(stream, *args)

    monkeypatch.setattr(capability, "inspect_payload", inspect_verified_stream)
    gcode_opens = []
    original_open = zipfile.ZipFile.open

    def spy_open(self, name, *args, **kwargs):
        member_name = name.filename if isinstance(name, zipfile.ZipInfo) else str(name)
        if member_name.endswith(".gcode"):
            gcode_opens.append(member_name)
        return original_open(self, name, *args, **kwargs)

    stage_calls = []
    original_stage = orch.stage_payload

    def spy_stage(*args, **kwargs):
        stage_calls.append(args[0])
        return original_stage(*args, **kwargs)

    from modules.printers import dispatch_payload_snapshot

    monkeypatch.setattr(zipfile.ZipFile, "open", spy_open)
    monkeypatch.setattr(orch, "stage_payload", spy_stage)
    monkeypatch.setattr(dispatch_payload_snapshot, "stage_payload", spy_stage)

    result = dispatch.dispatch_job(59, 53)
    assert result[0] is True, result
    assert len(inspections) == 1
    assert 0 < len(gcode_opens) <= 2, f"Expected <= 2 G-code opens, saw {len(gcode_opens)}: {gcode_opens}"
    assert len(stage_calls) == 1, f"Expected 1 stage_payload call, saw {len(stage_calls)}"


def test_inspect_ams_payload_already_staged_does_not_re_stage_and_verifies_hash(tmp_path, monkeypatch):
    from core.print_payload_snapshot import PayloadChanged
    from modules.printers import dispatch_payload_snapshot, services
    from tests.test_contracts.test_ams_source_inspection import archive

    path = tmp_path / "staged.3mf"
    path.write_bytes(archive())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    def forbidden_stage(*args, **kwargs):
        raise AssertionError("stage_payload must not be called when already_staged=True")

    monkeypatch.setattr(dispatch_payload_snapshot, "stage_payload", forbidden_stage)
    facts = services.inspect_ams_payload(
        path,
        expected_sha256=digest,
        expected_bytes=path.stat().st_size,
        plate_index=0,
        already_staged=True,
    )
    assert facts["material"] == "PETG"
    with pytest.raises(PayloadChanged):
        services.inspect_ams_payload(
            path,
            expected_sha256="0" * 64,
            expected_bytes=path.stat().st_size,
            plate_index=0,
            already_staged=True,
        )
    with pytest.raises(ValueError, match="changed"):
        services.inspect_ams_payload(
            path,
            expected_sha256=digest,
            expected_bytes=1,
            plate_index=0,
            already_staged=True,
        )


# --------------------------------------------------------------------------- 5. H3C preview runs off caller thread


def test_h3c_rack_sources_runs_on_3mf_executor_thread(tmp_path, monkeypatch):
    from core import ams_mapping

    fixture = Path(__file__).parents[1] / "fixtures/telemetry/h3c-cli-single.3mf"
    observed_threads = []
    ams_mapping._rack_sources.cache_clear()
    original = ams_mapping._rack_sources.__wrapped__

    def spy(path, identity, plate_index):
        observed_threads.append(threading.current_thread().name)
        return original(path, identity, plate_index)

    monkeypatch.setattr(ams_mapping, "_rack_sources", spy)
    caller_thread = threading.current_thread().name
    sources = ams_mapping.read_rack_sources(str(fixture), 0)
    assert sources, "Expected H3C rack sources from fixture"
    assert observed_threads and observed_threads[0] != caller_thread
    assert observed_threads[0].startswith("odin-3mf")


def test_h3c_rack_sources_reuses_occupied_executor_slot(tmp_path, monkeypatch):
    from core import ams_mapping
    from modules.models_library import mesh_isolation as pool

    fixture = Path(__file__).parents[1] / "fixtures/telemetry/h3c-cli-single.3mf"
    monkeypatch.setenv("ODIN_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(pool, "MAX_WORKERS", 1)

    def admitted_scan():
        # The outer inspection is running on the executor and holds the only slot.
        monkeypatch.setattr(pool, "reserve", lambda *_: pytest.fail("requested a second ticket"))
        return ams_mapping.read_rack_sources(fixture, 0)

    assert pool.inspect_on_executor(admitted_scan)


# --------------------------------------------------------------------------- 6. B138 project ZIP export lists oversized files


def test_b138_project_zip_export_lists_oversized_files_in_manifest_and_header(project_client, review_db, tmp_path, monkeypatch):
    from modules.archives.project_zip_manifest import MAX_FILE_BYTES

    root = tmp_path / "print_files"
    root.mkdir()
    monkeypatch.setenv("PRINT_FILE_STORAGE_ROOT", str(root))

    client, issue = project_client
    small = root / "small.3mf"
    small.write_bytes(b"small-archive-payload")
    oversized = root / "huge-h3c-plate.3mf"
    with oversized.open("wb") as fh:
        fh.truncate(MAX_FILE_BYTES + 1)

    review_db.execute(
        text("UPDATE print_archives SET project_id=101, file_path=:p, print_name='Small Print' WHERE id=201"),
        {"p": str(small)},
    )
    review_db.execute(
        text(
            "INSERT INTO print_archives(id,print_name,status,org_id,ownership_state,ownership_source,project_id,file_path) "
            "VALUES(299,'Giant H3C Plate','completed',1,'known','synthetic_owner',101,:p)"
        ),
        {"p": str(oversized)},
    )
    review_db.commit()

    response = client.get("/projects/101/export", headers=issue(["read:jobs"]))
    assert response.status_code == 200, response.text

    assert json.loads(response.headers["X-Odin-Export-Omitted"]) == {
        "count": 1, "names": ["huge-h3c-plate.3mf"],
    }

    # Manifest lists the omitted file and why without leaking host storage paths.
    with zipfile.ZipFile(io.BytesIO(response.content)) as pkg:
        assert sorted(pkg.namelist()) == ["files/000.3mf", "project.json"]
        raw_manifest = pkg.read("project.json").decode("utf-8")
        assert str(root) not in raw_manifest
        manifest = json.loads(raw_manifest)
        omitted, = manifest["omitted_files"]
        assert omitted["original_name"] == "huge-h3c-plate.3mf"
        assert omitted["print_name"] == "Giant H3C Plate"
        assert omitted["size"] == MAX_FILE_BYTES + 1
        assert "128 MiB" in omitted["reason"]

    # Exported ZIP with omitted_files still imports cleanly!
    imported = client.post(
        "/projects/import",
        headers=issue(["write:jobs"]),
        files={"file": ("export.zip", response.content, "application/zip")},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["archives"] == 2
    assert imported.json()["files"] == 1


@pytest.mark.parametrize("kind", ["symlink", "directory", "oversized", "bad_extension", "bad_hash", "metadata_refusal"])
def test_staged_ams_reader_preserves_snapshot_checks_and_refusal(tmp_path, kind, monkeypatch):
    from core import print_payload_snapshot
    from core.print_payload_snapshot import PayloadUnavailable
    from modules.printers.services import inspect_ams_payload
    from tests.test_contracts.test_ams_source_inspection import archive

    raw = archive(code="T99\n")
    path = tmp_path / ("staged.txt" if kind == "bad_extension" else "staged.3mf")
    if kind == "symlink":
        target = tmp_path / "original.3mf"
        target.write_bytes(raw)
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "oversized":
        with path.open("wb") as f:
            monkeypatch.setattr(print_payload_snapshot, "MAX_PAYLOAD_BYTES", 1024)
            f.truncate(1025)
    else:
        path.write_bytes(raw)
    digest = "invalid" if kind == "bad_hash" else hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError if kind == "metadata_refusal" else PayloadUnavailable):
        inspect_ams_payload(path, expected_sha256=digest, expected_bytes=len(raw), plate_index=0, already_staged=True)


def test_staged_ams_refuses_a_change_during_inspection(tmp_path, monkeypatch):
    from core import ams_source_inspection
    from core.print_payload_snapshot import PayloadChanged
    from modules.printers.services import inspect_ams_payload
    from tests.test_contracts.test_ams_source_inspection import archive

    path = tmp_path / "staged.3mf"
    raw = archive()
    path.write_bytes(raw)
    inspect = ams_source_inspection.inspect_single_source_ams

    def changed(source, plate_index):
        facts = inspect(source, plate_index)
        with path.open("ab") as writer:
            writer.write(b"changed")
        return facts

    monkeypatch.setattr(ams_source_inspection, "inspect_single_source_ams", changed)
    with pytest.raises(PayloadChanged, match="changed during inspection"):
        inspect_ams_payload(path, expected_sha256=hashlib.sha256(raw).hexdigest(),
                            expected_bytes=len(raw), plate_index=0, already_staged=True)

@pytest.mark.parametrize("char", ["a", "🚀"])
def test_export_header_bounded_for_512_long_names(project_client, monkeypatch, char):
    from modules.archives.routes import projects

    omitted = [{"original_name": f"{i:03}" + char * 252} for i in range(512)]
    monkeypatch.setattr(projects, "build_project_zip_with_report", lambda *_: (b"zip", omitted))
    client, issue = project_client
    header = client.get("/projects/101/export", headers=issue(["read:jobs"])).headers["X-Odin-Export-Omitted"]
    assert len(header.encode("ascii")) < 1024
    summary = json.loads(header)
    assert summary["count"] == 512 and len(summary["names"]) == 5
    for item, name in zip(omitted, summary["names"]):
        assert 3 < len(name) <= 80 and item["original_name"].startswith(name)


def test_complete_export_validates_with_280_manifest(project_client):
    from typing import Literal
    from pydantic import BaseModel, ConfigDict, Field
    from modules.archives.project_zip_manifest import ImportedArchive, ProjectDisplay, MAX_ARCHIVES

    class LegacyManifest(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        version: Literal[2]
        project: ProjectDisplay
        archives: list[ImportedArchive] = Field(max_length=MAX_ARCHIVES)

    client, issue = project_client
    response = client.get("/projects/101/export", headers=issue(["read:jobs"]))
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as package:
        LegacyManifest.model_validate_json(package.read("project.json"))


@pytest.mark.parametrize("path", ["/print-files/upload", "/prints"])
@pytest.mark.parametrize("size", [16, 700 * 1024**2])
def test_upload_requires_credentials_before_body_and_prechecks(upload_client, monkeypatch, path, size):
    from modules.models_library import upload_stream

    client, _, _ = upload_client
    monkeypatch.setattr(upload_stream, "_check_disk_space", lambda *_: pytest.fail("anonymous disk check"))
    reads = []
    async def counted(scope, receive, send):
        async def count():
            message = await receive()
            reads.append(len(message.get("body", b"")))
            return message
        await client.app(scope, count, send)

    response = TestClient(counted).post(path, content=b"test", headers={"Content-Length": str(size)})
    assert response.status_code == 401
    assert reads == []


@pytest.mark.parametrize("exception", [MemoryError, "member"])
@pytest.mark.parametrize("caller", ["gcode", "sliced", "unsliced"])
def test_metadata_refusal_reaches_caller(upload_client, review_db, tmp_path, monkeypatch, exception, caller):
    from modules.models_library import print_file_meta, threemf_parser as tp

    def fail(*args):
        if exception == "member":
            raise tp.MemberTooLargeError("project_settings.config", 100)
        raise MemoryError()

    monkeypatch.setattr(print_file_meta, "extract_print_file_meta", fail)
    if caller == "gcode":
        client, issue, storage = upload_client
        response = client.post("/print-files/upload", headers=issue(["write:models"]),
                               files={"file": ("part.gcode", b"G28")})
        assert response.status_code == 400
        assert "file is too large to check" in response.json()["detail"]
        assert review_db.execute(text("SELECT id FROM print_files WHERE filename = 'part.gcode'")).first() is None
        assert review_db.execute(text("SELECT id FROM models WHERE name = 'part'")).first() is None
        assert not list(storage.iterdir())
    else:
        path = tmp_path / "part.3mf"
        path.write_bytes(_zip_bytes({"Metadata/plate_1.gcode": b"G28"}))
        with zipfile.ZipFile(path) as package, pytest.raises(ValueError, match="file is too large to check"):
            if caller == "sliced":
                tp.parse_3mf(path)
            else:
                tp.parse_unsliced_3mf(package, path)


@pytest.mark.parametrize("case", ["queue_full", "deadline"])
def test_rack_preview_refuses_busy_or_late_scan(tmp_path, monkeypatch, caplog, case):
    from core import ams_mapping
    from modules.models_library import mesh_isolation as pool

    path = tmp_path / "plate.3mf"
    path.touch()
    finished = threading.Event()
    release = threading.Event()
    def scan(*args):
        try:
            assert case == "deadline"
            assert release.wait(2), "preview waited for scan instead of returning at deadline"
            return ()
        finally:
            finished.set()
    monkeypatch.setattr(ams_mapping, "_rack_sources", scan)
    monkeypatch.setattr(pool, "TIMEOUT_SECONDS", 0.1)
    if case == "queue_full":
        def refuse(*args):
            raise pool.Busy()
        monkeypatch.setattr(pool, "reserve", refuse)
    try:
        assert ams_mapping.read_rack_sources(path, 0) == []
        assert "Could not read rack tools" in caplog.text
        assert ("Busy" if case == "queue_full" else "TimeoutError") in caplog.text
    finally:
        release.set()
    if case == "deadline":
        assert finished.wait(1)


@pytest.mark.parametrize("model", ["H3C", "H2D"])
def test_multi_source_dispatch_gcode_opens(review_db, tmp_path, monkeypatch, request, model):
    from modules.printers import dispatch
    from tests.test_contracts.test_multi_source_ams_dispatch import setup_multi, owned_read

    events = []
    if model == "H3C":
        request.getfixturevalue("target")
    else:
        engine, _, report = setup_multi(review_db, tmp_path, monkeypatch, model=model,
                                        mapping=[2, 4], extruders=[0, 1])
        owned_read(monkeypatch, engine, report, events, slots=(3, 5))

    def adapter(*args, before_start, **kwargs):
        events.append("upload")
        ok = before_start.prepare() and before_start()
        return ok, "accepted" if ok else str(before_start.refusal)

    monkeypatch.setattr(dispatch, "_dispatch_bambu", adapter)
    opens = []
    original = zipfile.ZipFile.open
    def count(package, name, *args, **kwargs):
        member = getattr(name, "filename", name)
        if member.lower().endswith(".gcode"):
            opens.append(member)
        return original(package, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, "open", count)
    ok, message = dispatch.dispatch_job(59, 53)
    assert ok, message
    assert events[0] == "upload"
    assert 0 < len(opens) <= 2, opens
