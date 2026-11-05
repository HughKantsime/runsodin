"""Bounded streaming, exact caps and late corruption on synthetic plates."""
import asyncio
import hashlib
import io
import zipfile
from pathlib import Path
import pytest
from core import ams_source_inspection as ams
from core import print_payload_snapshot as snapshot
from core.zip_bounds import ArchiveRefused, check_expansion, open_bounded
from tests.test_contracts.test_ams_source_inspection import archive


def test_fixed_limits_agree():
    from core.print_upload_limits import MAX_UPLOAD_BYTES
    from modules.organizations.education_submission_service import MAX_UPLOAD_BYTES as education
    assert ams.MAX_GCODE_BYTES == 256 * 1024**2
    assert ams.MAX_ARCHIVE_BYTES == MAX_UPLOAD_BYTES == education == snapshot.MAX_PAYLOAD_BYTES == 160 * 1024**2


def test_zip_member_is_streamed_to_eof(monkeypatch):
    payload = archive(code='; filament_type = PETG\nM620 S0A\nM621 S0A\n' + 'G1 X1 Y2 E0.1\n' * 10000)
    original = zipfile.ZipExtFile.read
    requests = []
    def bounded(self, n=-1):
        if self.name.endswith('.gcode'):
            assert 0 <= n <= ams.MAX_GCODE_LINE_BYTES + 1
            requests.append(n)
        return original(self, n)
    monkeypatch.setattr(zipfile.ZipExtFile, 'read', bounded)
    assert ams.inspect_ams_sources(io.BytesIO(payload)).sources[0].material == 'PETG'
    assert len(requests) > 1


@pytest.mark.parametrize('tail', ['T1\n', ';hidden\rT1\n', ';' + 'x' * (128 * 1024) + '\n', '; filament_type = PETG\n'])
def test_checks_still_apply_after_many_chunks(tail):
    with pytest.raises(ValueError):
        ams.inspect_ams_sources(archive(code='; filament_type = PETG\nM620 S0A\nM621 S0A\n' + 'G1 X1 Y2 E0.1\n' * 10000 + tail))


def test_member_exact_cap_and_plus_one(monkeypatch):
    code = '; filament_type = PETG\nM620 S0A\nM621 S0A\n'
    monkeypatch.setattr(ams, 'MAX_GCODE_BYTES', len(code))
    assert ams.inspect_ams_sources(archive(code=code)).sources
    with pytest.raises(ValueError, match='too large.*limit'):
        ams.inspect_ams_sources(archive(code=code + '\n'))


def test_archive_exact_cap_and_plus_one(monkeypatch):
    payload = archive()
    monkeypatch.setattr(ams, 'MAX_ARCHIVE_BYTES', len(payload))
    assert ams.inspect_ams_sources(io.BytesIO(payload)).sources
    with pytest.raises(ValueError, match='too large.*limit'):
        ams.inspect_ams_sources(io.BytesIO(payload + b' '))


def test_ratio_and_member_count_still_refuse():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr('bomb', b'0' * 1000000)
    with open_bounded(payload) as package, pytest.raises(ArchiveRefused, match='compression_ratio'):
        check_expansion(package)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w') as package:
        for n in range(513):
            package.writestr(str(n), b'')
    with pytest.raises(ArchiveRefused, match='too_many_members'):
        open_bounded(payload)


def test_service_passes_disk_stream_and_keeps_hash_and_size_binding(tmp_path, monkeypatch):
    from modules.printers.services import inspect_ams_payload
    path = tmp_path / 'synthetic.3mf'
    path.write_bytes(archive())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    original = ams.inspect_single_source_ams
    def checked(payload, plate):
        assert not isinstance(payload, bytes)
        return original(payload, plate)
    monkeypatch.setattr(ams, 'inspect_single_source_ams', checked)
    assert inspect_ams_payload(path, expected_sha256=digest, expected_bytes=path.stat().st_size, plate_index=0)['material'] == 'PETG'
    with pytest.raises(ValueError, match='changed'):
        inspect_ams_payload(path, expected_sha256=digest, expected_bytes=1, plate_index=0)
    with pytest.raises(snapshot.PayloadUnavailable, match='changed'):
        inspect_ams_payload(path, expected_sha256='0' * 64, expected_bytes=path.stat().st_size, plate_index=0)


