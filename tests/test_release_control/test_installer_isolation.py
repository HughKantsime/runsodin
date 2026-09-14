import subprocess
from pathlib import Path

import pytest

from ops.release_control import installer_smoke
from ops.release_control.installer_smoke import InstallerSmokeError, Resources

ROOT = Path(__file__).parents[2]


def test_IN01_real_isolated_installer_entrypoint_is_wired():
    source = (ROOT / "ops/release_control/local_gate.py").read_text()
    overlay = (ROOT / "install/docker-compose.test.yml").read_text()
    assert "ops.release_control.installer_smoke" in source
    assert "com.runsodin.install-smoke" in overlay


def test_IN02_updater_reuses_isolated_compose_project():
    source = (ROOT / "install/update.sh").read_text()
    assert 'compose_cmd=("${COMPOSE_CMD[@]}" -p "$ODIN_COMPOSE_PROJECT"' in source
    assert "COMPOSE_CMD=(docker-compose)" in source
    assert "docker-compose.test.yml" in source


def test_IN03_invalid_resource_name_is_rejected():
    with pytest.raises(InstallerSmokeError):
        Resources.from_run_id("BAD name")


def test_IN04_path_outside_run_root_is_rejected(tmp_path):
    with pytest.raises(InstallerSmokeError):
        installer_smoke.validate_test_path(tmp_path.parent / "outside", tmp_path / "root")


def test_IN05_occupied_port_is_rejected():
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        with pytest.raises(InstallerSmokeError):
            installer_smoke.assert_ports_free([sock.getsockname()[1]])
    finally:
        sock.close()


@pytest.mark.parametrize("kind", ["container", "network", "volume"], ids=["IN06", "IN07", "IN08"])
def test_exact_preexisting_resource_is_rejected(monkeypatch, tmp_path, kind):
    resources = Resources.from_run_id("abc-123")
    target = getattr(resources, kind)
    monkeypatch.setattr(installer_smoke, "_inspect", lambda probe_kind, name: {} if (probe_kind, name) == (kind, target) else None)
    with pytest.raises(InstallerSmokeError):
        installer_smoke.assert_absent(resources, tmp_path / "install", tmp_path / "data")


def test_IN09_preexisting_path_is_rejected(monkeypatch, tmp_path):
    resources = Resources.from_run_id("abc-123")
    monkeypatch.setattr(installer_smoke, "_inspect", lambda *_: None)
    install = tmp_path / "install"
    install.mkdir()
    with pytest.raises(InstallerSmokeError):
        installer_smoke.assert_absent(resources, install, tmp_path / "data")


def test_IN10_candidate_image_identity_comparison_is_exact():
    source = (ROOT / "ops/release_control/installer_smoke.py").read_text()
    assert "actual_image != expected_image" in source


def test_IN11_database_backed_readiness_is_mandatory():
    assert "/health/ready" in (ROOT / "install/install.sh").read_text()
    assert "ready\"[[:space:]]*:[[:space:]]*true" in (ROOT / "install/install.sh").read_text()


def test_IN12_cleanup_refuses_identity_or_label_mismatch(monkeypatch):
    resources = Resources.from_run_id("abc-123")
    records = {"container": {"name": resources.container, "identity": "expected"}}
    payload = {"Id": "different", "Config": {"Labels": {installer_smoke.LABEL: resources.run_id, f"{installer_smoke.LABEL}.kind": "container"}}}
    monkeypatch.setattr(installer_smoke, "_inspect", lambda *_: payload)
    with pytest.raises(InstallerSmokeError):
        installer_smoke.cleanup_owned(resources, records)


def test_IN13_zero_owned_residue_passes(monkeypatch):
    resources = Resources.from_run_id("abc-123")
    monkeypatch.setattr(installer_smoke, "_run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=""))
    installer_smoke.assert_zero_residue(resources)


def test_IN14_unix_defaults_remain_customer_compatible():
    source = (ROOT / "install/install.sh").read_text()
    assert 'ODIN_CONTAINER_NAME="${ODIN_CONTAINER_NAME:-odin}"' in source
    assert 'ODIN_HTTP_PORT="${ODIN_HTTP_PORT:-8000}"' in source
    assert 'INSTALL_DIR="${ODIN_INSTALL_DIR:-./odin}"' in source


def test_IN15_powershell_defaults_remain_customer_compatible():
    source = (ROOT / "install/install.ps1").read_text()
    assert "else { 'odin' }" in source
    assert "else { 8000 }" in source
    assert '"C:\\odin"' in source


def test_custom_http_port_controls_readiness_exposure_and_user_guidance():
    shell = (ROOT / "install/install.sh").read_text()
    powershell = (ROOT / "install/install.ps1").read_text()
    assert 'http://localhost:${ODIN_HTTP_PORT}/health/ready' in shell
    assert '</dev/tcp/$PUBLIC_IP/$ODIN_HTTP_PORT' in shell
    assert 'CORS_ORIGINS=http://${HOST_IP}:${ODIN_HTTP_PORT}' in shell
    assert '-Port $HttpPort' in powershell
    assert 'http://localhost:$HttpPort' in powershell
    assert 'localport=$HttpPort' in powershell


def test_updater_reloads_persisted_custom_identity_and_port():
    source = (ROOT / "install/update.sh").read_text()
    for key in ("ODIN_COMPOSE_PROJECT", "ODIN_CONTAINER_NAME", "ODIN_HTTP_PORT"):
        assert f"^{key}=" in source
    assert 'URL|http://${HOST_IP}:${ODIN_HTTP_PORT}' in source


def test_installer_failure_cleanup_has_initialized_image_state():
    source = (ROOT / "ops/release_control/installer_smoke.py").read_text()
    assert source.index("image_built = False") < source.index("try:\n        assert_ports_free")


def test_installer_outer_timeout_covers_uncached_build():
    inventory = (ROOT / "ops/release_control/inventory.json").read_text()
    assert '"id":"CV01"' in inventory
    assert '"timeout_seconds":2700' in inventory
