"""preserved029: closed bundle, isolated child, archive routing and frozen-artifact identity."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from time import monotonic

import pytest

from core.schema import preserved029 as p
from core.schema.preserved029 import integrity

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "backend/core/schema"
BASE = Path(p.__file__).parent
SOURCE_COMMIT = "ba0b90254a2387de60a888a1939897eacf722c36"

# Byte digests at SOURCE_COMMIT. These artifacts are frozen: never edit or re-pin them.
FROZEN = {
    "pre026_restore_contract.py": "a69f865465cb478ad0c490ba7096a83f171d0714377b88d566994309d1ccbd9d",
    "pre026_restore_integrity.py": "2b83c4c2f7986c46fc7806dbcd9a290baf86467edc0eb17ad029126c5919236c",
    "prepriced_restore_contract.py": "2009b49bce52e7630422bbefce007defc79b560f5bcac1e01e07606c7792d5e7",
}
PRESERVED027_TREE = ("1ecbc53957108027428c17cb845da4ca8a7f4795030355fe42d146c226f82cfe", 121)
# The whole preserved029 package: parent bridge, runner, integrity, manifest and bundle.
# The manifest pin only covers the bundle; this covers the wrapper files too.
PRESERVED029_TREE = ("431a5cfdd7e0d7e5d72e91bcaea8d15aca581cc7af75e316e601998d7cd09ad2", 123)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(base):
    files = sorted(path for path in base.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(base).as_posix().encode() + b"\0" + _sha(path).encode() + b"\n")
    return digest.hexdigest(), len(files)


def test_frozen_restore_artifacts_are_byte_identical():
    for name, digest in FROZEN.items():
        assert _sha(SCHEMA / name) == digest, name
    assert _tree_digest(SCHEMA / "preserved027") == PRESERVED027_TREE
    assert _tree_digest(SCHEMA / "preserved029") == PRESERVED029_TREE


def _git_source(path):
    """Bytes of backend/<path> at SOURCE_COMMIT, or None when history is unavailable."""
    if shutil.which("git") is None:
        return None
    result = subprocess.run(["git", "-C", str(ROOT), "show", f"{SOURCE_COMMIT}:backend/{path}"], capture_output=True, timeout=30)
    return result.stdout if result.returncode == 0 else None


def test_every_bundle_file_is_the_source_commit_byte_for_byte():
    _, manifest = integrity.check_package()
    probe = _git_source("core/schema/bootstrap.py")
    if probe is None:
        pytest.skip(f"UNVERIFIED: git history with {SOURCE_COMMIT} is unavailable (shallow or exported checkout); "
                    "the preserved029 bundle cannot be compared with its source commit here")
    mismatched = [name for name in sorted(manifest["sources"])
                  if (data := _git_source(name)) is None or hashlib.sha256(data).hexdigest() != manifest["sources"][name]]
    assert mismatched == []


def test_bundle_is_the_exact_source_commit_restore_stage_closure():
    bundle, manifest = integrity.check_package()
    assert manifest["source_commit"] == SOURCE_COMMIT and manifest["profile"] == "restore-029"
    # The frozen contracts inside the bundle are the same bytes as the frozen live files.
    for name, digest in FROZEN.items():
        assert manifest["sources"]["core/schema/" + name] == digest
    # preserved027's closure plus the two files the 025/026 contract pins that it lacks, and
    # core/timelapse_recipes.py, which migration 019 reads by path for its own source pin.
    p27 = json.loads((SCHEMA / "preserved027/manifest.json").read_text())
    expected = set(p27["original_sources"]) | set(p27["producer_sources"]) | {
        "core/schema/pre026_restore_integrity.py", "modules/system/pre026_restore_stage.py", "core/timelapse_recipes.py"}
    assert set(manifest["sources"]) == expected
    # Every source pinned by the 025/026 contract is present with its pinned bytes.
    import ast, re
    text = (bundle / "core/schema/pre026_restore_contract.py").read_text()
    for symbol in ("SOURCE_PINS", "SQL_SOURCE_PINS", "EXTRA026_SOURCE_PINS"):
        pins = ast.literal_eval(re.search(r"^" + symbol + r"=(\{.*?\})$", text, re.M).group(1))
        assert all(manifest["sources"][name] == digest for name, digest in pins.items()), symbol
    assert b"030_" not in (bundle / "core/schema/migrator.py").read_bytes()
    assert len(manifest["ledger_ids"]) == 52 and "python:027-power-target-operations" in manifest["ledger_ids"]


@pytest.mark.parametrize("change", ["missing", "new", "changed", "file_symlink", "directory_symlink", "manifest_changed"])
def test_package_tamper_refuses(tmp_path, change):
    target = tmp_path / "package"
    shutil.copytree(BASE, target, ignore=shutil.ignore_patterns("__pycache__"))
    chosen = target / "bundle/core/schema/bootstrap.py"
    if change == "missing":
        chosen.unlink()
    elif change == "new":
        (target / "bundle/core/evil.py").write_text("raise Exception()")
    elif change == "changed":
        chosen.write_bytes(chosen.read_bytes() + b"\n# drift\n")
    elif change == "file_symlink":
        chosen.unlink(); chosen.symlink_to(BASE / "bundle/core/schema/bootstrap.py")
    elif change == "directory_symlink":
        shutil.rmtree(target / "bundle/core/migrations")
        (target / "bundle/core/migrations").symlink_to(BASE / "bundle/core/migrations", target_is_directory=True)
    else:
        (target / "manifest.json").write_text("{}")
    with pytest.raises(integrity.Preserved029Error):
        integrity.check_package(target)


def isolated(code, *, cwd=None, env=None):
    return subprocess.run([sys.executable, "-I", "-c", code], cwd=cwd, env=env, capture_output=True, text=True, timeout=30)


def runner_code(tail):
    return ("import importlib.util,sys;sys.dont_write_bytecode=True;s=importlib.util.spec_from_file_location('owned_runner',"
            + repr(str(BASE / "runner.py")) + ");r=importlib.util.module_from_spec(s);s.loader.exec_module(r);" + tail)


def test_isolated_import_ignores_live_checkout_pythonpath_and_cwd_shadow(tmp_path):
    (tmp_path / "core").mkdir(); (tmp_path / "core/__init__.py").write_text("raise RuntimeError('live shadow imported')")
    result = isolated(runner_code("b,m=r.prepare_sources();import core.schema.bootstrap as x;assert x.__file__==str(b/'core/schema/bootstrap.py');print('closed-origin')"),
                      cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout == "closed-origin\n"


def test_missing_dependency_cannot_use_live_checkout_fallback():
    result = isolated(runner_code("r.prepare_sources();import core.dependencies"), env={**os.environ, "PYTHONPATH": str(ROOT / "backend")})
    assert result.returncode != 0 and "dependency is not admitted" in result.stderr


def test_unisolated_runner_and_early_backend_import_refuse():
    result = subprocess.run([sys.executable, "-c", runner_code("r.prepare_sources()")], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "isolated interpreter required" in result.stderr
    result = isolated(runner_code("import types;sys.modules['core']=types.ModuleType('core');r.prepare_sources()"))
    assert result.returncode != 0 and "imported early" in result.stderr


def test_unknown_action_control_refuses_without_connecting(tmp_path):
    control = tmp_path / "control"; control.write_text(json.dumps({"action": "executeSQL"})); control.chmod(0o600)
    result = subprocess.run([sys.executable, "-I", str(BASE / "runner.py"), str(control)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 78 and result.stdout == ""  # EX_CONFIG: an environment refusal
    assert result.stderr.startswith("preserved029-refused:Preserved029Error:")


def _control(**overrides):
    data = {"action": "restore029", "archive": "/x.dump", "archive_sha256": "a" * 64, "target_url": "postgresql+psycopg://odin@pg/odin",
            "password_file": "/p", "maintenance_url": "postgresql+psycopg://odin@pg/postgres", "maintenance_password_file": "/m",
            "directory": "/d", "journal": "/j", "database": "odin_validate_" + "a" * 20, "oid": 1, "owner": 2,
            "stage_url": "postgresql+psycopg://odin@pg/odin_validate_" + "a" * 20, "deadline": monotonic() + 30}
    data.update(overrides)
    return data


@pytest.mark.parametrize("change", ["mode", "symlink", "duplicate", "oversize", "expired", "bool_oid", "extra", "foreign_name", "digest", "sql"])
def test_private_control_closed_shape_and_identity(tmp_path, change):
    control = tmp_path / "control"
    data = _control()
    if change == "expired": data["deadline"] = monotonic() - 1
    if change == "bool_oid": data["oid"] = True
    if change == "extra": data["sql"] = "SELECT 1"
    if change == "foreign_name": data["database"] = "postgres"
    if change == "digest": data["archive_sha256"] = "z" * 64
    if change == "sql": data["action"] = "build027"
    raw = json.dumps(data)
    if change == "duplicate": raw = '{"action":"restore029",' + raw[1:]
    if change == "oversize": raw = " " * 8193
    control.write_text(raw); control.chmod(0o644 if change == "mode" else 0o600)
    if change == "symlink":
        actual = tmp_path / "actual"; control.rename(actual); control.symlink_to(actual)
    result = isolated(runner_code("r.read_control(" + repr(str(control)) + ")"))
    assert result.returncode != 0


def test_valid_control_shape_is_admitted(tmp_path):
    control = tmp_path / "control"; control.write_text(json.dumps(_control())); control.chmod(0o600)
    result = isolated(runner_code("v=r.read_control(" + repr(str(control)) + ");print(v['action'])"))
    assert result.returncode == 0 and result.stdout == "restore029\n", result.stderr


@pytest.mark.parametrize("mode", ["success", "refusal", "stderr", "nonzero", "wrong_ack", "oversized", "timeout"])
def test_owned_child_supervision_closed_ack_output_deadline_and_reap(tmp_path, monkeypatch, mode):
    actual_popen = subprocess.Popen
    scripts = {"success": "print('preserved029-validated:26',flush=True)",
               "refusal": "import sys;print('preserved029-refused:BackupValidationError:Imported025 migration history differs from fixed packaged contract',file=sys.stderr);raise SystemExit(1)",
               "stderr": "import sys;print('preserved029-validated:27');print('uncertain',file=sys.stderr)",
               "nonzero": "raise SystemExit(2)",
               "wrong_ack": "print('preserved029-validated:30')",
               "oversized": "print('x'*20000,flush=True)",
               "timeout": "import time;time.sleep(5)"}
    child = tmp_path / "owned_child.py"; child.write_text(scripts[mode])
    process = []

    def launch(argv, **kw):
        assert argv[:2] == [sys.executable, "-I"] and argv[2] == str(BASE / "runner.py")
        assert kw["start_new_session"] is True and "PYTHONPATH" not in kw["env"]
        owned = actual_popen([sys.executable, "-I", str(child)], **kw); process.append(owned); return owned

    monkeypatch.setattr(p.subprocess, "Popen", launch)
    control = tmp_path / "control"; control.write_text("{}")
    if mode == "success":
        assert p._bounded_child(control, monotonic() + 5) == 26
    elif mode == "refusal":
        with pytest.raises(p.Preserved029Refused) as refused:
            p._bounded_child(control, monotonic() + 5)
        assert refused.value.kind == "BackupValidationError" and "migration history differs" in refused.value.detail
    else:
        with pytest.raises(p.Preserved029Error):
            p._bounded_child(control, monotonic() + (.3 if mode == "timeout" else 5))
    assert process[0].returncode is not None
    assert process[0].stdout.closed and process[0].stderr.closed


def test_stage_url_handed_to_the_child_never_carries_a_credential():
    from sqlalchemy.engine import make_url
    url = make_url("postgresql+psycopg://odin:secret-value@pg:5433/odin_validate_" + "a" * 20)
    clean = p.credentialless(url)
    assert clean.password is None and "secret-value" not in clean.render_as_string(hide_password=False)
    assert (clean.drivername, clean.username, clean.host, clean.port, clean.database) == (url.drivername, url.username, url.host, url.port, url.database)
    # Non-credential connection parameters are kept, as on the current restore path.
    kept = p.credentialless(make_url("postgresql+psycopg://odin@pg/odin_validate_" + "a" * 20 + "?sslmode=require&connect_timeout=10"))
    assert dict(kept.query) == {"sslmode": "require", "connect_timeout": "10"}
    for key in ("password", "sslpassword", "passfile", "service", "sslkey", "sslcert", "application_name", "PASSWORD", "Service"):
        with pytest.raises(p.Preserved029Error, match="credential"):
            p.credentialless(make_url("postgresql+psycopg://odin@pg/odin_validate_" + "a" * 20 + f"?sslmode=require&{key}=x"))


@pytest.mark.parametrize("key", ["target_url", "maintenance_url", "stage_url"])
def test_child_admits_non_credential_queries_and_refuses_credential_ones(tmp_path, key):
    good = _control(**{key: _control()[key] + "?sslmode=require"})
    bad = _control(**{key: _control()[key] + "?service=odin"})
    script = ("from sqlalchemy.engine import make_url;v=r.read_control({path!r});"
              "[r.admit_url(make_url(v[k]),k) for k in ('target_url','maintenance_url','stage_url')];print('admitted')")
    for data, admitted in ((good, True), (bad, False)):
        control = tmp_path / ("good" if admitted else "bad"); control.write_text(json.dumps(data)); control.chmod(0o600)
        result = isolated(runner_code(script.format(path=str(control))))
        assert (result.returncode == 0 and result.stdout == "admitted\n") is admitted, result.stderr


def test_forged_or_unowned_stage_refuses_before_child(tmp_path):
    from sqlalchemy import create_engine
    engine = create_engine("sqlite://")
    secret = tmp_path / "s"; secret.write_text("x"); secret.chmod(0o600)
    with pytest.raises(p.Preserved029Error):
        p.restore_into_stage(engine, "odin_validate_" + "a" * 20, archive=tmp_path / "a", archive_sha256="a" * 64, target_url="", password_file=secret,
                             maintenance_url="", maintenance_password_file=secret, directory=tmp_path, journal=tmp_path, drop=lambda names: [],
                             existing=lambda: set(),
                             deadline=monotonic() + 10)
    engine.dispose()


# --- Archive routing: current, preserved029 era, or refused. -------------------------------------------

def _ledger_sql(ids):
    rows = "\n".join(f"{i}\t{'0'*64}\tpostgresql\t2026-10-15 10:00:00.123456+00" for i in ids)
    return ("--\n-- PostgreSQL database dump\n--\nSET statement_timeout = 0;\n"
            "COPY public.odin_schema_migrations (migration_id, checksum, dialect, applied_at) FROM stdin;\n"
            + rows + ("\n" if rows else "") + "\\.\n\n")


FAKE_PG_RESTORE = """#!{python}
import os, sys
if "--list" in sys.argv:
    sys.stdout.write("1; 2615 2200 SCHEMA - public owner\\n2; 1259 1 TABLE public orders owner\\n")
    raise SystemExit(0)
