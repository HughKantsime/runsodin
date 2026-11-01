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
import time
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


def test_expect_starts_the_clock_once_and_confirm_keeps_it(server_data):
    database, _ = server_data
    assert _call("expect")[0] == 0
    first = read_status(offsite_path(database))["expected_since"]
    assert _call("expect")[0] == 0 and read_status(offsite_path(database))["expected_since"] == first
    assert _call("confirm", NAME, SHA)[0] == 0
    recorded = read_status(offsite_path(database))
    assert recorded["expected_since"] == first and recorded["confirmed_at"]


@pytest.mark.parametrize("arguments", [[], ["cat", "/etc/passwd"], ["send"], ["latest", "x"], ["expect", "x"], ["confirm", NAME], ["rm", NAME]])
def test_any_other_command_is_a_usage_error(server_data, arguments):
    assert _call(*arguments)[0] == offsite_export.USAGE


def test_forced_command_reads_only_ssh_original_command_and_has_no_shell():
    source = (ROOT / "backend/modules/system/offsite_export.py").read_text(encoding="utf-8")
    assert 'os.environ.get("SSH_ORIGINAL_COMMAND", "").split()' in source
    for forbidden in ("subprocess", "os.system", "eval(", "exec(", "unlink", "sqlite3"):
        assert forbidden not in source


FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, sys, time
root = os.environ["FAKE_SERVER"]
command = sys.argv[-1].split()
with open(os.path.join(root, "calls.log"), "a") as log:
    log.write(" ".join(command) + "\n")
with open(os.path.join(root, "args.log"), "w") as log:
    log.write(" ".join(sys.argv[1:]))
if os.path.exists(os.path.join(root, "down")):
    sys.stderr.write("Permission denied (publickey) for " + sys.argv[-2] + "\n")
    sys.exit(255)
if command[0] == "send" and os.path.exists(os.path.join(root, "stall")):
    sys.stdout.buffer.write(b"x" * 1000000)
    sys.stdout.buffer.flush()
    open(os.path.join(root, "ssh.pid"), "w").write(str(os.getpid()))
    time.sleep(600)
if command[0] == "latest":
    sys.stdout.write(open(os.path.join(root, "latest.json")).read())
elif command[0] == "send":
    sys.stdout.buffer.write(open(os.path.join(root, "payload"), "rb").read())
    sys.exit(int(open(os.path.join(root, "send_exit")).read()) if os.path.exists(os.path.join(root, "send_exit")) else 0)
elif command[0] == "confirm":
    open(os.path.join(root, "confirmed"), "w").write(" ".join(command[1:]))
elif command[0] == "expect":
    open(os.path.join(root, "expected"), "w").write("yes")
'''


# Loaded into the job with NODE_OPTIONS, so nothing in the job knows about it. It sits on the real
# write path: what the NAS receives differs from what the job wrote, as with a failing share.
NAS_FAULTS = r'''
import fs from "node:fs";
import { syncBuiltinESMExports } from "node:module";
const fault = process.env.NAS_FAULT;
const realCreate = fs.createWriteStream, realWriteFile = fs.writeFileSync, realRename = fs.renameSync;
if (fault === "flip-byte" || fault === "lose-tail" || fault === "hang-write") {
  fs.createWriteStream = (target, options = {}) => {
    if (!String(target).endsWith(".partial")) return realCreate(target, options);
    let written = 0;
    const wrapped = { ...fs };
    wrapped.write = (fd, buffer, offset, length, position, callback) => {
      if (fault === "hang-write") { setInterval(() => {}, 1000); return undefined; } // A real one holds the loop open too.
      const copy = Buffer.from(buffer);
      if (fault === "flip-byte" && written === 0) copy[offset + 10] ^= 0xff;
      written += length;
      const size = fault === "lose-tail" ? Math.max(0, length - 7) : length;
      return fs.write(fd, copy, offset, size, position, error => callback(error, length, buffer));
    };
    wrapped.writev = undefined;
    return realCreate(target, { ...options, fs: wrapped });
  };
}
if (fault === "hang-rename") {
  fs.renameSync = (source, target) => {
    if (String(target).includes("ODIN Backups")) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);
    return realRename(source, target);
  };
}
if (fault === "die-writing-state") {
  fs.writeFileSync = (target, data, options) => {
    if (String(target).includes("state.json")) {
      realWriteFile(target, String(data).slice(0, 5));
      process.kill(process.pid, "SIGKILL");
    }
    return realWriteFile(target, data, options);
  };
}
syncBuiltinESMExports();
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

    def job(self, *arguments, fault=None):
        env = dict(self.env)
        if fault:
            preload = self.tmp / "nas-faults.mjs"
            preload.write_text(NAS_FAULTS)
            env.update(NAS_FAULT=fault, NODE_OPTIONS=f"--import {preload.as_uri()}")
        return subprocess.run(["node", str(JOB), *arguments], env=env, capture_output=True, text=True, timeout=120)

    def set_stall_seconds(self, seconds):
        config = json.loads((self.home / "config.json").read_text())
        (self.home / "config.json").write_text(json.dumps({**config, "stall_seconds": seconds}))

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
    assert "ServerAliveInterval=15" in (job.server / "args.log").read_text()
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


