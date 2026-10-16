"""Cert group H — single-container runtime claims, exercised against a real image.

Claims: F22-22.1-01 (supervisord, 9 processes), F22-22.1-02 (non-root odin user),
F22-22.1-04 (persistent /data volume), F22-22.1-05 (auto-generated secrets),
F22-22.1-06 (onboarding wizard in the built bundle), F22-22.1-07 (setup lock),
F22-22.3-01 (idempotent bootstrap on every entrypoint run).

Requires Docker and CERT_H_IMAGE (an image built from the current Dockerfile).
Owned resources only: container ``cert-h-ctr-<pid>``, port CERT_H_HTTP_PORT
(default 18861), bind-mounted data under CERT_H_WORK (default
artifacts/application-certification/cert-H/work). Everything is removed at the end.
Test credentials are generated at runtime and never printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = os.environ.get("CERT_H_IMAGE", "")
PORT = int(os.environ.get("CERT_H_HTTP_PORT", "18861"))
WORK = Path(os.environ.get("CERT_H_WORK", ROOT / "artifacts/application-certification/cert-H/work"))
NAME = f"cert-h-ctr-{os.getpid()}"
BASE = f"http://127.0.0.1:{PORT}"

PROGRAMS = {
    "backend", "mqtt_monitor", "moonraker_monitor", "prusalink_monitor", "elegoo_monitor",
    "go2rtc", "vision_monitor", "timelapse_capture", "report_runner",
}
SECRET_FILES = (".encryption_key", ".jwt_secret", ".odin-install-id")

pytestmark = pytest.mark.skipif(
    not IMAGE or shutil.which("docker") is None, reason="CERT_H_IMAGE and docker required"
)


def _docker(*args: str, check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"docker {args[0]} failed: {result.stderr[-1500:]}")
    return result


def _request(method: str, path: str, *, token: str | None = None, json_body=None,
             form: dict | None = None, raw: bytes | None = None, content_type: str | None = None):
    headers = {}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif raw is not None:
        data = raw
        headers["Content-Type"] = content_type or "application/octet-stream"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json(method: str, path: str, **kw):
    status, body = _request(method, path, **kw)
    try:
        return status, json.loads(body or b"null")
    except json.JSONDecodeError:
        return status, body.decode(errors="replace")


def _wait_ready(timeout: float = 180) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            status, body = _json("GET", "/health/ready")
            if status == 200 and isinstance(body, dict) and body.get("ready") is True:
                return time.monotonic() - start
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        state = _docker("inspect", "-f", "{{.State.Status}}", NAME, check=False).stdout.strip()
        if state in {"exited", "dead"}:
            raise AssertionError("container exited before readiness: " + _docker("logs", NAME, check=False).stderr[-3000:])
        time.sleep(1)
    raise AssertionError("container did not become ready")


def _run_container() -> None:
    _docker(
        "run", "-d", "--name", NAME,
        "--label", "com.runsodin.cert-h=container",
        "-p", f"127.0.0.1:{PORT}:8000",
        "-v", f"{STATE['data']}:/data",
        "-e", "TZ=Etc/UTC",
        IMAGE,
    )


def _logs() -> str:
    result = _docker("logs", NAME, check=False)
    return result.stdout + result.stderr


def _exec(*cmd: str, user: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    args = ["exec"]
    if user:
        args += ["-u", user]
    return _docker(*args, NAME, *cmd, check=check)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _secret_fingerprints() -> dict[str, str]:
    out = {}
    for name in SECRET_FILES:
        out[name] = _sha(_exec("cat", f"/data/{name}").stdout)
    return out


def _schema_fingerprint() -> str:
    code = (
        "import sqlite3,hashlib;c=sqlite3.connect('file:/data/odin.db?mode=ro',uri=True);"
        "rows=c.execute(\"select type,name,coalesce(sql,'') from sqlite_master where name not like 'sqlite_%' order by type,name\").fetchall();"
        "print(hashlib.sha256(repr(rows).encode()).hexdigest(), len(rows))"
    )
    return _exec("python3", "-c", code).stdout.strip()


STATE: dict = {}


@pytest.fixture(scope="module", autouse=True)
def container():
    data = WORK / f"container-{os.getpid()}" / "odin-data"
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)
    STATE.clear()
    STATE["data"] = data
    STATE["username"] = "certh-admin"
    STATE["password"] = "Ch1" + secrets.token_urlsafe(18)
    STATE["farm"] = "CertH Farm " + uuid.uuid4().hex[:6]
    _docker("rm", "-f", NAME, check=False)
    _run_container()
    STATE["first_ready_s"] = _wait_ready()
    try:
        yield STATE
    finally:
        _docker("rm", "-f", NAME, check=False)
        # files created by the container are owned by its uid; remove via a throwaway container
        _docker("run", "--rm", "-v", f"{data.parent}:/w", "--entrypoint", "/bin/sh", IMAGE, "-c", "rm -rf /w/odin-data", check=False)
        shutil.rmtree(data.parent, ignore_errors=True)


# ── F22-22.1-05 auto-generated secrets ────────────────────────────────────────

def test_first_run_generates_persisted_secrets_with_owner_only_permissions():
    logs = _logs()
    for line in ("Generated encryption key", "Generated JWT secret", "Generated installation ID"):
        assert line in logs, line
    for name in SECRET_FILES:
        mode = _exec("stat", "-c", "%a", f"/data/{name}").stdout.strip()
        assert mode == "600", (name, mode)
        value = _exec("cat", f"/data/{name}").stdout.strip()
        assert len(value) >= 32, name
        assert value not in logs, f"{name} value leaked into container logs"
    # The generated values are the ones the app actually uses: the Fernet key decrypts/encrypts
    check = _exec("python3", "-c",
                  "from cryptography.fernet import Fernet;k=open('/data/.encryption_key').read().strip();"
                  "f=Fernet(k.encode());print(f.decrypt(f.encrypt(b'ok')).decode())").stdout.strip()
    assert check == "ok"
    STATE["secrets"] = _secret_fingerprints()


# ── F22-22.1-01 / 22.1-02 supervisor + non-root ───────────────────────────────

def _supervisor_status() -> dict[str, tuple[str, str]]:
    result = _exec("supervisorctl", "-c", "/etc/supervisor/conf.d/odin.conf", "status", user="odin", check=False)
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            pid = ""
            if "pid" in parts:
                pid = parts[parts.index("pid") + 1].rstrip(",")
            rows[parts[0]] = (parts[1], pid)
    return rows


def test_supervisord_runs_exactly_the_nine_advertised_programs_and_they_stay_up():
    first = _supervisor_status()
    assert set(first) == PROGRAMS, sorted(first)
    assert all(state == "RUNNING" for state, _ in first.values()), first
    time.sleep(12)
    second = _supervisor_status()
    assert all(state == "RUNNING" for state, _ in second.values()), second
    assert {k: v[1] for k, v in first.items()} == {k: v[1] for k, v in second.items()}, "a program restarted"
    # PID 1 is supervisord itself (single container, supervisor-managed)
    cmdline = _exec("cat", "/proc/1/cmdline").stdout.replace("\x00", " ")
    assert "supervisord" in cmdline


def test_every_container_process_runs_as_non_root_odin_user():
    assert _exec("id", "-u", "odin").stdout.strip() == "10001"
    code = (
        "import os\n"
        "out=[]\n"
        "for p in os.listdir('/proc'):\n"
        "  if not p.isdigit(): continue\n"
        "  try:\n"
        "    st=open(f'/proc/{p}/status').read(); cmd=open(f'/proc/{p}/cmdline').read().replace(chr(0),' ').strip()\n"
        "  except OSError: continue\n"
        "  uid=[l.split()[1:] for l in st.splitlines() if l.startswith('Uid:')][0]\n"
        "  out.append((int(p),uid,cmd))\n"
        "import json;print(json.dumps(out))\n"
    )
    procs = json.loads(_exec("python3", "-c", code, user="odin").stdout)
    me = os.getpid()  # noqa: F841 - host pid unrelated; exec helper excluded by cmdline below
    long_lived = [p for p in procs if "import os" not in p[2] and p[2]]
    assert len(long_lived) >= 10, long_lived  # supervisord + 9 programs (+ uvicorn workers)
    for pid, uids, cmd in long_lived:
        assert set(uids) == {"10001"}, (pid, uids, cmd)
    status = _supervisor_status()
    for name, (_, pid) in status.items():
        assert any(str(p[0]) == pid for p in long_lived), (name, pid)


# ── F22-22.1-06 onboarding wizard (built bundle in headless Chromium) ─────────

def test_onboarding_wizard_walks_six_steps_on_first_launch():
    status, body = _json("GET", "/api/setup/status")
    assert status == 200 and body["needs_setup"] is True, body
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        api_log = []
        page.on("response", lambda r: api_log.append((r.request.method, r.url.replace(BASE, ""), r.status)) if "/api/" in r.url else None)
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_url("**/setup**", timeout=30000)
        titles = page.evaluate(
            "() => Array.from(document.querySelectorAll('div.flex.items-center.justify-center.gap-2.mb-8 > div')).length"
        )
        assert titles == 6, titles
        page.get_by_role("heading", name="Welcome to O.D.I.N.").wait_for()
        page.get_by_role("button", name="Get Started").click()
        page.get_by_role("heading", name="Create Admin Account").wait_for()
        # F22-22.1-06 (Pro-only, 2026-10-16): the image starts on Community, so the instance-name
        # field is disabled with a Pro note and no name is sent.
        instance = page.get_by_role("textbox", name="Instance Name")
        STATE["instance_disabled"] = instance.is_disabled()
        STATE["instance_note"] = page.locator("#instance-name-note").inner_text()
        page.get_by_placeholder("admin").fill(STATE["username"])
        page.get_by_placeholder("you@example.com").fill("certh@example.invalid")
        page.get_by_placeholder("Min 8 characters (uppercase, lowercase, number)").fill(STATE["password"])
        page.get_by_placeholder("Repeat password").fill(STATE["password"])
        page.get_by_role("button", name="Create Account").click()
        page.get_by_role("heading", name="Pick a Tier").wait_for(timeout=30000)
        page.get_by_role("button", name="Community (free)").click()
        page.get_by_role("button", name="Start with Community").click()
        page.get_by_role("heading", name="Add a Printer").wait_for()
        page.get_by_role("button", name="I'll add printers later").click()
        page.get_by_role("heading", name="Network Configuration").wait_for()
        page.get_by_role("button", name="Skip", exact=True).click()
        page.get_by_role("heading", name="You're all set!").wait_for()
        page.get_by_role("button", name="Open Dashboard").click()
        page.wait_for_url(lambda url: "/setup" not in url, timeout=30000)
        page.wait_for_load_state("domcontentloaded")
        final_url = page.url
        page.screenshot(path=str(WORK.parent / "wizard-final.png"))
        (WORK.parent / "wizard-api-log.json").write_text(json.dumps({"final_url": final_url.replace(BASE, ""), "api": api_log}, indent=1))
        browser.close()
    assert final_url.replace(BASE, "") in {"/", "/dashboard"}, final_url
    assert ("POST", "/api/setup/complete", 200) in api_log
    status, body = _json("GET", "/api/setup/status")
    assert body["needs_setup"] is False and body["has_users"] is True, body
    status, body = _json("POST", "/api/auth/login", form={"username": STATE["username"], "password": STATE["password"]})
    assert status == 200, body
    STATE["token"] = body["access_token"]




def test_wizard_completion_persists_setup_complete():  # F22-22.1-07 fixed by cert-fix-F
    status, body = _json("GET", "/api/setup/status")
    assert body["is_complete"] is True, body


def test_wizard_farm_name_is_pro_only_on_community():
    """F22-22.1-06 decision: the farm name sets the app name on Pro and above. On the
    Community image the wizard disables the field and the stock branding is served."""
    assert STATE["instance_disabled"] is True
    assert "Pro feature" in STATE["instance_note"]
    status, body = _json("GET", "/api/branding", token=STATE.get("token"))
    assert status == 200, body
    assert body.get("app_name") == "O.D.I.N.", body.get("app_name")


# ── F22-22.1-07 setup permanently locked ──────────────────────────────────────

CLAIM_GATE = (
    ("POST", "/api/setup/admin", {"username": "intruder", "email": "x@example.invalid", "password": "Intruder123"}),
    ("POST", "/api/setup/test-printer", {"api_type": "moonraker", "api_host": "127.0.0.1"}),
)
POST_COMPLETION = (
    ("POST", "/api/setup/printer", {"name": "intruder-printer", "api_type": "moonraker", "api_host": "127.0.0.1"}),
    ("POST", "/api/setup/network", {"host_ip": "10.0.0.9"}),
    ("POST", "/api/setup/complete", {}),
)


def _user_count() -> str:
    return _exec("python3", "-c",
                 "import sqlite3;c=sqlite3.connect('file:/data/odin.db?mode=ro',uri=True);"
                 "print(c.execute('select count(*) from users').fetchone()[0])").stdout.strip()


def _assert_claim_gate():
    for token in (None, STATE["token"]):
        for method, path, body in CLAIM_GATE:
            status, payload = _json(method, path, token=token, json_body=body)
            assert status == 403, (path, token is not None, status, payload)
    for method, path, body in POST_COMPLETION:
        status, payload = _json(method, path, json_body=body)  # anonymous
        assert status in {401, 403}, (path, status, payload)
    assert _user_count() == "1"


def test_setup_claim_endpoints_locked_after_completion():
    _assert_claim_gate()


def test_every_setup_mutation_locked_after_completion():  # F22-22.1-07 fixed by cert-fix-F
    for method, path, body in POST_COMPLETION:
        status, payload = _json(method, path, token=STATE["token"], json_body=body)
        assert status == 403, (path, status, payload)


# ── F22-22.1-04 persistence across restart and recreation, 22.1-05 reload ─────

def test_data_secrets_and_lock_persist_across_restart_and_container_recreation():
    token = STATE["token"]
    gcode = b"; cert-H persistence fixture\nG28\nG1 X10 Y10 F1200\nM104 S0\n" + secrets.token_hex(8).encode()
    boundary = "certh" + uuid.uuid4().hex
    multipart = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"certh.gcode\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + gcode + f"\r\n--{boundary}--\r\n".encode()
    status, uploaded = _json("POST", "/api/print-files/upload", token=token, raw=multipart,
                             content_type=f"multipart/form-data; boundary={boundary}")
    assert status in {200, 201}, uploaded
    status, backup = _json("POST", "/api/backups", token=token)
    assert status == 200, backup
    _exec("sh", "-c", "mkdir -p /data/timelapses && printf certh > /data/timelapses/certh-boundary.mp4", user="odin")
    stored = _exec("python3", "-c",
                   "import sqlite3;c=sqlite3.connect('file:/data/odin.db?mode=ro',uri=True);"
                   "print(c.execute('select stored_path from print_files order by id desc limit 1').fetchone()[0])").stdout.strip()
    assert stored.startswith("/data/"), stored
    schema_before = _schema_fingerprint()
    secrets_before = STATE["secrets"]

    for phase in ("restart", "recreate"):
        if phase == "restart":
            _docker("restart", NAME)
        else:
            _docker("rm", "-f", NAME)
            _run_container()
        _wait_ready()
        logs = _logs()
        assert "Loaded encryption key" in logs and "Loaded JWT secret" in logs and "Installation ID present" in logs, phase
        assert _secret_fingerprints() == secrets_before, phase
        # old JWT still accepted -> the persisted signing secret is the one in use
        status, me = _json("GET", "/api/auth/me", token=token)
        assert status == 200 and me.get("username") == STATE["username"], (phase, status)
        status, files = _json("GET", "/api/print-files", token=token)
        assert status == 200, files
        listing = files.get("items", files) if isinstance(files, dict) else files
        assert any((f.get("original_filename") or f.get("filename")) == "certh.gcode" for f in listing), phase
        stored_sha = _exec("sha256sum", stored).stdout.split()[0]
        assert stored_sha == hashlib.sha256(gcode).hexdigest(), phase
        status, backups = _json("GET", "/api/backups", token=token)
        assert any(b["filename"] == backup["filename"] for b in backups), phase
        assert _exec("cat", "/data/timelapses/certh-boundary.mp4").stdout == "certh"
        for log_name in ("backend.log", "supervisord.log", "mqtt_monitor.log"):
            assert (STATE["data"] / log_name).exists(), log_name
        # entrypoint re-ran the bootstrap: schema identical (idempotent), setup still locked
        assert _schema_fingerprint() == schema_before, phase
        _assert_claim_gate()
        assert set(_supervisor_status()) == PROGRAMS
    # host side of the bind mount holds every advertised data class
    for rel in ("odin.db", "backups", "print_files", "timelapses", ".encryption_key", ".jwt_secret", "backend.log"):
        assert (STATE["data"] / rel).exists(), rel
