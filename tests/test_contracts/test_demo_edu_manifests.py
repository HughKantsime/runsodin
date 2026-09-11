"""Static deployment contracts for the EDU sandbox profile."""

import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _documents(relative_path: str):
    return list(yaml.safe_load_all((ROOT / relative_path).read_text()))


def test_odin_manifest_mounts_external_license_and_persona_secrets():
    deployment = _documents("ops/demo/k8s/40-odin.yaml")[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env_names = {entry["name"] for entry in container["env"]}
    assert {
        "ODIN_DEMO_EDU_SEED",
        "ODIN_LICENSE_READ_ONLY",
        "ODIN_DEMO_EDU_ADMIN_EMAIL",
        "ODIN_DEMO_EDU_ADMIN_PASSWORD",
        "ODIN_DEMO_EDU_TEACHER_EMAIL",
        "ODIN_DEMO_EDU_TEACHER_PASSWORD",
        "ODIN_DEMO_EDU_STUDENT_EMAIL",
        "ODIN_DEMO_EDU_STUDENT_PASSWORD",
    } <= env_names

    license_mount = next(m for m in container["volumeMounts"] if m["name"] == "license")
    assert license_mount == {
        "name": "license",
        "mountPath": "/data/odin.license",
        "subPath": "odin.license",
        "readOnly": True,
    }
    volumes = deployment["spec"]["template"]["spec"]["volumes"]
    assert next(v for v in volumes if v["name"] == "license")["secret"]["secretName"] == "odin-demo-license"


def test_publisher_manifest_uses_heartbeat_only_probes():
    deployment = _documents("ops/demo/k8s/50-publisher.yaml")[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
        command = container[probe_name]["exec"]["command"]
        assert "--check-heartbeat" in command
        assert "--max-stale-sec" in command
        assert not any("http" in part or "mosquitto" in part for part in command)


def test_reset_reseeds_via_post_schema_startup_and_waits_for_rollout():
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text()
    assert entrypoint.index("Upgrade migrations complete") < entrypoint.index("seed_edu_if_enabled.sh")
    assert entrypoint.index("seed_edu_if_enabled.sh") < entrypoint.index("O.D.I.N. is ready")

    reset = (ROOT / "ops/demo/k8s/80-cronjob-reset.yaml").read_text()
    assert "odin-demo-heartbeat" in reset
    assert "rollout status deploy/odin" in reset
    assert "rollout status deploy/publisher" in reset
    assert "rotating reviewer password" not in reset
    assert "! -name .odin-install-id" in reset
    assert "! -name .odin-device.key" in reset


def test_edu_seed_hook_failure_is_fatal(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nexit 23\n")
    fake_python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ODIN_DEMO_EDU_SEED": "1",
        "DATABASE_PATH": str(tmp_path / "odin.db"),
    }
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "docker/seed_edu_if_enabled.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 23


def test_compose_edu_overlay_requires_read_only_external_license():
    overlay = yaml.safe_load((ROOT / "ops/demo/docker-compose.edu.yml").read_text())
    odin = overlay["services"]["odin"]
    license_mount = odin["volumes"][0]
    assert license_mount["target"] == "/data/odin.license"
    assert license_mount["read_only"] is True
    assert odin["environment"]["ODIN_LICENSE_READ_ONLY"] == "1"
    assert odin["environment"]["ODIN_DEMO_EDU_SEED"] == "1"


def test_compose_edu_reset_preflights_before_wiping_data():
    makefile = (ROOT / "ops/demo/Makefile.demo").read_text()
    reset = makefile.split("demo-edu-reset:", 1)[1].split("\ndemo-write-credentials:", 1)[0]
    assert reset.index("validate_license_file") < reset.index("rm -rf")
    assert reset.index("config --quiet") < reset.index("rm -rf")
    assert reset.index(".odin-install-id") < reset.index("rm -rf")
