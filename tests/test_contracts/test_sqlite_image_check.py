"""The check that keeps a SQLite without the WAL-reset fix out of the image."""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("odin_sqlite_image_check", ROOT / "docker/sqlite_image_check.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


@pytest.mark.parametrize("version,fixed", [
    ((3, 46, 1), False),   # Debian's, what production ran during the incident
    ((3, 44, 5), False), ((3, 44, 6), True), ((3, 44, 7), True),
    ((3, 45, 3), False),
    ((3, 50, 6), False), ((3, 50, 7), True),
    ((3, 51, 0), False), ((3, 51, 2), False), ((3, 51, 3), True), ((3, 51, 4), True),
    ((3, 52, 0), False),   # withdrawn
    ((3, 53, 0), True), ((3, 54, 1), True), ((4, 0, 0), True),
])
def test_which_versions_carry_the_fix(version, fixed):
    assert check.has_wal_reset_fix(version) is fixed


def _good():
    return (3, 51, 3), list(check.EXPECTED_OPTIONS) + ["COMPILER=gcc-14.2.0"], [check.EXPECTED_LIBRARY] * 5


def test_the_pinned_build_passes():
    assert check.problems(*_good()) == []


def test_an_unfixed_version_fails():
    _, options, libraries = _good()
    found = check.problems((3, 46, 1), options, libraries)
    assert len(found) == 1 and "WAL-reset" in found[0]


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_any_change_in_compile_options_fails(change):
    version, options, libraries = _good()
    options = [o for o in options if o != "SECURE_DELETE"] if change == "missing" else options + ["DQS=0"]
    found = check.problems(version, options, libraries)
    assert len(found) == 1 and "compile options" in found[0]
    assert ("SECURE_DELETE" if change == "missing" else "DQS=0") in found[0]


def test_a_build_with_fts5_is_refused():
    """3.51.3 predates the fixes for CVE-2026-11822 and CVE-2026-11824, and ODIN doesn't use FTS5."""
    version, options, libraries = _good()
    assert "ENABLE_FTS5" not in options
    found = check.problems(version, options + ["ENABLE_FTS5"], libraries)
    assert len(found) == 1 and "ENABLE_FTS5" in found[0]
    assert "--fts5" not in DOCKERFILE.split("AS sqlite-build")[1].split("AS frontend-build")[0]


def test_nothing_in_the_backend_or_the_schema_uses_fts5():
    for path in (ROOT / "backend").rglob("*.py"):
        if "preserved" not in str(path):
            assert "fts5" not in path.read_text(encoding="utf-8").lower(), path


@pytest.mark.parametrize("paths", [
    ["/usr/lib/aarch64-linux-gnu/libsqlite3.so.0.8.6"],   # what arm64 loaded before the ld.so.conf.d file
    ["/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6"],
    [],
    [check.EXPECTED_LIBRARY, "/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6"],
])
def test_loading_any_other_library_fails(paths):
    version, options, _ = _good()
    found = check.problems(version, options, paths)
    assert len(found) == 1 and "loaded" in found[0]


def test_main_reports_failure_and_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(check, "_loaded_libraries", lambda: ["/somewhere/else.so"])
    assert check.main() == 1
    assert "SQLite image check failed" in capsys.readouterr().err


def test_script_runs_with_no_site_packages_as_the_build_does():
    result = subprocess.run([sys.executable, "-I", "-S", "-c",
                             "import runpy; m = runpy.run_path(%r); print(m['has_wal_reset_fix']((3, 51, 3)))"
                             % str(ROOT / "docker/sqlite_image_check.py")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and result.stdout.strip() == "True"


def test_dockerfile_pins_the_release_and_runs_the_check_in_the_final_image():
    assert "https://sqlite.org/2026/sqlite-autoconf-3510300.tar.gz" in DOCKERFILE
    assert re.search(r'echo "[0-9a-f]{64}  /tmp/sqlite\.tar\.gz" \| sha256sum -c -', DOCKERFILE)
    assert "32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e" in DOCKERFILE
    final = DOCKERFILE.split("FROM backend-base")[1]
    assert f"/build/sqlite/libsqlite3.so {check.EXPECTED_LIBRARY}" in final
    assert "/etc/ld.so.conf.d/000-odin-sqlite.conf" in final and "ldconfig" in final
    assert "python3 -I -S /usr/local/libexec/odin-sqlite-image-check.py" in final
    assert final.index("odin-sqlite-image-check.py\nRUN") < final.index("pip install")


def test_debian_flags_stay_in_the_build():
    build = DOCKERFILE.split("AS sqlite-build")[1].split("AS frontend-build")[0]
    for flag in ("-D_HAVE_SQLITE_CONFIG_H", "-DSQLITE_SECURE_DELETE", "-DSQLITE_ENABLE_LOAD_EXTENSION",
                 "-DSQLITE_MAX_VARIABLE_NUMBER=250000", "-DSQLITE_MAX_DEFAULT_PAGE_SIZE=32768", "--soname=legacy"):
        assert flag in build


def test_candidate_gate_runs_the_check_inside_the_running_container():
    runner = (ROOT / "ops/release_gate/runner.py").read_text(encoding="utf-8")
    assert '"python3", "-I", "-S", "/usr/local/libexec/odin-sqlite-image-check.py"' in runner
    assert runner.index("odin-sqlite-image-check.py") < runner.index('"setup-and-seed"')
