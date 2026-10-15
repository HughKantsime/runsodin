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
    from modules.system.pre026_restore_stage import identify_contract, PRESERVED029
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
            assert routed is PRESERVED029 and ids == [], (version, "needs a preservedNNN bundle before this source ships")


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
