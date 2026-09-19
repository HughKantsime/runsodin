from __future__ import annotations

import io
import json
import sys
import uuid
import zipfile
import asyncio
import threading
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def submission_db(tmp_path: Path):
    from core.schema import bootstrap_database

    engine = create_engine(f"sqlite:///{tmp_path / 'submissions.db'}")
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO groups (id,name,is_org) VALUES (1,'school',1)"))
        connection.execute(
            text(
                "INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) VALUES "
                "(1,'student','student@example.test','fixture','viewer',1,1),"
                "(2,'manager','manager@example.test','fixture','viewer',1,1),"
                "(3,'admin','admin@example.test','fixture','admin',1,1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO education_cost_centers "
                "(id,org_id,name_key,code_key,display_name,code,state,revision,created_by) "
                "VALUES (7,1,'engineering','eng','Engineering','ENG','active',1,3)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO education_cost_center_grants "
                "(org_id,cost_center_id,user_id,role,state,granted_by) VALUES "
                "(1,7,1,'student','active',3),(1,7,2,'manager','active',3)"
            )
        )
    session = Session(engine)
    try:
        yield session, tmp_path / "uploads"
    finally:
        session.close()
        engine.dispose()


def _principal():
    return {
        "id": 1,
        "username": "student",
        "role": "viewer",
        "group_id": 1,
        "is_active": True,
        "_auth_kind": "session_jwt",
    }


def _sliced_3mf() -> bytes:
    output = io.BytesIO()
    slice_info = """<config><plate>
    <metadata key="prediction" value="3600"/>
    <metadata key="weight" value="12.5"/>
    <metadata key="nozzle_diameters" value="0.4"/>
    <metadata key="printer_model_id" value="BL-P001"/>
    <filament id="1" type="PLA" color="#FFFFFF" used_m="4" used_g="12.5"/>
    </plate></config>"""
    project = {
        "printer_model": "Bambu Lab P1S",
        "nozzle_diameter": ["0.4"],
        "filament_type": ["PLA"],
        "bed_shape": "0x0,256x0,256x256,0x256",
    }
    model = """<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
    <metadata name="Title">Class Gear</metadata><resources/><build/></model>"""
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("Metadata/slice_info.config", slice_info)
        archive.writestr("Metadata/project_settings.config", json.dumps(project))
        archive.writestr("Metadata/plate_1.json", json.dumps({"nozzle_diameter": 0.4}))
        archive.writestr("Metadata/plate_1.gcode", "; sliced\nG28\n")
        archive.writestr("3D/3dmodel.model", model)
    return output.getvalue()


def _ample_disk(monkeypatch):
    from modules.organizations import education_submission_service as service

    usage = namedtuple("usage", "total used free")(
        200 * 1024**3, 20 * 1024**3, 180 * 1024**3
    )
    monkeypatch.setattr(service.shutil, "disk_usage", lambda path: usage)


