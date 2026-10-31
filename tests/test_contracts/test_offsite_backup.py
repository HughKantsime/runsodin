"""The off-server copy: the forced command on the server, and the job that pulls and encrypts."""
import hashlib
import http.server
import io
import json
import os
import re
import stat
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modules.system import offsite_export
from modules.system.backup_schedule import RECEIPT_SUFFIX
from modules.system.db_guardian_status import offsite_path, read_status, status_path, write_status

ROOT = Path(__file__).resolve().parents[2]
JOB = ROOT / "ops/offsite_backup/odin-offsite-backup.mjs"
NAME = "odin_backup_20261031_120000_000000.db"
PAYLOAD = b"SQLite format 3\x00" + b"synthetic database bytes " * 4000
SHA = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture
def server_data(tmp_path, monkeypatch):
    database = tmp_path / "data" / "odin.db"
    backups = database.parent / "backups"
    backups.mkdir(parents=True)
    (backups / NAME).write_bytes(PAYLOAD)
    (backups / (NAME + RECEIPT_SUFFIX)).write_text(json.dumps({
        "created_at": "2026-10-31T12:00:00+00:00", "sha256": SHA, "size_bytes": len(PAYLOAD)}))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    return database, backups


def _call(*arguments):
    output = io.BytesIO()
    return offsite_export.main(list(arguments), output), output.getvalue()


def test_latest_reports_the_receipt_and_the_guardian_state(server_data):
    database, _ = server_data
    write_status(status_path(database), {"integrity": {"state": "ok"}, "backup": {"state": "ok"}})
    code, output = _call("latest")
    assert code == 0
    assert json.loads(output) == {"filename": NAME, "sha256": SHA, "size_bytes": len(PAYLOAD), "created_at": "2026-10-31T12:00:00+00:00",
                                  "integrity_state": "ok", "backup_state": "ok"}


def test_latest_has_nothing_to_offer_without_a_scheduled_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'odin.db'}")
    assert json.loads(_call("latest")[1])["filename"] is None


def test_send_streams_exactly_that_backup(server_data):
    code, output = _call("send", NAME)
    assert code == 0 and output == PAYLOAD


@pytest.mark.parametrize("name", ["../odin.db", "/data/odin.db", "odin.db", ".encryption_key", NAME + "x", "odin_backup_1.db"])
def test_send_refuses_anything_else(server_data, name):
    database, backups = server_data
    (database.parent / "odin.db").write_bytes(b"live database")
    code, output = _call("send", name)
    assert code == offsite_export.REFUSED and output == b""


def test_send_refuses_a_path_even_with_a_receipt_beside_the_target(server_data):
    database, backups = server_data
    database.write_bytes(b"live database")
    (database.parent / ("odin.db" + RECEIPT_SUFFIX)).write_text(json.dumps({"sha256": "a" * 64}))
    code, output = _call("send", "../odin.db")
    assert code == offsite_export.REFUSED and output == b""


def test_send_refuses_a_backup_with_no_receipt(server_data):
    _, backups = server_data
    other = "odin_backup_20200101_000000_000000.db"
    (backups / other).write_bytes(b"manual backup")
    assert _call("send", other)[0] == offsite_export.REFUSED


def test_confirm_records_a_matching_hash_and_refuses_a_wrong_one(server_data):
    database, _ = server_data
    assert _call("confirm", NAME, "0" * 64)[0] == offsite_export.REFUSED
    assert read_status(offsite_path(database)) == {}
    assert _call("confirm", NAME, SHA)[0] == 0
    recorded = read_status(offsite_path(database))
    assert recorded["filename"] == NAME and recorded["sha256"] == SHA and recorded["confirmed_at"]


@pytest.mark.parametrize("arguments", [[], ["cat", "/etc/passwd"], ["send"], ["latest", "x"], ["confirm", NAME], ["rm", NAME]])
def test_any_other_command_is_a_usage_error(server_data, arguments):
    assert _call(*arguments)[0] == offsite_export.USAGE


