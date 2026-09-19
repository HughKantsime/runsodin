from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from modules.system.backup_service import BackupValidationError  # noqa: E402
from modules.system.postgres_backup_service import (  # noqa: E402
    _credentialless_cli_url,
    _pending_rollback_path,
    _postgres_password,
    _read_pending_manifest,
    canonical_tables,
    postgres_paths,
    validate_archive_toc,
)
from core.schema.bootstrap import RAW_REQUIRED_TABLES  # noqa: E402


def _toc(*, omit: str | None = None, extra: str | None = None) -> str:
    lines = [
        f"{index}; 1259 {10000 + index} TABLE public {table_name} odin"
        for index, table_name in enumerate(sorted(canonical_tables()), start=1)
        if table_name != omit
    ]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def test_toc_requires_exact_canonical_tables() -> None:
    result = validate_archive_toc(_toc())
    assert result["table_count"] == len(canonical_tables())

    with pytest.raises(BackupValidationError, match="missing required"):
        validate_archive_toc(_toc(omit="users"))
    with pytest.raises(BackupValidationError, match="non-ODIN table"):
        validate_archive_toc(
            _toc(extra="999; 1259 99999 TABLE public unrelated_customer_table odin")
        )


def test_canonical_tables_cover_every_raw_bootstrap_table() -> None:
    assert RAW_REQUIRED_TABLES <= canonical_tables()
    assert "education_commands" in canonical_tables()
    assert "education_monitor_claims" in canonical_tables()


def test_toc_rejects_executable_or_nonpublic_objects() -> None:
    with pytest.raises(BackupValidationError, match="forbidden object"):
        validate_archive_toc(
            _toc(extra="999; 1255 99999 FUNCTION public unsafe_function() odin")
        )
    with pytest.raises(BackupValidationError, match="outside public"):
        validate_archive_toc(
            _toc(extra="999; 1259 99999 TABLE private users odin")
        )


def test_toc_allows_only_canonicalized_trigger_structure() -> None:
    canonical_trigger = (
        "999; 2620 99999 TRIGGER public education_audit_events "
        "trg_education_audit_no_update odin"
    )
    validate_archive_toc(
        _toc(extra=canonical_trigger), allow_unresolved_structure=True
    )
    with pytest.raises(BackupValidationError, match="non-ODIN structural object"):
        validate_archive_toc(_toc(extra=canonical_trigger))


@pytest.mark.parametrize(
    "entry",
    (
        "999; 1259 99999 SEQUENCE public unrelated_sequence odin",
        "999; 0 99999 SEQUENCE SET public unrelated_sequence odin",
        "999; 1259 99999 INDEX public unrelated_index odin",
        "999; 2606 99999 CONSTRAINT public users unrelated_constraint odin",
        "999; 2604 99999 DEFAULT public users unrelated_default odin",
        "999; 2606 99999 FK CONSTRAINT public users unrelated_fk odin",
    ),
)
def test_toc_rejects_nonallowlisted_structural_entries(entry: str) -> None:
    with pytest.raises(BackupValidationError, match="non-ODIN structural object"):
        validate_archive_toc(_toc(extra=entry))


def test_cli_url_never_contains_a_password() -> None:
    value = _credentialless_cli_url(
        "postgresql://odin@postgres:5432/odin", "odin-backup"
    )
    assert "odin-backup" in value
    assert "@postgres" in value
    with pytest.raises(BackupValidationError, match="Credential-bearing"):
        _credentialless_cli_url(
            "postgresql://odin:visible-secret@postgres:5432/odin", "odin-backup"
        )


def test_postgres_password_is_read_from_restricted_mounted_file(tmp_path: Path) -> None:
    secret = tmp_path / "password"
    secret.write_text("fixture:secret\\value\n", encoding="utf-8")
    secret.chmod(0o644)
    with pytest.raises(BackupValidationError, match="too permissive"):
        _postgres_password(str(secret))
    secret.chmod(0o600)
    assert _postgres_password(str(secret)) == "fixture:secret\\value"


def test_postgres_paths_are_scoped_to_data_directory(tmp_path: Path) -> None:
    paths = postgres_paths(tmp_path)
    assert paths.backups.parent == tmp_path.resolve()
    assert paths.pending.name == "restore-pending.dump"
    assert paths.manifest.name == "restore-pending-postgres.json"


def test_interrupted_restore_manifest_resolves_only_verified_rollback(
    tmp_path: Path,
) -> None:
    paths = postgres_paths(tmp_path)
    paths.backups.mkdir(parents=True)
    rollback = paths.backups / "rollback_20260912_130000_123456.dump"
    rollback.write_bytes(b"x" * 100)
    import hashlib

    manifest = {
        "schema_version": 1,
        "dialect": "postgresql",
        "state": "restore_in_progress",
        "rollback_backup": rollback.name,
        "rollback_sha256": hashlib.sha256(rollback.read_bytes()).hexdigest(),
        "rollback_size_bytes": rollback.stat().st_size,
    }
    paths.manifest.write_text(__import__("json").dumps(manifest), encoding="utf-8")

    assert _read_pending_manifest(paths)["state"] == "restore_in_progress"
    assert _pending_rollback_path(paths, manifest) == rollback.resolve()

    manifest["rollback_backup"] = "../outside.dump"
    with pytest.raises(BackupValidationError, match="invalid rollback metadata"):
        _pending_rollback_path(paths, manifest)


def test_pending_manifest_rejects_unknown_state(tmp_path: Path) -> None:
    paths = postgres_paths(tmp_path)
    paths.manifest.write_text(
        '{"schema_version": 1, "dialect": "postgresql", "state": "done"}',
        encoding="utf-8",
    )
    with pytest.raises(BackupValidationError, match="unsupported"):
        _read_pending_manifest(paths)