def test_streamed_submission_commits_graph_accounting_audit_and_outbox(
    submission_db, monkeypatch
):
    from modules.organizations.education_submission_service import (
        process_sliced_3mf_submission,
    )

    db, upload_root = submission_db
    _ample_disk(monkeypatch)
    token = str(uuid.uuid4())
    payload = _sliced_3mf()
    result = process_sliced_3mf_submission(
        db,
        source=io.BytesIO(payload),
        original_filename="class-gear.3mf",
        operation_id=token,
        principal=_principal(),
        cost_center_id=7,
        storage_root=upload_root,
    )

    assert result["status"] == "submitted"
    operation = db.execute(
        text(
            "SELECT state,reserved_bytes,accounted_bytes,reservation_released_at,final_path "
            "FROM education_upload_operations WHERE operation_id=:id"
        ),
        {"id": token},
    ).one()
    assert operation.state == "committed"
    assert operation.reserved_bytes == len(payload)
    assert operation.accounted_bytes == len(payload)
    assert operation.reservation_released_at is not None
    assert Path(operation.final_path).read_bytes() == payload
    accounts = db.execute(
        text(
            "SELECT scope_kind,reserved_bytes,accounted_bytes FROM education_storage_accounts "
            "ORDER BY scope_kind"
        )
    ).all()
    assert [(row.scope_kind, row.reserved_bytes, row.accounted_bytes) for row in accounts] == [
        ("tenant", 0, len(payload)),
        ("user", 0, len(payload)),
    ]
    assert db.execute(text("SELECT COUNT(*) FROM education_audit_events")).scalar_one() == 1
    assert db.execute(text("SELECT COUNT(*) FROM education_notification_outbox")).scalar_one() == 2
    facts = json.loads(
        db.execute(
            text("SELECT compatibility_facts_json FROM print_files WHERE id=:id"),
            {"id": result["print_file_id"]},
        ).scalar_one()
    )
    assert facts["api_types"]["value"] == ["bambu"]
    assert facts["bed"]["source_key"] == "bed_shape"

    replay = process_sliced_3mf_submission(
        db,
        source=io.BytesIO(payload),
        original_filename="class-gear.3mf",
        operation_id=token,
        principal=_principal(),
        cost_center_id=7,
        storage_root=upload_root,
    )
    assert replay == result

    with pytest.raises(Exception, match="different file"):
        process_sliced_3mf_submission(
            db,
            source=io.BytesIO(payload + b"different"),
            original_filename="class-gear.3mf",
            operation_id=token,
            principal=_principal(),
            cost_center_id=7,
            storage_root=upload_root,
        )


def test_malformed_archive_aborts_and_releases_reservation(submission_db, monkeypatch):
    from modules.organizations.education_submission_service import (
        abort_upload,
        process_sliced_3mf_submission,
    )

    db, upload_root = submission_db
    _ample_disk(monkeypatch)
    token = str(uuid.uuid4())
    with pytest.raises(Exception, match="Invalid .3mf container"):
        process_sliced_3mf_submission(
            db,
            source=io.BytesIO(b"not-a-zip"),
            original_filename="broken.3mf",
            operation_id=token,
            principal=_principal(),
            cost_center_id=7,
            storage_root=upload_root,
        )
    operation = db.execute(
        text(
            "SELECT state,reservation_released_at FROM education_upload_operations "
            "WHERE operation_id=:id"
        ),
        {"id": token},
    ).one()
    assert operation.state == "aborted"
    assert operation.reservation_released_at is not None
    assert db.execute(
        text("SELECT SUM(reserved_bytes) FROM education_storage_accounts")
    ).scalar_one() == 0
    assert not any(upload_root.rglob("*.*"))

    abort_upload(db, operation_id=token, paths=())
    assert db.execute(
        text("SELECT SUM(reserved_bytes) FROM education_storage_accounts")
    ).scalar_one() == 0


def test_attempt_limits_are_persistent_and_rejected_attempt_is_not_counted(submission_db):
    from core.errors import OdinError
    from modules.organizations.education_submission_service import begin_upload_attempt

    db, _ = submission_db
    instant = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    for _ in range(10):
        begin_upload_attempt(
            db,
            operation_id=str(uuid.uuid4()),
            principal=_principal(),
            cost_center_id=7,
            now=instant,
        )
    with pytest.raises(OdinError) as rejected:
        begin_upload_attempt(
            db,
            operation_id=str(uuid.uuid4()),
            principal=_principal(),
            cost_center_id=7,
            now=instant,
        )
    assert rejected.value.code.value == "rate_limited"
    counts = db.execute(
        text(
            "SELECT bucket_kind,attempt_count FROM education_rate_counters "
            "WHERE scope_kind='user' ORDER BY bucket_kind"
        )
    ).all()
    assert [(row.bucket_kind, row.attempt_count) for row in counts] == [
        ("day", 10),
        ("hour", 10),
    ]


