"""Cert B round 6: stored untrusted 3MF packages are bounded and opened once.

Uploads refuse absurd archives (see test_cert_B_threemf_round6), so stored packages are
small to open; every parent-side reopen is still bounded from the end-of-central-directory
record before zipfile lists the directory, and validates several plates from one open.
Project ZIP import (029) keeps its own preflight member cap.
"""
import io
import zipfile

import pytest


def _many_members(path, count):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        for index in range(count):
            z.writestr(f"Metadata/m{index}.txt", "")
    return path


def test_ams_inspection_refuses_before_zipfile_lists_the_directory(tmp_path, monkeypatch):
    from core.ams_source_inspection import inspect_single_source_ams
    payload = _many_members(tmp_path / "many.3mf", 600).read_bytes()
    opened = []
    real = zipfile.ZipFile

    class Counting(real):
        def __init__(self, *a, **k):
            opened.append(True)
            super().__init__(*a, **k)
    monkeypatch.setattr(zipfile, "ZipFile", Counting)
    with pytest.raises(ValueError):
        inspect_single_source_ams(payload)
    assert opened == []  # refused from the end record; no 600-entry list was built


def test_schedule_options_opens_the_stored_package_once(tmp_path, monkeypatch):
    from core import print_payload
    path = tmp_path / "plates.3mf"
    with zipfile.ZipFile(path, "w") as z:
        for number in range(1, 6):
            z.writestr(f"Metadata/plate_{number}.gcode", "G1")
    opened = []
    real = zipfile.ZipFile

    class Counting(real):
        def __init__(self, *a, **k):
            opened.append(True)
            super().__init__(*a, **k)
    monkeypatch.setattr(zipfile, "ZipFile", Counting)
    numbers = print_payload.bambu_plate_numbers(str(path))
    for index in range(5):
        assert print_payload.validate_plate(str(path), index, "bambu", plate_numbers=numbers) == index + 1
    assert numbers == [1, 2, 3, 4, 5] and len(opened) == 1


def test_stored_absurd_package_is_refused_by_plate_validation(tmp_path):
    from fastapi import HTTPException
    from core.print_payload import validate_plate
    with pytest.raises(HTTPException) as refused:
        validate_plate(str(_many_members(tmp_path / "many.3mf", 600)), 0, "bambu")
    assert refused.value.status_code == 400 and "not a readable" in refused.value.detail  # refused, not listed


def test_project_zip_import_keeps_its_member_cap():
    from modules.archives import project_zip_admission
    from modules.archives.project_zip_manifest import MAX_ARCHIVES
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as z:
        z.writestr("project.json", "{}")
        for index in range(MAX_ARCHIVES + 1):
            z.writestr(f"files/{index:03d}.gcode", "G1")
    with pytest.raises(Exception):
        project_zip_admission.validate_project_zip(payload.getvalue())
