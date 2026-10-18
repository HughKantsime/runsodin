"""Cert group H — install.sh / update.sh / docker compose, executed end to end.

Claims: F22-22.1-03 (docker compose up), F22-22.4-01 (install.sh preflight, config,
pull, health wait), F22-22.4-02 (update.sh self-update, version diffing, --force,
rollback instructions), F22-22.4-03 (non-TTY mode), F22-22.4-04 (no eval, .env 0600,
SIGINT traps).

Real scripts, real Docker. The scripts run in their own ODIN_INSTALL_TEST_MODE with
owned project/container/volume/network names and ports 27431-27449. The only stubbed
boundary is the docker CLI in the preflight-failure / interrupt / health-timeout
cases (a missing or stopped Docker cannot be produced otherwise without touching the
host). Registry access is a read-only `docker pull` of the published image.

Env: CERT_H_IMAGE (locally built candidate), CERT_H_PUBLISHED_IMAGE (published image
reference to pull, default ghcr.io/hughkantsime/odin:v1.9.24), CERT_H_WORK.
"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import termios
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install/install.sh"
UPDATE_SH = ROOT / "install/update.sh"
COMPOSE = ROOT / "install/docker-compose.yml"
COMPOSE_TEST = ROOT / "install/docker-compose.test.yml"
IMAGE = os.environ.get("CERT_H_IMAGE", "")
PUBLISHED = os.environ.get(
    "CERT_H_PUBLISHED_IMAGE", "ghcr.io/hughkantsime/odin:v1.9.24",
)
WORK = Path(os.environ.get("CERT_H_WORK", ROOT / "artifacts/application-certification/cert-H/work"))
LOGS = WORK.parent / "installer-logs"
DOCKER = shutil.which("docker")
VERSION = (ROOT / "VERSION").read_text().strip()
ANSI = re.compile(r"\x1b\[")
BASE_PATH = f"{Path(DOCKER).parent if DOCKER else '/usr/local/bin'}:/usr/bin:/bin:/usr/sbin:/sbin"

# live_environment: needs a built image, Docker, network or an external checkout; the contract
# gate deselects it (tests/test_contracts/live_environment_inventory.txt) and the certification
# batches run it with the environment set.
pytestmark = [pytest.mark.live_environment, pytest.mark.skipif(not IMAGE or DOCKER is None, reason="CERT_H_IMAGE and docker required")]

RUN = "certh-" + uuid.uuid4().hex[:8]
STATE: dict = {}


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _log(name: str, text: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / f"{name}.log").write_text(text)


def _test_env(root: Path, ports=(27431, 27432, 27433), **extra) -> dict:
    project = root.name
    env = {
        "PATH": BASE_PATH, "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8",
        "ODIN_INSTALL_TEST_MODE": "1", "ODIN_TEST_ROOT": str(root), "ODIN_TEST_RUN_ID": project,
        "ODIN_COMPOSE_PROJECT": project, "ODIN_TEST_CONTAINER_NAME": f"{project}-odin",
        "ODIN_TEST_NETWORK_NAME": f"{project}-net", "ODIN_TEST_VOLUME_NAME": f"{project}-data",
        "ODIN_TEST_DATA_PATH": str(root / "data"), "ODIN_TEST_HTTP_PORT": str(ports[0]),
        "ODIN_TEST_GO2RTC_PORT": str(ports[1]), "ODIN_TEST_WEBRTC_PORT": str(ports[2]),
        "ODIN_TEST_COMPOSE_SOURCE": str(COMPOSE_TEST), "ODIN_COMPOSE_SOURCE": str(COMPOSE),
        "ODIN_UPDATE_SOURCE": str(UPDATE_SH), "ODIN_INSTALL_DIR": str(root / project),
        "ODIN_SKIP_PUBLIC_CHECK": "1",
    }
    env.update(extra)
    return env


def _run(script: Path | str, env: dict, *args: str, cwd: Path | None = None, timeout: int = 400) -> subprocess.CompletedProcess:
    # stdin </dev/null and captured stdout/stderr == the curl|bash / CI situation (non-TTY)
    return subprocess.run(["bash", str(script), *args], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)


def _stub_docker(bin_dir: Path, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "docker"
    stub.write_text("#!/bin/bash\n" + body)
    stub.chmod(0o755)


def _container_image(name: str) -> str:
    return subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", name], capture_output=True, text=True).stdout.strip()


def _container_id(name: str) -> str:
    return subprocess.run(["docker", "inspect", "-f", "{{.Id}}", name], capture_output=True, text=True).stdout.strip()


def _ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=5) as r:
            return json.loads(r.read()).get("ready") is True
    except Exception:
        return False


def _cleanup_project(root: Path) -> None:
    project = root.name
    subprocess.run(["docker", "rm", "-f", f"{project}-odin"], capture_output=True)
    ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
                         capture_output=True, text=True).stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
    for kind, name in (("volume", f"{project}-data"), ("network", f"{project}-net"), ("network", f"{project}_default")):
        subprocess.run(["docker", kind, "rm", name], capture_output=True)
    if root.exists():
        subprocess.run(["docker", "run", "--rm", "-v", f"{root}:/w", "--entrypoint", "/bin/sh", IMAGE, "-c",
                        "rm -rf /w/* /w/.[!.]* 2>/dev/null; true"], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module", autouse=True)
def work_root():
    for port in range(27431, 27450):
        assert _free(port), f"port {port} is busy"
    WORK.mkdir(parents=True, exist_ok=True)
    roots = []
    STATE["roots"] = roots
    yield
    for root in roots:
        _cleanup_project(root)


def _new_root(tag: str) -> Path:
    root = WORK / f"{RUN}-{tag}"
    root.mkdir(parents=True)
    STATE["roots"].append(root)
    return root


# ── F22-22.4-01 preflight failures (docker CLI boundary stubbed) ─────────────

def test_preflight_refuses_when_docker_is_missing():
    root = _new_root("nodocker")
    env = _test_env(root, PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    result = _run(INSTALL_SH, env)
    _log("preflight-nodocker", result.stdout + result.stderr)
    assert result.returncode == 1
    assert "Docker not found" in result.stderr
    assert not (root / root.name).exists(), "installer must stop before creating the install dir"


def test_preflight_refuses_when_docker_daemon_is_down():
    root = _new_root("nodaemon")
    stub = root / "bin"
    _stub_docker(stub, 'case "$1" in version) exit 1;; *) exit 0;; esac\n')
    result = _run(INSTALL_SH, _test_env(root, PATH=f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin"))
    _log("preflight-nodaemon", result.stdout + result.stderr)
    assert result.returncode == 1 and "Docker daemon not running" in result.stderr
    assert not (root / root.name).exists()


def test_preflight_refuses_when_compose_is_missing():
    root = _new_root("nocompose")
    stub = root / "bin"
    _stub_docker(stub, 'if [ "$1" = version ]; then echo 29.0.0; exit 0; fi\nexit 1\n')
    result = _run(INSTALL_SH, _test_env(root, PATH=f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin"))
    _log("preflight-nocompose", result.stdout + result.stderr)
    assert result.returncode == 1 and "Docker Compose not found" in result.stderr
    assert not (root / root.name).exists()


def test_preflight_refuses_occupied_port_with_real_docker():
    root = _new_root("busyport")
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 27432))
        sock.listen(1)
        result = _run(INSTALL_SH, _test_env(root))
    _log("preflight-busyport", result.stdout + result.stderr)
    assert result.returncode == 1
    assert "Port 27432 in use" in result.stderr and "Required ports are in use" in result.stderr
    assert "Docker " in result.stdout and "Docker Compose" in result.stdout  # docker/compose checks passed first
    assert not (root / root.name).exists()


# ── F22-22.4-01 / 22.4-03 / 22.4-04 full non-TTY install with registry pull ──

def _env_values(env_file: Path) -> dict:
    return dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line and not line.startswith("#"))


def test_non_tty_install_of_candidate_image_starts_and_waits_for_readiness():
    root = _new_root("main")
    STATE["main"] = root
    env = _test_env(root, ODIN_IMAGE=IMAGE, ODIN_SKIP_IMAGE_PULL="1")
    result = _run(INSTALL_SH, env)
    out = result.stdout + result.stderr
    _log("install-non-tty", out)
    assert result.returncode == 0, out[-3000:]
    for marker in ("[1/9] Preflight checks", "✓ Docker ", "✓ Docker Compose", "✓ curl available",
                   "Ports 27431, 27432, 27433 free", "[2/9] Configuration", "[5/9] Generating environment",
                   "✓ Container started", "✓ O.D.I.N. is healthy", "✓ API responding on port 27431", "O.D.I.N. is ready!"):
        assert marker in out, marker
    # non-TTY: no colour codes, no prompt text, defaults used without blocking
    assert not ANSI.search(out)
    assert "▸ Host IP" not in out and "[y/N]" not in out
    install_dir = root / root.name
    env_file = install_dir / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    values = _env_values(env_file)
    assert values["ODIN_IMAGE"] == IMAGE and values["ODIN_HTTP_PORT"] == "27431"
    assert values["TZ"] and values["ODIN_HOST_IP"]
    assert f"Host IP: {values['ODIN_HOST_IP']}" in out and f"Timezone: {values['TZ']}" in out
    assert (install_dir / "update.sh").stat().st_mode & stat.S_IXUSR
    assert _container_image(f"{root.name}-odin") == IMAGE
    assert _ready(27431)
    STATE["main_env"] = env
    STATE["current_version"] = subprocess.run(["docker", "exec", f"{root.name}-odin", "cat", "/app/VERSION"],
                                              capture_output=True, text=True).stdout.strip()
    assert STATE["current_version"] == VERSION


def test_non_tty_reinstall_over_existing_install_does_not_prompt():
    root = STATE["main"]
    # The install is still running and holds its ports: the preflight recognises its own
    # container (same compose project and install directory) instead of refusing.
    assert _ready(27431) and not _free(27431)
    result = _run(INSTALL_SH, STATE["main_env"])
    out = result.stdout + result.stderr
    _log("reinstall-non-tty", out)
    assert result.returncode == 0, out[-2000:]
    assert "Port 27431 is held by this install's running O.D.I.N. container" in out
    assert "Ports 27431, 27432, 27433 free" not in out
    assert ("Ports 27431, 27432, 27433 usable (27431, 27432, 27433 held by this install's "
            f"{root.name}-odin container, which will be recreated)") in out
    assert "Existing install detected in non-interactive mode. Re-installing." in out
    assert _ready(27431)


def test_reinstall_still_refuses_ports_held_by_another_container():
    root = STATE["main"]
    # same install dir and ports, but the container holding them is not this project's
    env = {**STATE["main_env"], "ODIN_COMPOSE_PROJECT": f"{root.name}-foreign"}
    result = _run(INSTALL_SH, env)
    out = result.stdout + result.stderr
    _log("reinstall-foreign-project", out)
    assert result.returncode == 1, out[-2000:]
    assert "Port 27431 in use" in out and "Required ports are in use" in out
    assert "held by this install's running" not in out
    assert _ready(27431) and _container_image(f"{root.name}-odin")


def test_reinstall_refuses_ports_of_the_same_project_started_from_another_directory():
    root = STATE["main"]
    # same compose project and container name, but this install directory is not the one it runs from
    other = root / "other-install"
    other.mkdir(exist_ok=True)
    shutil.copy(COMPOSE, other / "docker-compose.yml")
    env = {**STATE["main_env"], "ODIN_INSTALL_DIR": str(other)}
    result = _run(INSTALL_SH, env)
    out = result.stdout + result.stderr
    _log("reinstall-other-workdir", out)
    assert result.returncode == 1, out[-2000:]
    assert "Port 27431 in use" in out and "Required ports are in use" in out
    assert "held by this install's running" not in out
    assert sorted(p.name for p in other.iterdir()) == ["docker-compose.yml"]  # refused before writing anything
    assert _ready(27431)


def test_non_tty_install_pulls_published_image_from_registry():
    root = _new_root("pub")
    STATE["pub"] = root
    env = _test_env(root, ports=(27434, 27435, 27436), ODIN_IMAGE=PUBLISHED)
    result = _run(INSTALL_SH, env)
    out = result.stdout + result.stderr
    _log("install-registry-pull", out)
    assert result.returncode == 0, out[-3000:]
    assert f"Pulling Docker image" in out and f"✓ Pulled {PUBLISHED}" in out
    assert "ODIN_SKIP_IMAGE_PULL" not in env
    assert _container_image(f"{root.name}-odin") == PUBLISHED and _ready(27434)
    STATE["pub_env"] = env


# ── F22-22.4-02 update.sh ────────────────────────────────────────────────────

def _repo(root: Path, version: str) -> str:
    repo = root / f"repo-{version}"
    repo.mkdir(exist_ok=True)
    (repo / "VERSION").write_text(version + "\n")
    return f"file://{repo}"


def _update_env(base: str = "main_env", **extra) -> dict:
    env = dict(STATE[base])
    env.pop("ODIN_IMAGE", None)  # the updater reads ODIN_IMAGE from the install's .env
    env.update(extra)
    return env


def test_update_reports_already_current_without_touching_the_container():
    root = STATE["main"]
    install_dir = root / root.name
    before = _container_id(f"{root.name}-odin")
    result = _run(install_dir / "update.sh", _update_env(ODIN_REPO=_repo(root, STATE["current_version"])), cwd=install_dir)
    out = result.stdout + result.stderr
    _log("update-already-current", out)
    assert result.returncode == 0, out
    assert f"Current: {STATE['current_version']}" in out
    assert f"Already up to date! ({STATE['current_version']})" in out and "Use --force to pull anyway." in out
    assert _container_id(f"{root.name}-odin") == before


def test_update_self_updates_then_reexecs():
    root = STATE["main"]
    install_dir = root / root.name
    newer = root / "update-newer.sh"
    newer.write_text(UPDATE_SH.read_text() + "\n# cert-H self-update marker\n")
    result = _run(install_dir / "update.sh",
                  _update_env(ODIN_REPO=_repo(root, STATE["current_version"]), ODIN_UPDATE_SOURCE=str(newer)), cwd=install_dir)
    out = result.stdout + result.stderr
    _log("update-self-update", out)
    assert result.returncode == 0, out
    assert "Updater has a new version, restarting..." in out
    assert (install_dir / "update.sh").read_text() == newer.read_text()
    assert out.count("Updater — Orchestrated") == 2  # banner printed by the old and the re-exec'd updater
    assert "Already up to date!" in out


def test_update_diffs_versions_and_never_reports_an_update_that_did_not_happen():
    root = STATE["main"]
    install_dir = root / root.name
    before = _container_id(f"{root.name}-odin")
    result = _run(install_dir / "update.sh", _update_env(ODIN_REPO=_repo(root, "9.9.9")), cwd=install_dir)
    out = result.stdout + result.stderr
    _log("update-version-diff", out)
    cur = STATE["current_version"]
    assert f"Latest:  9.9.9" in out and f"Update available: {cur} → 9.9.9" in out
    assert "✓ Container restarted" in out and "✓ O.D.I.N. is healthy" in out
    assert _container_id(f"{root.name}-odin") == before  # same image -> compose keeps the container
    assert result.returncode == 3, out
    assert f"The running version is still {cur}; 9.9.9 was announced" in out
    assert "O.D.I.N. updated!" not in out


def test_update_force_pulls_and_restarts_even_when_current():
    root = STATE["main"]
    install_dir = root / root.name
    before = _container_id(f"{root.name}-odin")
    result = _run(install_dir / "update.sh",
                  _update_env(ODIN_REPO=_repo(root, STATE["current_version"])), "--force", cwd=install_dir)
    out = result.stdout + result.stderr
    _log("update-force", out)
    assert result.returncode == 0, out
    assert f"Force pulling ({STATE['current_version']})" in out and "Pulled (" in out
    assert "✓ Container restarted" in out and "✓ O.D.I.N. is healthy" in out and "O.D.I.N. updated!" in out
    assert _ready(27431)


def test_update_force_pulls_published_image_from_registry():
    root = STATE["pub"]
    install_dir = root / root.name
    result = _run(install_dir / "update.sh",
                  _update_env("pub_env", ODIN_REPO=_repo(root, STATE["current_version"])), "--force", cwd=install_dir)
    out = result.stdout + result.stderr
    _log("update-force-registry", out)
    assert result.returncode == 0, out
    assert f"Pulling new image" in out and "Pulled (" in out and "Using local candidate image" not in out
    assert _ready(27434)


def test_update_failure_prints_rollback_that_actually_restores_previous_version():
    root = STATE["pub"]
    install_dir = root / root.name
    result = _run(install_dir / "update.sh",
                  _update_env("pub_env", ODIN_REPO=_repo(root, STATE["current_version"]),
                              ODIN_READINESS_URL="http://127.0.0.1:27449/health/ready"),
                  "--force", cwd=install_dir, timeout=200)
    out = result.stdout + result.stderr
    _log("update-rollback-hint", out)
    assert result.returncode == 1, out
    assert "Container did not become healthy within 60s" in out and "Update failed." in out
    hint = re.search(r"Rollback:\s+(ODIN_IMAGE=(\S+) docker compose up -d)", out)
    assert hint, out
    pinned = hint.group(2)
    assert pinned == f"ghcr.io/hughkantsime/odin:v{STATE['current_version']}"
    # Execute the printed rollback exactly as a user would, from the install directory.
    env = {"PATH": BASE_PATH, "HOME": os.environ.get("HOME", "/tmp"), "ODIN_IMAGE": pinned}
    up = subprocess.run(["docker", "compose", "up", "-d"], cwd=install_dir, env=env, capture_output=True, text=True, timeout=600)
    _log("update-rollback-executed", up.stdout + up.stderr)
    assert up.returncode == 0, up.stderr[-2000:]
    assert _container_image(f"{root.name}-odin") == pinned
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and not _ready(27434):
        time.sleep(2)
    assert _ready(27434)
    version = subprocess.run(["docker", "exec", f"{root.name}-odin", "cat", "/app/VERSION"], capture_output=True, text=True).stdout.strip()
    assert version == STATE["current_version"]


def _assert_self_updated(install_dir: Path, newer: Path, out: str) -> None:
    assert "Updater has a new version, restarting..." in out, out[-1500:]
    assert out.count("Updater — Orchestrated") == 2, out[-1500:]  # the old updater, then the re-exec'd one
    assert "Already up to date!" in out, out[-1500:]
    installed = install_dir / "update.sh"
    assert installed.read_text() == newer.read_text()
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    assert not list(install_dir.glob(".update.sh.*"))  # the atomic staging file was renamed, not left behind


def test_update_works_when_invoked_by_relative_path_from_parent_directory():
    root = STATE["main"]
    install_dir = root / root.name
    # a differing update source, given relative to the caller's directory, so the run copies and re-execs
    newer = root / "update-relative-newer.sh"
    newer.write_text(UPDATE_SH.read_text() + "\n# cert-H relative-path self-update marker\n")
    assert newer.read_text() != (install_dir / "update.sh").read_text()
    result = _run(f"{root.name}/update.sh",
                  _update_env(ODIN_REPO=_repo(root, STATE["current_version"]), ODIN_UPDATE_SOURCE=newer.name), cwd=root)
    out = result.stdout + result.stderr
    _log("update-relative-path", out)
    assert result.returncode == 0, out[-1500:]
    _assert_self_updated(install_dir, newer, out)


def test_update_self_update_is_not_misled_by_an_exported_cdpath():
    root = STATE["main"]
    install_dir = root / root.name
    # CDPATH holds a decoy directory with the same name; `cd odin-dir` would resolve to it and print its path
    decoy = root / "cdpath" / root.name
    decoy.mkdir(parents=True, exist_ok=True)
    newer = root / "update-cdpath-newer.sh"
    newer.write_text(UPDATE_SH.read_text() + "\n# cert-H CDPATH self-update marker\n")
    env = _update_env(ODIN_REPO=_repo(root, STATE["current_version"]), ODIN_UPDATE_SOURCE=str(newer),
                      CDPATH=str(root / "cdpath"))
    result = _run(f"{root.name}/update.sh", env, cwd=root)
    out = result.stdout + result.stderr
    _log("update-cdpath", out)
    assert result.returncode == 0, out[-1500:]
    _assert_self_updated(install_dir, newer, out)
    assert list(decoy.iterdir()) == []


# ── F22-22.4-04 SIGINT traps ─────────────────────────────────────────────────

def _interrupt(cmd: list[str], env: dict, cwd: Path, wait_for: str, timeout: float = 120) -> tuple[int, str]:
    proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    buf = b""
    deadline = time.monotonic() + timeout
    os.set_blocking(proc.stdout.fileno(), False)
    while time.monotonic() < deadline and wait_for.encode() not in buf:
        ready, _, _ = select.select([proc.stdout], [], [], 0.5)
        if ready:
            buf += proc.stdout.read() or b""
    assert wait_for.encode() in buf, buf.decode(errors="replace")
    time.sleep(1)
    os.killpg(proc.pid, signal.SIGINT)
    rest, _ = proc.communicate(timeout=60)
    return proc.returncode, (buf + (rest or b"")).decode(errors="replace")


def test_install_sigint_aborts_cleanly_before_any_change():
    root = _new_root("sigint-install")
    stub = root / "bin"
    _stub_docker(stub, 'if [ "$1" = version ]; then sleep 60; echo 29.0.0; fi\nexit 0\n')
    env = _test_env(root, PATH=f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin")
    code, out = _interrupt(["bash", str(INSTALL_SH)], env, root, "[1/9] Preflight checks")
    _log("sigint-install", out)
    assert code == 130, (code, out)
    assert "Interrupted." in out and "Installation failed." not in out
    assert not (root / root.name).exists()


def test_update_sigint_during_health_wait_aborts_with_interrupt_message():
    root = STATE["main"]
    install_dir = root / root.name
    env = _update_env(ODIN_REPO=_repo(root, STATE["current_version"]),
                      ODIN_READINESS_URL="http://127.0.0.1:27449/health/ready")
    code, out = _interrupt(["bash", str(install_dir / "update.sh"), "--force"], env, install_dir, "[5/6] Waiting for healthy")
    _log("sigint-update", out)
    assert code == 130, (code, out)
    assert "Interrupted." in out and "Update failed." not in out


# ── F22-22.4-01 health wait timeout (docker boundary stubbed, nothing listening) ─

def test_install_fails_closed_when_service_never_becomes_ready():
    root = _new_root("neverready")
    stub = root / "bin"
    _stub_docker(stub, 'case "$1" in version) echo 29.0.0;; image) [ "$2" = inspect ] && echo 1048576;; esac\nexit 0\n')
    env = _test_env(root, ports=(27440, 27441, 27442), PATH=f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                    ODIN_IMAGE="certh/never-ready:stub")
    result = _run(INSTALL_SH, env, timeout=200)
    out = result.stdout + result.stderr
    _log("install-never-ready", out)
    assert result.returncode == 1
    assert "Container did not become healthy within 60s" in out and "Installation failed." in out
    assert "O.D.I.N. is ready!" not in out


# ── F22-22.4-01 interactive configuration through a real TTY ─────────────────

def _pty_run(cmd: list[str], env: dict, cwd: Path, answers: list[tuple[str, str]], timeout: float = 120) -> tuple[int, str]:
    master, slave = pty.openpty()

    def make_ctty():
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
                            start_new_session=True, preexec_fn=make_ctty, close_fds=True)
    os.close(slave)
    out = b""
    pending = list(answers)
    answered_at = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r, _, _ = select.select([master], [], [], 0.3)
        if r:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if pending and pending[0][0].encode() in out[answered_at:]:
            time.sleep(0.2)
            os.write(master, pending[0][1].encode() + b"\r")
            pending.pop(0)
            answered_at = len(out)
        if proc.poll() is not None and not r:
            break
    proc.wait(timeout=30)
    os.close(master)
    return proc.returncode, out.decode(errors="replace")


def test_interactive_install_prompts_with_detected_defaults_and_uses_answers():
    root = _new_root("tty")
    env = _test_env(root, ports=(27440, 27441, 27442), ODIN_IMAGE="certh/not-present:none", ODIN_SKIP_IMAGE_PULL="1",
                    TERM="xterm")
    code, out = _pty_run(["bash", str(INSTALL_SH)], env, root,
                         [("Host IP for camera streaming", "10.11.12.13"), ("Timezone", "Europe/Berlin")])
    _log("install-tty", out)
    assert ANSI.search(out), "TTY mode should colourise output"
    assert re.search(r"Host IP for camera streaming .*\[[^\]]+\]", out)
    assert re.search(r"Timezone .*\[[^\]]+\]", out)
    env_file = root / root.name / ".env"
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line and not line.startswith("#"))
    assert values["ODIN_HOST_IP"] == "10.11.12.13" and values["TZ"] == "Europe/Berlin"
    assert values["CORS_ORIGINS"].startswith("http://10.11.12.13:27440")
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    # stops at the image phase (image deliberately absent) without starting anything
    assert code == 1 and "Candidate image is not present" in out


def test_interactive_reinstall_prompt_defaults_to_no():
    root = STATE["roots"][-1] if STATE["roots"][-1].name.endswith("-tty") else None
    assert root is not None
    env = _test_env(root, ports=(27440, 27441, 27442), ODIN_IMAGE="certh/not-present:none", ODIN_SKIP_IMAGE_PULL="1", TERM="xterm")
    code, out = _pty_run(["bash", str(INSTALL_SH)], env, root, [("Reinstall?", "")])
    _log("install-tty-reinstall", out)
    assert "Existing installation found" in out and "[y/N]" in out
    assert code == 0 and "./update.sh" in out


# ── F22-22.4-04 static security properties of the shipped scripts ────────────

@pytest.mark.parametrize("script", [INSTALL_SH, UPDATE_SH], ids=["install.sh", "update.sh"])
def test_scripts_contain_no_eval(script):
    code_lines = [l for l in script.read_text().splitlines() if not l.lstrip().startswith("#")]
    assert not [l for l in code_lines if re.search(r"(^|[;&|(\s])eval(\s|$)", l)]


# ── F22-22.1-03 docker compose up with the published compose file ────────────

def test_docker_compose_up_deploys_healthy_service():
    root = _new_root("compose")
    project = root.name
    env = {
        "PATH": BASE_PATH, "HOME": os.environ.get("HOME", "/tmp"),
        "ODIN_IMAGE": IMAGE, "ODIN_CONTAINER_NAME": f"{project}-odin",
        "ODIN_HTTP_PORT": "27443", "ODIN_GO2RTC_PORT": "27444", "ODIN_WEBRTC_PORT": "27445",
        "ODIN_DATA_PATH": str(root / "odin-data"), "ODIN_GO2RTC_DATA_PATH": str(root / "odin-data/go2rtc"),
    }
    shutil.copy(COMPOSE, root / "docker-compose.yml")
    up = subprocess.run(["docker", "compose", "-p", project, "up", "-d"], cwd=root, env=env,
                        capture_output=True, text=True, timeout=600)
    _log("compose-up", up.stdout + up.stderr)
    assert up.returncode == 0, up.stderr[-2000:]
    deadline = time.monotonic() + 240
    health = ""
    while time.monotonic() < deadline:
        health = subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", f"{project}-odin"],
                                capture_output=True, text=True).stdout.strip()
        if health == "healthy":
            break
        time.sleep(3)
    assert health == "healthy", health
    assert _ready(27443)
    with urllib.request.urlopen("http://127.0.0.1:27443/", timeout=10) as r:
        assert r.status == 200 and b"<div id=\"root\"" in r.read()
    assert (root / "odin-data/odin.db").exists()
    down = subprocess.run(["docker", "compose", "-p", project, "down"], cwd=root, env=env, capture_output=True, text=True)
    assert down.returncode == 0