with open(os.environ["FAKE_LEDGER_FILE"], "rb") as source:
    repeat = int(os.environ.get("FAKE_LEDGER_REPEAT", "1"))
    head, _, tail = source.read().partition(b"\\\\.\\n")
    out = sys.stdout.buffer
    out.write(head)
    if os.environ.get("FAKE_LEDGER_PIDFILE"):
        open(os.environ["FAKE_LEDGER_PIDFILE"], "w").write(str(os.getpid()))
    if os.environ.get("FAKE_LEDGER_HANG"):
        out.flush(); __import__("time").sleep(60)
    for i in range(repeat - 1):
        out.write(("python:9%07d-padding\\t" % i).encode() + b"0" * 64 + b"\\tpostgresql\\t2026-10-15 10:00:00+00\\n")
    out.write(b"\\\\.\\n" + tail)
"""


@pytest.fixture
def routed(tmp_path, monkeypatch):
    """Real subprocess plumbing with a fake pg_restore on PATH."""
    archive = tmp_path / "archive.dump"; archive.write_bytes(b"x" * 200)
    tools = tmp_path / "bin"; tools.mkdir()
    fake = tools / "pg_restore"; fake.write_text(FAKE_PG_RESTORE.format(python=sys.executable)); fake.chmod(0o755)
    secret = tmp_path / "secret"; secret.write_text("owned-test-only"); secret.chmod(0o600)
    ledger = tmp_path / "ledger.sql"
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("FAKE_LEDGER_FILE", str(ledger))

    class State(dict):
        def __setitem__(self, key, value):
            if key == "ledger":
                ledger.write_text(value)
            super().__setitem__(key, value)

    return archive, State()


def test_current_archive_takes_the_ordinary_path(routed):
    from modules.system.pre026_restore_stage import identify_contract, post027_migration_ids
    archive, state = routed
    state["ledger"] = _ledger_sql(sorted(p.ledger_ids()) + list(post027_migration_ids()))
    assert identify_contract(archive, "postgresql://o@pg/odin", None, archive.parent) is None


@pytest.mark.parametrize("era", ["025", "026", "027"])
def test_pre_post027_archives_route_to_preserved029(routed, era):
    from core.schema import pre026_restore_contract as C
    from modules.system.pre026_restore_stage import identify_contract, PRESERVED029
    archive, state = routed
    ids = {"025": [row[0] for row in C.EXPECTED_POSTGRES_LEDGER], "026": [row[0] for row in C.EXPECTED_POSTGRES_LEDGER026],
           "027": sorted(p.ledger_ids())}[era]
    state["ledger"] = _ledger_sql(ids)
    assert identify_contract(archive, "postgresql://o@pg/odin", None, archive.parent) is PRESERVED029


@pytest.mark.parametrize("case", ["unknown_id", "malformed_row", "no_copy", "duplicate_id", "unterminated"])
def test_unknown_or_malformed_ledger_refuses(routed, case):
    from modules.system.backup_service import BackupValidationError
    from modules.system.pre026_restore_stage import identify_contract
    archive, state = routed
    ids = sorted(p.ledger_ids())
    sql = _ledger_sql(ids + ["python:099-from-the-future"] if case == "unknown_id" else ids)
    if case == "malformed_row": sql = sql.replace("\tpostgresql\t", "\tpostgresql;DROP\t", 1)
    if case == "no_copy": sql = "SET x = 1;\n"
    if case == "duplicate_id": sql = _ledger_sql(ids + ids[:1])
    if case == "unterminated": sql = sql.replace("\\.\n", "")
    state["ledger"] = sql
    with pytest.raises(BackupValidationError):
        identify_contract(archive, "postgresql://o@pg/odin", None, archive.parent)


def test_ledger_probe_streams_with_a_byte_cap_and_stops_the_tool(routed, monkeypatch):
    from modules.system import pre026_restore_stage as stage
    from modules.system.backup_service import BackupValidationError
    archive, state = routed
    state["ledger"] = _ledger_sql(sorted(p.ledger_ids()))
    monkeypatch.setenv("FAKE_LEDGER_REPEAT", str(200000))  # ~25 MB of well-formed rows
    started = monotonic()
    with pytest.raises(BackupValidationError, match="exceeds"):
        stage.archive_ledger_ids(archive, "postgresql://o@pg/odin", None, archive.parent)
    assert monotonic() - started < 20


def test_ledger_probe_has_a_timeout(routed, monkeypatch):
    from modules.system import pre026_restore_stage as stage
    from modules.system.backup_service import BackupValidationError
    archive, state = routed
    state["ledger"] = "COPY public.odin_schema_migrations (migration_id, checksum, dialect, applied_at) FROM stdin;\n"
    monkeypatch.setenv("FAKE_LEDGER_HANG", "1")
    monkeypatch.setattr(stage, "LEDGER_SECONDS", 1)
    started = monotonic()
    with pytest.raises(BackupValidationError, match="timed out"):
        stage.archive_ledger_ids(archive, "postgresql://o@pg/odin", None, archive.parent)
    assert monotonic() - started < 10


def test_undecodable_ledger_is_a_validation_error(routed):
    from modules.system import pre026_restore_stage as stage
    from modules.system.backup_service import BackupValidationError
    archive, state = routed
    state["ledger"] = _ledger_sql(sorted(p.ledger_ids()))
    Path(os.environ["FAKE_LEDGER_FILE"]).write_bytes(Path(os.environ["FAKE_LEDGER_FILE"]).read_bytes().replace(b"python:001", b"python:\xff01", 1))
    with pytest.raises(BackupValidationError, match="malformed"):
        stage.archive_ledger_ids(archive, "postgresql://o@pg/odin", None, archive.parent)


def test_intermediate_post027_archive_refuses_loudly(routed, monkeypatch):
    from core.schema import bootstrap
    from modules.system.backup_service import BackupValidationError
    from modules.system.pre026_restore_stage import identify_contract
    archive, state = routed
    monkeypatch.setattr(bootstrap, "POST027_MIGRATIONS", bootstrap.POST027_MIGRATIONS + (("python:031-next", "core.schema.migrations.031_next"),))
    state["ledger"] = _ledger_sql(sorted(p.ledger_ids()) + [bootstrap.POST027_MIGRATIONS[0][0]])
    with pytest.raises(BackupValidationError, match="intermediate post-027 build"):
        identify_contract(archive, "postgresql://o@pg/odin", None, archive.parent)


# --- Post-027 registry. ----------------------------------------------------------------------------------

def test_post027_registry_is_ordered_allowlisted_and_self_identifying():
    from core.schema import bootstrap, migrator
    numbers = []
    for migration_id, module_name in bootstrap.POST027_MIGRATIONS:
        module = importlib.import_module(module_name)
        assert module.MIGRATION_ID == migration_id
        assert module_name in migrator._ALLOWED_PYTHON_MIGRATIONS
        assert migration_id not in p.ledger_ids()
        numbers.append(int(migration_id.split(":")[1][:3]))
    assert numbers == sorted(set(numbers)) and numbers[0] >= 30
    # Every allowlisted module is either preserved-era (ledgered by preserved029) or registered post-027.
    registered = {module for _, module in bootstrap.POST027_MIGRATIONS}
    for module_name in migrator._ALLOWED_PYTHON_MIGRATIONS - registered:
        assert importlib.import_module(module_name).MIGRATION_ID in p.ledger_ids(), module_name


def test_restore_wraps_migration_refusals_as_validation_errors(tmp_path, monkeypatch):
    from core.schema.migrator import MigrationError
    from modules.system import postgres_backup_service as b
    from modules.system import pre026_restore_stage as stage
    archive = tmp_path / "a.dump"; archive.write_bytes(b"x" * 200)
    monkeypatch.setattr(stage, "identify_contract", lambda *a: None)
    def refuse(*a, **k):
        raise MigrationError("Migration history mismatch: python:027-power-target-operations")
    monkeypatch.setattr(b, "_validate_current_postgres_restore", refuse)
    with pytest.raises(b.BackupValidationError, match="cannot be upgraded.*history mismatch"):
        b.validate_postgres_restore(archive, "postgresql://o@pg/odin", "postgresql://o@pg/postgres", base_dir=tmp_path)


# --- Killed children and stale artifacts. -----------------------------------------------------------------

def test_killed_child_drops_only_databases_it_created(tmp_path, monkeypatch):
    from modules.system import restore_artifacts
    backups = tmp_path / "backups"; backups.mkdir()
    journal = restore_artifacts.journal_dir(backups)
    work = backups / (".odin-preserved029-" + "b" * 20); (work / "backups").mkdir(parents=True)
    created = "odin_validate_" + "c" * 20         # CREATE DATABASE succeeded
    creating = "odin_validate_" + "e" * 20        # killed while CREATE DATABASE ran
    preexisting = "odin_validate_" + "f" * 20     # intent for a name that already existed
    concurrent = "odin_validate_" + "a" * 20      # another operation, journaled meanwhile
    stage = "odin_validate_" + "d" * 20
    restore_artifacts.record(backups, stage)
    child_journal = journal / work.name
    child = tmp_path / "child.py"
    child.write_text("import os,time\n"
                     f"open(os.path.join({str(child_journal)!r},{created!r}),'w').close()\n"
                     f"open(os.path.join({str(child_journal)!r},{creating + '.intent'!r}),'w').close()\n"
                     f"open(os.path.join({str(child_journal)!r},{preexisting + '.intent'!r}),'w').close()\n"
                     f"open(os.path.join({str(journal)!r},{concurrent!r}),'w').close()\n"
                     f"open(os.path.join({str(work / 'backups')!r},'.odin-canonical-schema-x.dump'),'w').write('x')\n"
                     "time.sleep(30)\n")
    actual_popen = subprocess.Popen
    monkeypatch.setattr(p.subprocess, "Popen", lambda argv, **kw: actual_popen([sys.executable, "-I", str(child)], **kw))
    dropped = []
    with pytest.raises(p.Preserved029Error, match="deadline"):
        p._supervised({"action": "restore029"}, monotonic() + 2, journal=journal, work=work, keep={stage},
                      drop=lambda names: dropped.extend(sorted(names)) or sorted(names),
                      existing=lambda: {preexisting, stage, concurrent})
    assert dropped == sorted([created, creating])
    assert not work.exists() and not child_journal.exists()
    # The caller's own stage and the concurrent operation's database stay journaled.
    assert set(restore_artifacts.journal_entries(backups)) == {stage, concurrent}


def test_startup_sweep_drops_only_stale_journaled_databases_and_files(tmp_path):
    from modules.system import restore_artifacts
    backups = tmp_path / "backups"; backups.mkdir()
    old, fresh = "odin_validate_" + "1" * 20, "odin_validate_" + "2" * 20
    restore_artifacts.record(backups, old); restore_artifacts.record(backups, fresh)
    now = __import__("time").time()
    os.utime(restore_artifacts.journal_dir(backups) / old, (now - 7 * 3600, now - 7 * 3600))
    stale_dump = backups / ".odin-upgraded029-abc.dump"; stale_dump.write_text("x")
    stale_dir = backups / (".odin-preserved029-" + "e" * 20); stale_dir.mkdir()
    keep_dump = backups / ".odin-canonical-schema-new.dump"; keep_dump.write_text("x")
    real_backup = backups / "odin_backup_20261015_000000_000000.dump"; real_backup.write_text("x")
    for path in (stale_dump, stale_dir, real_backup):
        os.utime(path, (now - 7 * 3600, now - 7 * 3600))
    asked = []
    result = restore_artifacts.sweep_stale(backups, "unused", None, drop=lambda names: asked.extend(names) or list(names))
    assert asked == [old] and result["databases"] == [old]
    assert set(restore_artifacts.journal_entries(backups)) == {fresh}
    assert not stale_dump.exists() and not stale_dir.exists() and keep_dump.exists() and real_backup.exists()


@pytest.mark.parametrize("code,category", [(65, "archive"), (75, "retry"), (78, "environment"), (70, "environment"), (3, "environment")])
def test_child_failure_category_comes_from_its_exit_code(tmp_path, monkeypatch, code, category):
    actual_popen = subprocess.Popen
    child = tmp_path / "owned_child.py"
    child.write_text("import sys;print('preserved029-refused:BackupValidationError:PostgreSQL client operation failed: pg_dump',file=sys.stderr);"
                     f"raise SystemExit({code})")
    monkeypatch.setattr(p.subprocess, "Popen", lambda argv, **kw: actual_popen([sys.executable, "-I", str(child)], **kw))
    control = tmp_path / "control"; control.write_text("{}")
    with pytest.raises(p.Preserved029Refused) as refused:
        p._bounded_child(control, monotonic() + 5)
    assert refused.value.category == category


@pytest.mark.parametrize("make,code", [
    ("BackupValidationError('Archive schema differs')", 65),
    ("MigrationError('Migration history mismatch: x')", 65),
    ("Preserved029Error('Preserved029 stage tables differ')", 65),
    ("_client_failure(['pg_dump','--dbname','postgresql://x'])", 75),
    ("_client_failure(['pg_restore','--list','/a.dump'])", 65),
    ("_client_timeout(['pg_restore','--list','/a.dump'])", 75),
    ("_password_failure()", 78),
    ("Preserved029Error('Preserved029 credential is unsafe')", 78),
    ("RuntimeError('unexpected')", 70),
])
def test_child_classifies_its_own_failures_by_type(make, code):
    script = runner_code(
        "import subprocess\n"
        "class BackupValidationError(RuntimeError):pass\n"
        "class MigrationError(RuntimeError):pass\n"
        "Preserved029Error=r.Preserved029Error\n"
        "def _client_failure(cmd):\n"
        "  try:raise subprocess.CalledProcessError(1,cmd)\n"
        "  except subprocess.CalledProcessError as e:\n"
        "    try:raise BackupValidationError('PostgreSQL client operation failed: '+cmd[0]) from e\n"
        "    except BackupValidationError as x:return x\n"
        "def _client_timeout(cmd):\n"
        "  try:raise subprocess.TimeoutExpired(cmd,300)\n"
        "  except subprocess.TimeoutExpired as e:\n"
        "    try:raise BackupValidationError('PostgreSQL client operation failed: '+cmd[0]) from e\n"
        "    except BackupValidationError as x:return x\n"
        "def _password_failure():\n"
        "  try:raise OSError('denied')\n"
        "  except OSError as e:\n"
        "    try:raise BackupValidationError('PostgreSQL password file is not readable') from e\n"
        "    except BackupValidationError as x:return x\n"
        f"print(r.exit_code_for({make}))")
    result = isolated(script)
    assert result.returncode == 0 and result.stdout.strip() == str(code), result.stderr


def test_supervised_cleans_up_when_listing_existing_databases_fails(tmp_path):
    from modules.system import restore_artifacts
    backups = tmp_path / "backups"; backups.mkdir()
    journal = restore_artifacts.journal_dir(backups)
    work = backups / (".odin-preserved029-" + "9" * 20); (work / "backups").mkdir(parents=True)
    def broken():
        raise ConnectionError("maintenance database unreachable")
    with pytest.raises(ConnectionError):
        p._supervised({"action": "restore029"}, monotonic() + 5, journal=journal, work=work, keep=set(), drop=lambda names: [], existing=broken)
    assert not work.exists() and not (journal / work.name).exists()


def test_sweep_never_drops_intent_only_names_and_removes_empty_child_journals(tmp_path):
    from modules.system import restore_artifacts
    backups = tmp_path / "backups"; backups.mkdir()
    journal = restore_artifacts.journal_dir(backups)
    old = __import__("time").time() - 7 * 3600
    child = journal / (".odin-preserved029-" + "8" * 20); child.mkdir(mode=0o700)
    confirmed, intended = "odin_validate_" + "3" * 20, "odin_validate_" + "4" * 20
    (child / confirmed).write_text(""); (child / (intended + ".intent")).write_text("")
    empty = journal / (".odin-preserved029-" + "7" * 20); empty.mkdir(mode=0o700)
    for path in (child / confirmed, child / (intended + ".intent"), empty, child):
        os.utime(path, (old, old))
    asked = []
    result = restore_artifacts.sweep_stale(backups, "unused", None, drop=lambda names: asked.extend(sorted(names)) or sorted(names))
    assert asked == [confirmed] and result["databases"] == [confirmed]
    assert not empty.exists() and not child.exists()  # emptied: the unprovable intent entry is forgotten, not dropped


def test_an_early_ledger_refusal_stops_pg_restore_at_once(routed, monkeypatch, tmp_path):
    from modules.system import pre026_restore_stage as stage
    from modules.system.backup_service import BackupValidationError
    archive, state = routed
    state["ledger"] = "COPY public.odin_schema_migrations (migration_id, checksum, dialect, applied_at) FROM stdin;\nnot a ledger row\n"
    pidfile = tmp_path / "pg_restore.pid"
    monkeypatch.setenv("FAKE_LEDGER_PIDFILE", str(pidfile)); monkeypatch.setenv("FAKE_LEDGER_HANG", "1")
    try:
        stage.archive_ledger_ids(archive, "postgresql://o@pg/odin", None, archive.parent)
    except BackupValidationError as error:
        held = error  # keep the traceback (and its frames) alive, as a caller would
    else:
        raise AssertionError("malformed ledger admitted")
    pid = int(pidfile.read_text())
    import time
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("pg_restore kept running after the ledger was refused")
    assert held
