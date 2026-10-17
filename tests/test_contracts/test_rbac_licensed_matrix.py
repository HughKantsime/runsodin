"""tests/test_rbac.py's Pro/Enterprise expectations, through the cert-G harness.

A container cannot be given a test-signed license: the image embeds the production
license public key and has no override (and none is added for tests). The licensed rows
therefore run here: the cert-G harness subprocess runs the real app factory with its
throwaway trust root, installs a signed Enterprise license (a superset of the Pro and
Education features), seeds the admin and the viewer/operator/admin role users, and serves
the app with uvicorn on loopback. The unmodified tests/test_rbac.py then runs against it
over HTTP with RBAC_LICENSE_TIER=enterprise. The Community expectations of the same table
run against a live container built from the tree (scratchpad runner run_rbac_live.sh).
"""
import os
import secrets

from tests.test_contracts.test_cert_G_harness import ROOT, run_probe

PROBE = r'''
import subprocess, threading, uvicorn
install_license("enterprise")
for name, role in (("rbac-admin", "admin"), ("test_viewer_rbac", "viewer"),
                   ("test_operator_rbac", "operator"), ("test_admin_rbac", "admin")):
    seed_user(name, role)
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="on", log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(400):
    if server.started: break
    time.sleep(0.05)
port = server.servers[0].sockets[0].getsockname()[1]
env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "LICENSE_DIR")}
env.update({"BASE_URL": f"http://127.0.0.1:{port}", "ADMIN_USERNAME": "rbac-admin", "ADMIN_PASSWORD": PASSWORD,
            "TEST_VIEWER_PASSWORD": PASSWORD, "TEST_OPERATOR_PASSWORD": PASSWORD, "TEST_ADMIN_PASSWORD": PASSWORD,
            "RBAC_LICENSE_TIER": "enterprise", "ODIN_CONTAINER_EXEC": "/usr/bin/false",
            "PYTHONPATH": "tests:backend:."})
args = [sys.executable, "-m", "pytest", "tests/test_rbac.py", "-q", "-p", "no:cacheprovider", "--tb=short", "-rfE",
        "--basetemp", str(WORK / "pytest-tmp"), "--junitxml", str(WORK / "rbac-licensed-junit.xml")]
result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=1500)
(WORK / "rbac-licensed.log").write_text(result.stdout + result.stderr)
OUT["returncode"] = result.returncode
OUT["summary"] = (result.stdout.strip().splitlines() or [""])[-1]
OUT["failures"] = [l for l in result.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))][:80]
'''


def test_rbac_matrix_passes_on_an_enterprise_licensed_server(tmp_path_factory):
    extra = {"API_KEY": "k" + secrets.token_urlsafe(24)}
    for name in ("RBAC_RECORD",):
        if os.environ.get(name):
            extra[name] = os.environ[name]
    out = run_probe("rbac-licensed", PROBE, tmp_path_factory.mktemp("rbac-licensed"), extra_env=extra, timeout=1800)
    assert out["returncode"] == 0, (out["summary"], out["failures"])
    assert out["_net_attempts"] == [] or all("192.168.99.99" in str(a) for a in out["_net_attempts"]), out["_net_attempts"]
    assert ROOT.exists()
