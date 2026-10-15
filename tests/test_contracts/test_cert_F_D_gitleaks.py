"""Cert F / cluster D — F12-12.3-04 gitleaks pre-commit hook + CI secret scanning.

Behavior evidence: the real gitleaks CLI, driven with the repository's own .gitleaks.toml and the
pre-commit hook's command line, blocks a staged synthetic secret (generated in-test, never real) and
passes a clean commit in a throwaway git repo. CI evidence: the workflows are parsed and the CI
secret gate actually wired into them is identified (it is NOT gitleaks-action). Findings are never
printed; every scan uses --redact.
"""
from __future__ import annotations

import secrets
import shutil
import string
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GITLEAKS = shutil.which("gitleaks")
pytestmark = pytest.mark.skipif(GITLEAKS is None, reason="gitleaks CLI not installed")


DEFECT_RULES = ("DEFECT: .gitleaks.toml:1-21 defines only an [allowlist] - no [[rules]] and no [extend] useDefault=true - "
                "so every gitleaks run that loads it (pre-commit hook via auto-discovered .gitleaks.toml, Makefile:196 "
                "security-secrets, CI CV03 security-structured `--config .gitleaks.toml`) has ZERO rules. Scenario: a "
                "contributor stages a real GitHub PAT/AWS key; the hook and the Trusted Validation secret gate both "
                "report 'no leaks found' and the credential is committed/pushed into history.")

def _precommit_hook():
    cfg = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    for repo in cfg["repos"]:
        if repo["repo"].rstrip("/").endswith("gitleaks/gitleaks"):
            return repo
    return None


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for k, v in (("user.email", "cert@example.test"), ("user.name", "cert")):
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True)
    return tmp_path


def _hook_scan(repo: Path) -> int:
    # Equivalent of the gitleaks pre-commit hook entry (staged changes, redacted).
    r = subprocess.run([GITLEAKS, "git", "--pre-commit", "--staged", "--redact=100", "--no-banner",
                        "--config", str(ROOT / ".gitleaks.toml"), str(repo)],
                       capture_output=True, text=True, timeout=120)
    return r.returncode


def test_precommit_config_declares_gitleaks_hook():
    repo = _precommit_hook()
    assert repo is not None
    assert [h["id"] for h in repo["hooks"]] == ["gitleaks"]
    assert repo["rev"] == "v8.21.2"


def _token():
    return "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))


def test_positive_control_default_gitleaks_rules_detect_synthetic_token(tmp_path):
    d = tmp_path / "scan"
    d.mkdir()
    (d / "settings.py").write_text(f'GITHUB_TOKEN = "{_token()}"\n')
    r = subprocess.run([GITLEAKS, "dir", "--redact=100", "--no-banner", str(d)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 1


@pytest.mark.xfail(strict=True, reason=DEFECT_RULES)
def test_gitleaks_hook_command_blocks_staged_synthetic_secret(tmp_path):
    repo = _repo(tmp_path)
    token = "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
    (repo / "settings.py").write_text(f'GITHUB_TOKEN = "{token}"\n')
    subprocess.run(["git", "-C", str(repo), "add", "settings.py"], check=True)
    assert _hook_scan(repo) == 1


def test_gitleaks_hook_command_passes_clean_staged_change(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("print('hello')\n")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    assert _hook_scan(repo) == 0


@pytest.mark.xfail(strict=True, reason=DEFECT_RULES)
def test_hook_with_autodiscovered_repo_config_blocks_staged_secret(tmp_path):
    """pre-commit runs gitleaks in the repo root, where gitleaks auto-loads .gitleaks.toml."""
    repo = _repo(tmp_path)
    shutil.copy(ROOT / ".gitleaks.toml", repo / ".gitleaks.toml")
    (repo / "settings.py").write_text(f'GITHUB_TOKEN = "{_token()}"\n')
    subprocess.run(["git", "-C", str(repo), "add", "settings.py", ".gitleaks.toml"], check=True)
    r = subprocess.run([GITLEAKS, "git", "--pre-commit", "--staged", "--redact=100", "--no-banner", "."],
                       cwd=repo, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1


@pytest.mark.xfail(strict=True, reason=DEFECT_RULES)
def test_repository_allowlist_config_loads_and_still_detects(tmp_path):
    """The repo .gitleaks.toml allowlist must not neuter detection outside its allowlisted paths."""
    d = tmp_path / "scan"
    d.mkdir()
    token = "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
    (d / "conftest.py").write_text(f'TOKEN = "{token}"\n')     # not tests/conftest.py => not allowlisted
    r = subprocess.run([GITLEAKS, "dir", "--redact=100", "--no-banner", "--config", str(ROOT / ".gitleaks.toml"), str(d)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1


def _workflows():
    out = {}
    for p in sorted((ROOT / ".github/workflows").glob("*.yml")):
        out[p.name] = yaml.safe_load(p.read_text())
    return out


def test_ci_has_no_gitleaks_action_step():
    """Claim says 'CI secret scanning (gitleaks-action)'. No workflow step uses gitleaks/gitleaks-action."""
    uses = []
    for name, wf in _workflows().items():
        for job in (wf.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if "uses" in step:
                    uses.append(step["uses"])
    assert uses, "no action steps parsed"
    assert not any("gitleaks" in u.lower() for u in uses)


def test_ci_secret_scan_is_gitleaks_cli_inside_manually_dispatched_trusted_validation():
    """What CI actually has: Trusted Validation (workflow_dispatch only, self-hosted) runs
    `make trusted-validation-gate`, whose CV03 component runs `make security-structured`, which
    invokes `gitleaks detect ... --redact=100` (dispatch proven by test_SecretScanPipelineClaimAcceptance)."""
    wfs = _workflows()
    tv = wfs["trusted-validation.yml"]
    triggers = tv.get("on", tv.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    runs = " ".join(s.get("run", "") for j in tv["jobs"].values() for s in j.get("steps", []))
    assert "make trusted-validation-gate" in runs
    for name, wf in wfs.items():
        trig = wf.get("on", wf.get(True))
        assert "push" not in trig and "pull_request" not in trig, name
    from ops.release_control.policy import load_inventory
    comp = next(c for c in load_inventory()["components"] if c["id"] == "CV03")
    assert comp["command"][:2] == ["make", "security-structured"]
