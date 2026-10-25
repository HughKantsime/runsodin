"""Release rule: every published release's post-027 ledger stays restorable.

A published release whose PostgreSQL archives record a post-027 ledger other than the
current POST027_MIGRATIONS (and not the empty 027-era one) would be refused as an
"intermediate post-027 build". Before such a source change ships, a preservedNNN bundle
must route that ledger. publish-image.yml refuses to publish a VERSION whose entry is
missing or differs from the candidate's registry.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from core.schema import preserved029
from core.schema.bootstrap import POST027_MIGRATIONS

from .test_preserved029_bundle import FAKE_PG_RESTORE, _ledger_sql

ROOT = Path(__file__).resolve().parents[2]
LEDGERS = ROOT / "backend/core/schema/released_post027_ledgers.json"


def _releases():
    return json.loads(LEDGERS.read_text())["releases"]


@pytest.fixture
def fake_pg_restore(tmp_path, monkeypatch):
    tools = tmp_path / "bin"; tools.mkdir()
    fake = tools / "pg_restore"; fake.write_text(FAKE_PG_RESTORE.format(python=sys.executable)); fake.chmod(0o755)
    secret = tmp_path / "secret"; secret.write_text("owned-test-only"); secret.chmod(0o600)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))
    archive = tmp_path / "archive.dump"; archive.write_bytes(b"x" * 200)
    return tmp_path, archive


def test_every_recorded_release_ledger_is_current_or_routed_by_a_bundle(fake_pg_restore, monkeypatch):
    from modules.system.pre026_restore_stage import identify_contract, PRESERVED029, PRESERVED034, PRESERVED035, PRESERVED036, PRESERVED037
    tmp_path, archive = fake_pg_restore
    current = [migration_id for migration_id, _ in POST027_MIGRATIONS]
    releases = _releases()
    assert "1.9.24" in releases and len(releases) >= 170
    for version, ids in releases.items():
        assert ids == [i for i in current if i in ids], (version, "ledger must be a registry prefix in order")
        ledger = tmp_path / f"{version}.sql"
        ledger.write_text(_ledger_sql(sorted(preserved029.ledger_ids()) + ids))
        monkeypatch.setenv("FAKE_LEDGER_FILE", str(ledger))
        routed = identify_contract(archive, "postgresql://o@pg/odin", None, tmp_path)
        if ids == current:
            assert routed is None, version
        else:
            assert (routed is PRESERVED029 and ids == []) or (routed is PRESERVED034 and ids == current[:3]) or (routed is PRESERVED035 and ids == current[:4]) or (routed is PRESERVED036 and ids == current[:5]) or (routed is PRESERVED037 and ids == current[:6]), (version, "needs a preservedNNN bundle before this source ships")


def test_every_ledger_variant_an_archive_can_record_is_routed_or_refused_by_name(fake_pg_restore, monkeypatch):
    """Migrations apply in registry order, so an archive's post-027 ids are a registry prefix,
    on top of a 025, 026 or 027-era base ledger. Only the full prefix (current) and the
    empty one (preserved029) restore; any other prefix is an intermediate build."""
    from core.schema import pre026_restore_contract as C
    from modules.system.backup_service import BackupValidationError
    from modules.system.pre026_restore_stage import identify_contract, PRESERVED029, PRESERVED034, PRESERVED035, PRESERVED036, PRESERVED037
    tmp_path, archive = fake_pg_restore
    from core.schema import bootstrap
    # The synthetic 099 registry is a future build: under it a released ledger such as 2.0.0's
    # is an intermediate prefix by construction (that release's archives would then need a
    # preservedNNN bundle), so the stranding check applies to the real registry only.
    registries = [(POST027_MIGRATIONS, True),
                  (POST027_MIGRATIONS + (("python:099-future-synthetic", "core.schema.migrations.099_future"),), False)]
    for registry, real in registries:
        monkeypatch.setattr(bootstrap, "POST027_MIGRATIONS", registry)
        _variants(tmp_path, archive, monkeypatch, [migration_id for migration_id, _ in registry], check_released=real)


def _variants(tmp_path, archive, monkeypatch, current, check_released=True):
    from core.schema import pre026_restore_contract as C
    from modules.system.backup_service import BackupValidationError
    from modules.system.pre026_restore_stage import identify_contract, PRESERVED029, PRESERVED034, PRESERVED035, PRESERVED036, PRESERVED037
    bases = {"025": [row[0] for row in C.EXPECTED_POSTGRES_LEDGER], "026": [row[0] for row in C.EXPECTED_POSTGRES_LEDGER026],
             "027": sorted(preserved029.ledger_ids())}
    releases = _releases()
    for base_name, base in bases.items():
        for size in range(len(current) + 1):
            prefix = current[:size]
            ledger = tmp_path / f"{base_name}-{len(current)}-{size}.sql"; ledger.write_text(_ledger_sql(base + prefix))
            monkeypatch.setenv("FAKE_LEDGER_FILE", str(ledger))
            if size == len(current):
                assert identify_contract(archive, "postgresql://o@pg/odin", None, tmp_path) is None
            elif size == 0:
                assert identify_contract(archive, "postgresql://o@pg/odin", None, tmp_path) is PRESERVED029
            elif frozenset(base + prefix) == __import__("core.schema.preserved034",fromlist=["ledger_ids"]).ledger_ids():
                assert identify_contract(archive, "postgresql://o@pg/odin", None, tmp_path) is PRESERVED034
            elif frozenset(base + prefix) == __import__('core.schema.preserved035', fromlist=['ledger_ids']).ledger_ids():
                assert identify_contract(archive, 'postgresql://o@pg/odin', None, tmp_path) is PRESERVED035
            elif frozenset(base + prefix) == __import__('core.schema.preserved036', fromlist=['ledger_ids']).ledger_ids():
                assert identify_contract(archive, 'postgresql://o@pg/odin', None, tmp_path) is PRESERVED036
            elif frozenset(base + prefix) == __import__('core.schema.preserved037', fromlist=['ledger_ids']).ledger_ids():
                assert identify_contract(archive, 'postgresql://o@pg/odin', None, tmp_path) is PRESERVED037
            else:
                with pytest.raises(BackupValidationError, match="intermediate post-027 build"):
                    identify_contract(archive, "postgresql://o@pg/odin", None, tmp_path)
                if check_released and base_name == "027":
                    assert prefix not in releases.values(), (prefix, "a released ledger would be stranded: add a preservedNNN bundle")


def test_publish_workflow_gate_without_a_ledger_file_fails_with_a_clear_message(tmp_path):
    candidate = _candidate(tmp_path, {"1.9.24": []})
    (candidate / "backend/core/schema/released_post027_ledgers.json").unlink()
    result = subprocess.run([sys.executable, "-P", "-c", _gate_script()], cwd=candidate, capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], "INPUT_VERSION": "2.0.0"}, timeout=60)
    assert result.returncode != 0 and "Traceback" not in result.stderr
    assert "released_post027_ledgers.json is missing" in result.stderr


def _gate_script():
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish-image.yml").read_text())
    steps = {step["name"]: step for step in workflow["jobs"]["publish"]["steps"]}
    names = list(steps)
    step = steps["Verify post-027 release ledger"]
    assert step["working-directory"] == "candidate" and "${{" not in step["run"]
    assert names.index("Verify candidate and version before registry login") < names.index("Verify post-027 release ledger") < names.index("Login with isolated registry configuration")
    body = step["run"]
    return body[body.index("<<'PY'\n") + len("<<'PY'\n"):body.rindex("PY")]


def _candidate(tmp_path, releases):
    candidate = tmp_path / "candidate"
    (candidate / "backend/core/schema").mkdir(parents=True)
    shutil.copy(ROOT / "backend/core/schema/bootstrap.py", candidate / "backend/core/schema/bootstrap.py")
    (candidate / "backend/core/schema/released_post027_ledgers.json").write_text(json.dumps({"schema_version": 1, "releases": releases}))
    return candidate


@pytest.mark.parametrize("case", ["missing", "mismatch", "match"])
def test_publish_workflow_gate_requires_a_matching_entry_for_version(tmp_path, case):
    current = [migration_id for migration_id, _ in POST027_MIGRATIONS]
    releases = {"1.9.24": []}
    if case == "mismatch": releases["2.0.0"] = []
    if case == "match": releases["2.0.0"] = current
    candidate = _candidate(tmp_path, releases)
    result = subprocess.run([sys.executable, "-P", "-c", _gate_script()], cwd=candidate, capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], "INPUT_VERSION": "2.0.0"}, timeout=60)
    assert (result.returncode == 0) is (case == "match"), result.stderr
    if case == "missing":
        assert "no entry for 2.0.0" in result.stderr
    if case == "mismatch":
        assert "registers" in result.stderr


def test_record_helper_appends_once_and_never_rewrites(tmp_path):
    sys.path.insert(0, str(ROOT))
    from ops.release_control import record_post027_ledger as helper
    candidate = _candidate(tmp_path, {"1.9.24": []})
    (candidate / "VERSION").write_text("2.0.0\n")
    assert helper.main(["--root", str(candidate)]) == 0
    current = [migration_id for migration_id, _ in POST027_MIGRATIONS]
    assert json.loads((candidate / "backend/core/schema/released_post027_ledgers.json").read_text())["releases"]["2.0.0"] == current
    assert helper.main(["--root", str(candidate)]) == 0
    with pytest.raises(SystemExit, match="cannot be rewritten"):
        helper.record(candidate, "1.9.24")
