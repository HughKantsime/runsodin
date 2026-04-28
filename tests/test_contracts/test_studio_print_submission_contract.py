"""
Contract tests — ODIN Studio sliced-file submission endpoint.

ODIN Studio v1.0 posts multipart sliced G-code to POST /api/v1/prints.
The backend contract must be queue-only: upload/link a print file and create
a pending job, but never start printer hardware.
"""

import ast
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI not installed")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
PRINT_FILES = BACKEND_DIR / "modules" / "models_library" / "routes" / "print_files.py"
DRY_RUN = BACKEND_DIR / "core" / "middleware" / "dry_run.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


@pytest.fixture(scope="module")
def route_src() -> str:
    src = _get_function_source(PRINT_FILES.read_text(), "submit_studio_print")
    assert src, "submit_studio_print missing from print_files.py"
    return src


def test_studio_print_route_registered(route_src: str):
    assert '@router.post("/prints"' in PRINT_FILES.read_text()
    assert "file: UploadFile = File(...)" in route_src
    assert "target_printer_id: int = Form(...)" in route_src
    assert "profile_metadata: Optional[str] = Form(None)" in route_src
    assert "note: Optional[str] = Form(None)" in route_src


def test_studio_print_uses_agent_write_auth(route_src: str):
    assert re.search(r"require_role\(\s*['\"]operator['\"]\s*\)", route_src)
    assert re.search(
        r"require_any_scope\(\s*['\"]admin['\"]\s*,\s*AGENT_WRITE_SCOPE\s*\)",
        route_src,
    )


def test_studio_print_supports_registered_dry_run(route_src: str):
    dry_src = DRY_RUN.read_text()
    assert '("POST", "/api/v1/prints")' in dry_src
    assert "is_dry_run(request)" in route_src
    assert "dry_run_preview(" in route_src
    dry_run_pos = route_src.index("is_dry_run(request)")
    assert dry_run_pos < route_src.index("upload_3mf(")
    assert dry_run_pos < route_src.index("INSERT INTO jobs")
    assert "queue_studio_print" in route_src
    assert "No DB, filesystem, or " in route_src
    assert "printer side effects run on dry-run." in route_src


def test_studio_print_is_queue_only(route_src: str):
    forbidden_calls = (
        "dispatch_job(",
        "_send_printer_command(",
        "run_command(",
        "upload_and_start",
        "start_print(",
    )
    for call in forbidden_calls:
        assert call not in route_src
    assert "'pending'" in route_src
    assert '"queue_only"' in route_src
    assert '"dispatch_required": True' in route_src


def test_studio_print_response_contract(route_src: str):
    for key in (
        '"id": str(job_id)',
        '"status": "queued"',
        '"queued_position": queued_position',
        '"job_id": job_id',
        '"file_id": file_id',
        '"target_printer_id": target_printer_id',
        '"project_name": uploaded.get("project_name")',
        '"start_mode": "queue_only"',
        '"next_actions": build_next_actions(',
    ):
        assert key in route_src


def test_profile_metadata_validation_uses_odin_error():
    src = PRINT_FILES.read_text()
    helper_src = _get_function_source(src, "_parse_studio_profile_metadata")
    assert "json.loads(raw)" in helper_src
    assert "OdinError(" in helper_src
    assert "ErrorCode.validation_failed" in helper_src