def test_forced_command_reads_only_ssh_original_command_and_has_no_shell():
    source = (ROOT / "backend/modules/system/offsite_export.py").read_text(encoding="utf-8")
    assert 'os.environ.get("SSH_ORIGINAL_COMMAND", "").split()' in source
    for forbidden in ("subprocess", "os.system", "eval(", "exec(", "unlink", "sqlite3"):
        assert forbidden not in source


FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, sys
root = os.environ["FAKE_SERVER"]
command = sys.argv[-1].split()
with open(os.path.join(root, "calls.log"), "a") as log:
    log.write(" ".join(command) + "\n")
if os.path.exists(os.path.join(root, "down")):
    sys.exit(255)
if command[0] == "latest":
    sys.stdout.write(open(os.path.join(root, "latest.json")).read())
elif command[0] == "send":
    sys.stdout.buffer.write(open(os.path.join(root, "payload"), "rb").read())
    sys.exit(int(open(os.path.join(root, "send_exit")).read()) if os.path.exists(os.path.join(root, "send_exit")) else 0)
elif command[0] == "confirm":
    open(os.path.join(root, "confirmed"), "w").write(" ".join(command[1:]))
'''


class Harness:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.server = tmp_path / "server"
        self.server.mkdir()
        self.mount = tmp_path / "nas"
        self.mount.mkdir()
        self.dest = self.mount / "ODIN Backups"
        self.home = tmp_path / "home"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.alerts = []
        ssh = self.bin / "ssh"
        ssh.write_text(FAKE_SSH)
        ssh.chmod(0o755)
        self.mount_bin = self.bin / "mount"
        self.set_mount(f"//user@TEST-NAS._smb._tcp.local/share on {self.mount} (smbfs, nodev, nosuid)")
        harness = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                harness.alerts.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
                self.send_response(200)
                self.end_headers()
            def log_message(self, *_):
                pass
        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "FAKE_SERVER": str(self.server), "ODIN_OFFSITE_HOME": str(self.home)}
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({
            "target": "user@server.invalid", "mount": str(self.mount), "share": "TEST-NAS", "ssh": str(ssh),
            "mount_bin": str(self.mount_bin), "ntfy": f"http://127.0.0.1:{self.http.server_address[1]}/alerts"}))
        self.offer(PAYLOAD, NAME)

    def set_mount(self, line):
        self.mount_bin.write_text(f"#!/bin/sh\necho '{line}'\n")
        self.mount_bin.chmod(0o755)

    def offer(self, payload, name, *, sha=None, created=None, integrity="ok"):
        (self.server / "payload").write_bytes(payload)
        (self.server / "latest.json").write_text(json.dumps({
            "filename": name, "sha256": sha or hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload),
            "created_at": (created or datetime.now(timezone.utc)).isoformat(), "integrity_state": integrity, "backup_state": "ok"}))

    def job(self, *arguments):
        return subprocess.run(["node", str(JOB), *arguments], env=self.env, capture_output=True, text=True, timeout=120)

    def calls(self):
        path = self.server / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def status(self):
        return json.loads((self.home / "status.json").read_text())

    def key(self):
        return (self.home / "secrets/backup.key").read_text().strip()


@pytest.fixture
def job(tmp_path):
    harness = Harness(tmp_path)
    harness.created = harness.job("init-key")
    assert harness.created.returncode == 0, harness.created.stderr
    yield harness
    harness.http.shutdown()


def test_init_key_is_private_refuses_to_overwrite_and_never_prints_the_key(job):
    key_file = job.home / "secrets/backup.key"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((job.home / "secrets").stat().st_mode) == 0o700
    key = job.key()
    assert key not in job.created.stdout + job.created.stderr
    again = job.job("init-key")
    assert again.returncode == 1 and job.key() == key and "refusing to replace" in again.stderr
    assert key not in again.stdout + again.stderr


def test_a_run_stores_only_encrypted_bytes_that_decrypt_to_the_verified_backup(job):
    result = job.job("run")
    assert result.returncode == 0, result.stderr
    stored = job.dest / (NAME + ".enc")
    raw = stored.read_bytes()
    assert raw.startswith(b"ODINBAK1") and b"SQLite format 3" not in raw and b"synthetic database" not in raw
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert (job.server / "confirmed").read_text() == f"{NAME} {SHA}"
    assert job.status()["ok"] is True and job.alerts == []
    output = job.tmp / "restored.db"
    assert job.job("decrypt", str(stored), str(output)).returncode == 0
    assert output.read_bytes() == PAYLOAD and stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not list(job.dest.glob("*.partial"))


def test_the_key_appears_in_no_output_status_alert_or_file_on_the_nas(job):
    job.job("run")
    (job.server / "down").write_text("")
    failed = [job.job("run") for _ in range(3)]
    key = job.key()
    text = "".join(run.stdout + run.stderr for run in failed) + (job.home / "status.json").read_text() + (job.home / "state.json").read_text() + "".join(job.alerts)
    assert key not in text and job.alerts
    assert key.encode() not in (job.dest / (NAME + ".enc")).read_bytes()


def test_an_hourly_run_with_nothing_new_downloads_nothing(job):
    assert job.job("run").returncode == 0
    assert job.job("run").returncode == 0
    assert sum(call.startswith("send") for call in job.calls()) == 1
    assert sum(call.startswith("confirm") for call in job.calls()) == 1


@pytest.mark.parametrize("mount", ["", "//other@NAS/x on {m} (smbfs)", "/dev/disk3 on {m} (apfs, local)", "TEST-NAS:/x on {m} (nfs, nodev)"],
                         ids=["unmounted", "other-share", "look-alike-folder", "same-name-not-smb"])
def test_a_missing_or_wrong_mount_writes_nothing_and_fails(job, mount):
    job.set_mount(mount.format(m=job.mount))
    result = job.job("run")
    assert result.returncode == 1 and ("NAS" in result.stderr or "share" in result.stderr)
    assert not job.dest.exists() and job.calls() == []


def test_one_or_two_passing_outages_are_quiet_and_three_alert_then_it_throttles(job):
    (job.server / "down").write_text("")
    for expected in (0, 0, 1, 1):
        assert job.job("run").returncode == 1
        assert len(job.alerts) == expected
    assert job.status()["ok"] is False


def test_bytes_that_differ_from_the_receipt_are_never_published_and_alert_at_once(job):
    job.offer(PAYLOAD, NAME, sha="0" * 64)
    result = job.job("run")
    assert result.returncode == 1 and "receipt" in result.stderr
    assert list(job.dest.iterdir()) == [] and not (job.server / "confirmed").exists()
    assert len(job.alerts) == 1


def test_a_server_that_fails_mid_send_publishes_nothing(job):
    (job.server / "send_exit").write_text("3")
    assert job.job("run").returncode == 1
    assert list(job.dest.iterdir()) == [] and not (job.server / "confirmed").exists()


def _old_copy(job, when):
    """A real encrypted copy from an earlier run, named for that day."""
    name = f"odin_backup_{when:%Y%m%d_%H%M%S}_000000.db"
    job.offer(PAYLOAD, name)
    assert job.job("run").returncode == 0
    return job.dest / (name + ".enc")


def test_a_missed_backup_on_the_server_alerts_after_the_grace_hours(job):
    job.offer(PAYLOAD, NAME, created=datetime.now(timezone.utc) - timedelta(hours=27))
    for _ in range(3):
        result = job.job("run")
    assert result.returncode == 1 and "Backup missed" in result.stderr and "Backup missed" in job.alerts[0]
    assert list(job.dest.iterdir()) == []


def test_damage_reported_by_the_server_alerts_at_once_and_keeps_every_copy(job):
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    kept = [_old_copy(job, now - timedelta(days=day)) for day in range(3)]
    job.offer(PAYLOAD, NAME, integrity="damaged")
    result = job.job("run")
    assert result.returncode == 1 and "damaged" in result.stderr and len(job.alerts) == 1
    assert all(copy.exists() for copy in kept)


def test_retention_prunes_old_copies_only_after_a_good_run(job):
    start = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    copies = [_old_copy(job, start + timedelta(days=day)) for day in range(40)]
    left = sorted(path.name for path in job.dest.iterdir())
    newest = {copy.name for copy in copies[-7:]}
    assert newest <= set(left) and 7 <= len(left) <= 11
    assert len({datetime.strptime(name[12:20], "%Y%m%d").isocalendar()[:2] for name in left}) >= 4


def test_a_corrupt_retained_copy_blocks_pruning_and_alerts(job):
    start = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    for day in range(9):
        _old_copy(job, start + timedelta(days=day))
    before = sorted(path.name for path in job.dest.iterdir())
    victim = job.dest / before[0]
    raw = bytearray(victim.read_bytes())
    raw[100] ^= 0xFF
    victim.write_bytes(bytes(raw))
    job.offer(PAYLOAD, NAME)
    result = job.job("run")
    assert result.returncode == 1 and "authentication" in result.stderr and len(job.alerts) == 1
    assert set(before) <= {path.name for path in job.dest.iterdir()}


def test_an_unencrypted_file_on_the_nas_stops_the_run(job):
    job.dest.mkdir()
    (job.dest / "odin.db").write_bytes(b"SQLite format 3\x00 plaintext")
    result = job.job("run")
    assert result.returncode == 1 and "Unencrypted" in result.stderr and len(job.alerts) == 1
    assert job.calls() == []


def test_a_missing_or_short_key_fails_before_anything_moves(job):
    key_file = job.home / "secrets/backup.key"
    key_file.write_text("dG9vIHNob3J0\n")
    result = job.job("run")
    assert result.returncode == 1 and "32 bytes" in result.stderr and job.calls() == []
    key_file.unlink()
    assert job.job("run").returncode == 1 and job.calls() == []


def test_a_tampered_copy_will_not_decrypt_and_leaves_no_output(job):
    job.job("run")
    stored = job.dest / (NAME + ".enc")
    raw = bytearray(stored.read_bytes())
    raw[-1] ^= 0xFF
    stored.write_bytes(bytes(raw))
    output = job.tmp / "restored.db"
    result = job.job("decrypt", str(stored), str(output))
    assert result.returncode == 1 and "authentication" in result.stderr and "at " not in result.stderr
    assert not output.exists() and not Path(f"{output}.partial").exists()


def test_a_partial_from_a_killed_run_is_cleared(job):
    job.dest.mkdir()
    leftover = job.dest / (NAME + ".enc.partial")
    leftover.write_bytes(b"ODINBAK1 half")
    assert job.job("run").returncode == 0
    assert not leftover.exists() and (job.dest / (NAME + ".enc")).exists()


def test_a_missing_or_incomplete_config_fails_before_anything_moves(job):
    config = job.home / "config.json"
    saved = json.loads(config.read_text())
    config.write_text(json.dumps({key: value for key, value in saved.items() if key != "target"}))
    result = job.job("run")
    assert result.returncode == 1 and "no target" in result.stderr and job.calls() == []
    config.unlink()
    assert job.job("run").returncode == 1 and job.calls() == [] and job.status()["ok"] is False


def test_this_public_repository_holds_no_site_details_and_installs_nothing_by_itself():
    source = JOB.read_text(encoding="utf-8")
    assert not re.search(r"\b(10|172|192)\.\d+\.\d+\.\d+\b", source)
    assert 'else if (command === "install") install();' in source
    assert not list(JOB.parent.glob("*.key")) and not list(JOB.parent.glob("config*"))