@pytest.mark.parametrize('excess', [0, 1])
def test_upload_bounded_chunks_hash_limit_and_cleanup(tmp_path, monkeypatch, excess):
    from fastapi import HTTPException
    from modules.models_library import upload_stream as upload
    monkeypatch.setattr(upload.tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(upload, 'MAX_UPLOAD_BYTES', 2 * upload.CHUNK_BYTES)
    remaining = 2 * upload.CHUNK_BYTES + excess
    class Source:
        async def read(self, size):
            nonlocal remaining
            assert 0 < size <= upload.CHUNK_BYTES
            n = min(size, remaining)
            remaining -= n
            return b'x' * n
    async def run():
        async with upload.staged_upload(Source(), '.3mf') as (path, digest):
            assert Path(path).stat().st_size == 2 * upload.CHUNK_BYTES
            assert digest == hashlib.sha256(b'x' * (2 * upload.CHUNK_BYTES)).hexdigest()
    if excess:
        with pytest.raises(HTTPException) as error:
            asyncio.run(run())
        assert error.value.status_code == 413 and 'bytes' in error.value.detail and 'limit' in error.value.detail
    else:
        asyncio.run(run())
    assert not list(tmp_path.iterdir())


def test_upload_cancel_removes_partial_file(tmp_path, monkeypatch):
    from modules.models_library import upload_stream as upload
    monkeypatch.setattr(upload.tempfile, 'tempdir', str(tmp_path))
    class Source:
        calls = 0
        async def read(self, size):
            self.calls += 1
            if self.calls > 1:
                raise asyncio.CancelledError()
            return b'x'
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(upload.write_upload(Source(), '.3mf'))
    assert not list(tmp_path.iterdir())


def test_capability_tail_seek_uses_small_reads(tmp_path, monkeypatch):
    from ops.large_plate_benchmark import make_plate
    from modules.printers.dispatch_capability_payload import inspect_payload
    path = tmp_path / 'synthetic.3mf'
    make_plate(path, 8)
    original = zipfile.ZipExtFile.read
    def bounded(self, n=-1):
        if self.name.endswith('.gcode'):
            assert 0 <= n <= 3 * 1024**2
        return original(self, n)
    monkeypatch.setattr(zipfile.ZipExtFile, 'read', bounded)
    assert inspect_payload(path, 1, 'H2D')['mapping_refusal'] is None


def test_zip_readline_fragments_are_never_used(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('ZipExtFile.readline is not a reliable line boundary')
    monkeypatch.setattr(zipfile.ZipExtFile, 'readline', forbidden)
    assert ams.inspect_ams_sources(archive()).sources


@pytest.mark.parametrize('kind', ['materials', 'models', 'switches'])
def test_repeated_evidence_cannot_accumulate_with_file_size(monkeypatch, kind):
    from tests.test_contracts.test_ams_source_inspection import p2s_archive
    filler = 'G1 X1 Y2 E0.1\n' * 100000
    if kind == 'models':
        payload = p2s_archive(lambda code: code.replace('; printer_model = Bambu Lab P2S\n',
            '; printer_model = Bambu Lab P2S\n; printer_model = Bambu Lab P2S\n') + '; synthetic padding\n' * 100000)
    else:
        bad = '; filament_type = PETG\n' if kind == 'materials' else 'M620 S9A\n'
        payload = archive(code='; filament_type = PETG\nM620 S0A\nM621 S0A\n' + bad + filler)
    original = zipfile.ZipExtFile.read
    consumed = 0
    def counted(self, n=-1):
        nonlocal consumed
        value = original(self, n)
        if self.name.endswith('.gcode'):
            consumed += len(value)
        return value
    monkeypatch.setattr(zipfile.ZipExtFile, 'read', counted)
    with pytest.raises(ValueError):
        ams.inspect_ams_sources(payload)
    assert consumed <= 128 * 1024


def test_worker_ratio_refusal_is_not_reported_as_member_count(tmp_path):
    from modules.models_library.mesh_worker import _analysis, _plates
    path = tmp_path / 'synthetic-ratio.3mf'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr('Metadata/plate_1.gcode', b'G1 X1\n' * 100000)
    assert _analysis(str(path), 'synthetic', 'upload')['status'] == 'bad_zip'
    assert _plates(str(path))['status'] == 'bad_zip'


def test_worker_preserves_specific_plate_json_size_refusal(tmp_path):
    from modules.models_library.mesh_worker import _analysis, _plates
    path = tmp_path / 'synthetic-oversize-json.3mf'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr('Metadata/plate_2.json', b'{}' * (3 * 1024**2))
    assert _analysis(str(path), 'synthetic', 'upload')['status'] == 'plate_json_too_large'
    assert _plates(str(path))['status'] == 'plate_json_too_large'


@pytest.mark.parametrize('bound', ['ratio', 'member', 'total'])
def test_ams_checks_expansion_of_unused_members(monkeypatch, bound):
    from core import zip_bounds
    payload = io.BytesIO(archive())
    with zipfile.ZipFile(payload, 'a', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr('unused-padding', b'x' * 100000)
    if bound != 'ratio':
        monkeypatch.setattr(zip_bounds, 'MAX_COMPRESSION_RATIO', 1000000)
        monkeypatch.setattr(zip_bounds, 'MAX_MEMBER_BYTES' if bound == 'member' else 'MAX_TOTAL_BYTES', 99999)
    with pytest.raises(ValueError):
        ams.inspect_ams_sources(payload.getvalue())