def test_concurrent_first_use_claims_token_once_and_counts_one_attempt(submission_db):
    from core.errors import OdinError
    from modules.organizations.education_submission_service import begin_upload_attempt

    db, _ = submission_db
    engine = db.get_bind()
    token = str(uuid.uuid4())
    barrier = threading.Barrier(2)

    def claim():
        with Session(engine) as session:
            barrier.wait()
            try:
                result = begin_upload_attempt(
                    session,
                    operation_id=token,
                    principal=_principal(),
                    cost_center_id=7,
                )
                return "claimed" if result is None else "replayed"
            except OdinError as exc:
                return exc.code.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        outcomes = sorted(future.result() for future in futures)
    assert outcomes == ["claimed", "idempotency_conflict"]
    counts = db.execute(
        text(
            "SELECT bucket_kind,attempt_count FROM education_rate_counters "
            "WHERE scope_kind='user' ORDER BY bucket_kind"
        )
    ).all()
    assert [(row.bucket_kind, row.attempt_count) for row in counts] == [
        ("day", 1),
        ("hour", 1),
    ]


def test_reconciliation_without_graph_cleans_file_and_releases_once(
    submission_db, monkeypatch
):
    from modules.organizations.education_submission_service import (
        begin_upload_attempt,
        prepare_upload_paths,
        reconcile_upload_operation,
        reserve_storage_chunk,
    )

    db, upload_root = submission_db
    _ample_disk(monkeypatch)
    token = str(uuid.uuid4())
    assert begin_upload_attempt(
        db,
        operation_id=token,
        principal=_principal(),
        cost_center_id=7,
    ) is None
    staging_path, _ = prepare_upload_paths(
        db,
        operation_id=token,
        principal=_principal(),
        original_filename="uncertain.3mf",
        storage_root=upload_root,
    )
    payload = b"uncertain"
    reserve_storage_chunk(
        db,
        operation_id=token,
        amount=len(payload),
        storage_root=upload_root,
    )
    staging_path.write_bytes(payload)
    db.execute(
        text(
            "UPDATE education_upload_operations SET state='needs_reconciliation',"
            "cleanup_path=staging_path WHERE operation_id=:id"
        ),
        {"id": token},
    )
    db.commit()

    assert reconcile_upload_operation(db, operation_id=token) is None
    operation = db.execute(
        text(
            "SELECT state,reservation_released_at FROM education_upload_operations "
            "WHERE operation_id=:id"
        ),
        {"id": token},
    ).one()
    assert operation.state == "aborted"
    assert operation.reservation_released_at is not None
    assert not staging_path.exists()
    assert db.execute(
        text("SELECT SUM(reserved_bytes) FROM education_storage_accounts")
    ).scalar_one() == 0


def test_http_multipart_parser_reserves_each_chunk_before_direct_disk_write(
    submission_db, monkeypatch
):
    from starlette.datastructures import Headers
    from modules.organizations.routes_education_submissions import (
        _DurableMultipartParser,
    )
    from modules.organizations.education_submission_service import abort_upload

    db, upload_root = submission_db
    _ample_disk(monkeypatch)
    boundary = "odin-education-boundary"
    payload = _sliced_3mf()
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"cost_center_id\"\r\n\r\n7\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"submission_token\"\r\n\r\n{uuid.uuid4()}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"part.3mf\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()

    async def stream():
        for offset in range(0, len(body), 97):
            yield body[offset : offset + 97]

    parser = _DurableMultipartParser(
        Headers({"content-type": f"multipart/form-data; boundary={boundary}"}),
        stream(),
        db=db,
        principal=_principal(),
        storage_root=upload_root,
        max_files=1,
        max_fields=2,
        max_part_size=1024,
    )
    form = asyncio.run(parser.parse())
    upload = form["file"]
    try:
        assert upload.retain is True
        assert upload.byte_count == len(payload)
        assert parser.staging_path.read_bytes() == payload
        reserved = db.execute(
            text(
                "SELECT reserved_bytes FROM education_storage_accounts "
                "WHERE scope_kind='user'"
            )
        ).scalar_one()
        assert reserved == len(payload)
    finally:
        asyncio.run(upload.close())
        abort_upload(
            db,
            operation_id=parser.operation_id,
            paths=(parser.staging_path, parser.final_path),
        )