def _corrupt(path):
    raw = bytearray(path.read_bytes())
    raw[100] ^= 0xFF
    path.write_bytes(bytes(raw))


def test_a_corrupt_retained_copy_is_reported_kept_and_not_counted(job):
    """Only good copies count towards retention, so the NAS can't grow without limit, and nothing bad is deleted."""
    start = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    for day in range(9):
        _old_copy(job, start + timedelta(days=day))
    before = sorted(path.name for path in job.dest.iterdir())
    victim = job.dest / before[0]
    _corrupt(victim)
    job.offer(PAYLOAD, NAME)
    result = job.job("run")
    assert result.returncode == 1 and "failed their check" in result.stderr and len(job.alerts) == 1
    assert before[0] in job.alerts[0]
    assert victim.exists() and (job.dest / (NAME + ".enc")).exists()
    assert job.status()["ok"] is False
    # The new copy is proven, so ODIN hears about it; only the old one is wrong.
    assert (job.server / "confirmed").read_text() == f"{NAME} {SHA}"


def test_a_corrupt_retained_copy_keeps_reporting_every_hour(job):
    """It used to alert on the daily run and show ok an hour later."""
    job.job("run")
    job.offer(PAYLOAD, "odin_backup_20261101_120000_000000.db")
    assert job.job("run").returncode == 0
    _corrupt(job.dest / (NAME + ".enc"))
    state = json.loads((job.home / "state.json").read_text())
    (job.home / "state.json").write_text(json.dumps({**state, "lastAuditAt": "2020-01-01T00:00:00+00:00"}))
    for _ in range(3):
        result = job.job("run")
        assert result.returncode == 1 and job.status()["ok"] is False
    assert len(job.alerts) == 1, "the repeat is limited; the status isn't"


def test_a_copy_that_never_decrypts_is_not_reported_ok_or_confirmed(job):
    """The review's reproduction: a prior run stopped before confirming, and the copy was byte-flipped."""
    assert job.job("run").returncode == 0
    (job.server / "confirmed").unlink()
    state = json.loads((job.home / "state.json").read_text())
    (job.home / "state.json").write_text(json.dumps({**state, "confirmed": None}))
    _corrupt(job.dest / (NAME + ".enc"))
    result = job.job("run")
    assert result.returncode == 1 and job.status()["ok"] is False
    assert not (job.server / "confirmed").exists()


def test_a_copy_whose_contents_differ_from_the_receipt_is_not_ok(job):
    assert job.job("run").returncode == 0
    state = json.loads((job.home / "state.json").read_text())
    (job.home / "state.json").write_text(json.dumps({**state, "confirmed": None}))
    job.offer(PAYLOAD + b"changed", NAME)
    result = job.job("run")
    assert result.returncode == 1 and job.status()["ok"] is False


def test_an_hourly_run_with_nothing_new_prunes_nothing(job):
    """Pruning only follows a run that has just re-read every copy."""
    start = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    extras = [_old_copy(job, start + timedelta(days=day)) for day in range(9)]
    job.offer(PAYLOAD, NAME)
    assert job.job("run").returncode == 0
    # Older copies appear (from a restore, say). A no-op hour must leave them.
    planted = job.dest / "odin_backup_20200101_000000_000000.db.enc"
    planted.write_bytes(extras[-1].read_bytes())
    calls = len(job.calls())
    assert job.job("run").returncode == 0
    assert planted.exists() and len(job.calls()) == calls + 1, "only the hourly latest"


@pytest.mark.parametrize("fault", ["flip-byte", "lose-tail"])
def test_a_nas_write_fault_is_caught_by_the_read_back(job, fault):
    """Real injection on the write path: the hash of what was sent is fine, what the NAS holds isn't."""
    result = job.job("run", fault=fault)
    assert result.returncode == 1 and "failed authentication" in result.stderr
    assert list(job.dest.iterdir()) == [], "nothing published, no partial left"
    assert not (job.server / "confirmed").exists()
    assert len(job.alerts) == 1 and job.status()["ok"] is False


def test_each_copy_gets_its_own_random_nonce(job):
    """Nonce reuse under one key is the catastrophic mistake for this cipher."""
    other = "odin_backup_20261101_120000_000000.db"
    assert job.job("run").returncode == 0
    job.offer(PAYLOAD, other)
    assert job.job("run").returncode == 0
    first, second = (job.dest / (NAME + ".enc")).read_bytes(), (job.dest / (other + ".enc")).read_bytes()
    assert first[9:21] != second[9:21], "the 96-bit IV"
    assert first[21:61] != second[21:61], "same plaintext, so the ciphertext shows a reused keystream"
    assert first[:9] == b"ODINBAK1\x01" and len(first) == 21 + len(PAYLOAD) + 16


def test_a_renamed_copy_will_not_decrypt(job):
    """An older copy renamed to a newer name, on a NAS someone else can write to."""
    assert job.job("run").returncode == 0
    older = job.dest / (NAME + ".enc")
    renamed = job.dest / "odin_backup_20261231_120000_000000.db.enc"
    older.rename(renamed)
    output = job.tmp / "restored.db"
    result = job.job("decrypt", str(renamed), str(output))
    assert result.returncode == 1 and "failed authentication" in result.stderr and not output.exists()
    renamed.rename(older)
    assert job.job("decrypt", str(older), str(output)).returncode == 0 and output.read_bytes() == PAYLOAD


def test_a_stalled_transfer_is_stopped_recorded_and_clears_on_the_next_run(job):
    """The review's reproduction hung with no status and no alert. launchd starts no new run while one lives."""
    job.set_stall_seconds(2)
    (job.server / "stall").write_text("")
    started = time.monotonic()
    result = job.job("run")
    assert result.returncode == 1 and "stalled" in result.stderr and time.monotonic() - started < 30
    assert job.status()["ok"] is False and "no progress" in job.status()["detail"]
    pid = int((job.server / "ssh.pid").read_text())
    for _ in range(40):
        if not Path(f"/proc/{pid}").exists() and subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0:
            break
        time.sleep(0.25)
    assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0, "its ssh must go too"
    (job.server / "stall").unlink()
    assert job.job("run").returncode == 0, "the partial it left must not block the next run"
    assert not list(job.dest.glob("*.partial"))


@pytest.mark.parametrize("fault", ["hang-write", "hang-rename"], ids=["async-write", "blocking-call"])
def test_a_hung_nas_is_stopped_even_when_it_blocks_the_process(job, fault):
    """A hung mount blocks synchronous calls, so only something outside that process can notice."""
    job.set_stall_seconds(2)
    started = time.monotonic()
    result = job.job("run", fault=fault)
    assert result.returncode == 1 and "stalled" in result.stderr and time.monotonic() - started < 30
    assert job.status()["ok"] is False


def test_repeated_stalls_alert_like_any_failure(job):
    job.set_stall_seconds(2)
    (job.server / "stall").write_text("")
    for expected in (0, 0, 1):
        assert job.job("run").returncode == 1
        assert len(job.alerts) == expected
    assert "stalled" in job.alerts[0]


def test_a_half_written_state_file_does_not_stop_the_job(job):
    """The review's reproduction: it crashed inside the catch, with no status and no alert, every run."""
    (job.home / "state.json").write_text('{"confirmed": "odin_ba')
    result = job.job("run")
    assert result.returncode == 0, result.stderr
    assert job.status()["ok"] is True and json.loads((job.home / "state.json").read_text())["failures"] == 0
    (job.home / "state.json").write_text("[1, 2]")
    assert job.job("run").returncode == 0


def test_a_half_written_state_file_is_handled_on_the_failure_path_too(job):
    (job.home / "state.json").write_text('{"confirmed": "odin_ba')
    (job.server / "down").write_text("")
    result = job.job("run")
    assert result.returncode == 1 and job.status()["ok"] is False and job.status()["failures"] == 1


def test_the_state_file_is_replaced_atomically(job):
    assert job.job("run").returncode == 0
    before = (job.home / "state.json").read_text()
    result = job.job("run", fault="die-writing-state")
    assert result.returncode != 0
    assert (job.home / "state.json").read_text() == before, "a kill mid-write must leave the old file"


def test_ssh_failures_keep_their_reason_locally_and_send_none_of_it_out(job):
    """The review's reproduction: the ssh target and key path reached a possibly public ntfy topic."""
    (job.server / "down").write_text("")
    for _ in range(3):
        job.job("run")
    assert len(job.alerts) == 1
    assert "user@server.invalid" not in job.alerts[0] and "secrets" not in job.alerts[0] and str(job.home) not in job.alerts[0]
    assert "Permission denied" in job.status()["detail"], "the real cause stays in status.json"


@pytest.mark.parametrize("break_it", ["key", "mount"])
def test_no_path_reaches_an_alert_from_any_other_failure(job, break_it):
    if break_it == "key":
        (job.home / "secrets/backup.key").unlink()
    else:
        job.set_mount("")
    for _ in range(3):
        job.job("run")
    assert len(job.alerts) == 1
    assert str(job.home) not in job.alerts[0] and str(job.mount) not in job.alerts[0] and "backup.key" not in job.alerts[0]


def test_register_tells_odin_a_copy_is_expected(job):
    assert job.job("register").returncode == 0 and (job.server / "expected").exists()
    (job.server / "down").write_text("")
    result = job.job("register")
    assert result.returncode == 1 and "user@server.invalid" not in result.stderr


def test_the_ignore_rules_cover_the_jobs_site_details_and_keys():
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for rule in ("ops/offsite_backup/config.json", "ops/offsite_backup/secrets/", "ops/offsite_backup/state.json", "ops/offsite_backup/status.json"):
        assert rule in rules


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
    assert 'else if (command === "init-key" || command === "register" || command === "install")' in source
    assert not list(JOB.parent.glob("*.key")) and not list(JOB.parent.glob("config*"))
