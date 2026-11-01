#!/usr/bin/env node
// Pulls ODIN's newest verified backup off the ODIN server and keeps it encrypted on a NAS.
// Standalone and dependency-free: launchd runs an installed copy, so it never depends on a checkout.
//
// The key and the SSH key live only in secrets/ on the machine running this, never on the ODIN server or
// the NAS. Plaintext never reaches a disk here: bytes go from SSH through the cipher to the NAS, their
// hash is compared with the one the server recorded when it verified and restore-tested that backup, and
// the stored file is read back and authenticated. Nothing is pruned until the new copy and every retained
// copy have been read back. Each copy's name is authenticated, so a renamed copy won't decrypt.
//
// The NAS and SSH work runs in a child process. A hung mount blocks synchronous calls, so nothing in that
// process could notice; the parent never touches the NAS, kills a child that stops making progress, and
// always records the result.
//
// Site details are in config.json beside secrets/. See docs/BACKUP_RESTORE_RUNBOOK.md.
// "init-key", "register" and "install" change this machine or the server, so they need the owner's go.
import { execFileSync, fork, spawn } from "node:child_process";
import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";
import { chmodSync, closeSync, copyFileSync, createReadStream, createWriteStream, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, readSync, readdirSync, renameSync, rmSync, statSync, writeFileSync, writeSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { Transform, Writable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { fileURLToPath } from "node:url";

const LABEL = "app.odin.offsite-backup";
const SELF = fileURLToPath(import.meta.url);
const HOME = process.env.ODIN_OFFSITE_HOME ?? path.join(os.homedir(), "Library/Application Support/ODIN Offsite Backup");
const SECRETS = path.join(HOME, "secrets");
const KEY_FILE = path.join(SECRETS, "backup.key");
const SSH_KEY = path.join(SECRETS, "ssh_ed25519");
const STATE = path.join(HOME, "state.json");
const STATUS = path.join(HOME, "status.json");

const MAGIC = Buffer.from("ODINBAK1");
const VERSION = 1;
const IV_BYTES = 12;
const HEADER_BYTES = MAGIC.length + 1 + IV_BYTES;
const TAG_BYTES = 16;
const NAME = /^odin_backup_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_\d{6}\.db\.enc$/;
const SERVER_NAME = /^odin_backup_\d{8}_\d{6}_\d{6}\.db$/;
const KEEP_DAILY = 7, KEEP_WEEKLY = 4;
const MISSED_AFTER_HOURS = 26;
const AUDIT_EVERY_HOURS = 24;
// Runs hourly, so one failed hour (ODIN restarting, the NAS waking up) stays quiet.
const ALERT_AFTER_FAILURES = 3, ALERT_REPEAT_HOURS = 6;
const STALL_SECONDS = 120, MAX_RUN_SECONDS = 6 * 3600, COMMAND_SECONDS = 60;

// message goes to ntfy, which may be public, so it never holds a host, a path or a key. detail stays in
// status.json, on this machine. immediate means the data failed a check: alert now, not after three hours.
class JobError extends Error {
  constructor(message, { detail = null, immediate = false } = {}) {
    super(message);
    this.detail = detail;
    this.immediate = immediate;
  }
}
const checkFailed = (message, detail = null) => new JobError(message, { detail, immediate: true });
const GENERIC = "ODIN's off-server backup failed. See status.json on the machine that runs it.";

// { "target": "user@host", "mount": "/path", "share": "NAS name in the mount line", "ntfy": "url" }
// This repository is public, so none of these belong in it. "ssh", "mount_bin", "launchctl", "stall_seconds",
// "max_run_seconds" and "desktop_alert" override defaults.
function loadConfig() {
  let config;
  try {
    config = JSON.parse(readFileSync(path.join(HOME, "config.json"), "utf8"));
  } catch (error) {
    throw new JobError("The off-server backup's config.json can't be read", { detail: error.message });
  }
  for (const field of ["target", "mount", "share", "ntfy"]) {
    if (typeof config[field] !== "string" || !config[field]) throw new JobError(`config.json has no ${field}`);
  }
  return { ssh: "/usr/bin/ssh", mount_bin: "/sbin/mount", launchctl: "/bin/launchctl", stall_seconds: STALL_SECONDS,
           max_run_seconds: MAX_RUN_SECONDS, ...config, dest: path.join(config.mount, "ODIN Backups") };
}

// Atomic, so a kill mid-write can't leave a file the next run can't read.
function writeJson(file, value) {
  const temporary = `${file}.${process.pid}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  renameSync(temporary, file);
}

function readState() {
  try {
    const state = JSON.parse(readFileSync(STATE, "utf8"));
    return state && typeof state === "object" && !Array.isArray(state) ? state : {};
  } catch {
    return {}; // Missing or unreadable: the next good run writes it again.
  }
}

function assertMounted(config) {
  const line = execFileSync(config.mount_bin, { encoding: "utf8", timeout: COMMAND_SECONDS * 1000 }).split("\n").find(l => l.includes(` on ${config.mount} (`));
  if (!line) throw new JobError("The NAS is not mounted", { detail: `nothing mounted at ${config.mount}` });
  if (!line.includes("(smbfs") || !line.includes(config.share)) throw new JobError("The NAS mount is not the expected SMB share", { detail: line });
}

function loadKey() {
  let raw;
  try {
    raw = readFileSync(KEY_FILE, "utf8");
  } catch (error) {
    throw new JobError("The backup key can't be read", { detail: error.code });
  }
  const key = Buffer.from(raw.trim(), "base64");
  if (key.length !== 32) throw new JobError("Backup key must contain exactly 32 bytes");
  return key;
}

function hasMagic(file) {
  const fd = openSync(file, "r"), head = Buffer.alloc(MAGIC.length);
  try { readSync(fd, head, 0, MAGIC.length, 0); } finally { closeSync(fd); }
  return head.equals(MAGIC);
}

function sshArgs(config, ...command) {
  return ["-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
          "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", config.target, command.join(" ")];
}

function server(config, ...command) {
  try {
    return execFileSync(config.ssh, sshArgs(config, ...command), { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: COMMAND_SECONDS * 1000 });
  } catch (error) {
    const detail = String(error.stderr ?? "").trim().slice(-500) || error.code || `exit ${error.status}`;
    throw new JobError(`The ODIN server refused or didn't answer "${command[0]}"`, { detail });
  }
}

function fileTime(name) {
  const m = NAME.exec(name);
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])) : null;
}

// The header and the copy's name are authenticated with the data.
function authenticatedData(header, name) {
  return Buffer.concat([header, Buffer.from(name, "utf8")]);
}

function readEnvelope(file) {
  const size = statSync(file).size, fd = openSync(file, "r");
  if (size < HEADER_BYTES + TAG_BYTES) { closeSync(fd); throw checkFailed(`${path.basename(file)} is too short`); }
  const header = Buffer.alloc(HEADER_BYTES), tag = Buffer.alloc(TAG_BYTES);
  try { readSync(fd, header, 0, HEADER_BYTES, 0); readSync(fd, tag, 0, TAG_BYTES, size - TAG_BYTES); } finally { closeSync(fd); }
  if (!header.subarray(0, MAGIC.length).equals(MAGIC) || header[MAGIC.length] !== VERSION) throw checkFailed(`${path.basename(file)} has no encryption header`);
  return { size, header, tag };
}

function decipherFor(envelope, key, name) {
  const decipher = createDecipheriv("aes-256-gcm", key, envelope.header.subarray(HEADER_BYTES - IV_BYTES));
  decipher.setAAD(authenticatedData(envelope.header, name));
  decipher.setAuthTag(envelope.tag);
  return decipher;
}

// Decrypts in memory, only to prove the file. name is the copy's published name, whatever it's called now.
async function authenticate(file, key, name, progress = () => {}) {
  const envelope = readEnvelope(file);
  const hash = createHash("sha256");
  const source = envelope.size > HEADER_BYTES + TAG_BYTES ? createReadStream(file, { start: HEADER_BYTES, end: envelope.size - TAG_BYTES - 1 }) : [];
  try {
    await pipeline(source, decipherFor(envelope, key, name), new Writable({ write(chunk, _e, done) { hash.update(chunk); progress(); done(); } }));
  } catch (error) {
    throw checkFailed(`${name} failed authentication`, error.code ?? error.message);
  }
  return hash.digest("hex");
}

// SSH stdout -> hash of what the server sent -> cipher -> file. Plaintext is never on a disk here.
async function pullEncrypted(config, name, finalName, target, key, progress) {
  const iv = randomBytes(IV_BYTES);
  const header = Buffer.concat([MAGIC, Buffer.from([VERSION]), iv]);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(authenticatedData(header, finalName));
  const received = createHash("sha256");
  let bytes = 0;
  const tally = new Transform({ transform(chunk, _e, done) { received.update(chunk); bytes += chunk.length; progress(); done(null, chunk); } });
  const ssh = spawn(config.ssh, sshArgs(config, "send", name), { stdio: ["ignore", "pipe", "pipe"] });
  let stderr = "";
  ssh.stderr.on("data", chunk => { stderr = (stderr + chunk).slice(-500); });
  const exited = new Promise(resolve => ssh.on("close", resolve));
  writeFileSync(target, header, { mode: 0o600, flag: "wx" });
  await pipeline(ssh.stdout, tally, cipher, createWriteStream(target, { flags: "a", mode: 0o600 }));
  const code = await exited;
  if (code !== 0) throw new JobError("The ODIN server stopped sending the backup", { detail: stderr.trim() || `exit ${code}` });
  // On the NAS before anything relies on it. The read-back can still come from the client's cache.
  const fd = openSync(target, "a");
  try { writeSync(fd, cipher.getAuthTag()); fsyncSync(fd); } finally { closeSync(fd); }
  return { sha256: received.digest("hex"), bytes };
}

function syncDirectory(directory) {
  let fd;
  try {
    fd = openSync(directory, "r");
    fsyncSync(fd);
  } catch { /* Not every network filesystem allows this; the file itself was synced. */ } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

// The newest per day for KEEP_DAILY days and per ISO week for KEEP_WEEKLY weeks, as on the server.
function retained(names) {
  const isoWeek = date => {
    const day = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
    day.setUTCDate(day.getUTCDate() + 4 - (day.getUTCDay() || 7));
    const start = Date.UTC(day.getUTCFullYear(), 0, 1);
    return `${day.getUTCFullYear()}-${Math.ceil(((day - start) / 86400000 + 1) / 7)}`;
  };
  const daily = new Map(), weekly = new Map();
  for (const name of [...names].sort().reverse()) {
    const when = fileTime(name);
    const day = when.toISOString().slice(0, 10);
    if (!daily.has(day)) daily.set(day, name);
    if (!weekly.has(isoWeek(when))) weekly.set(isoWeek(when), name);
  }
  return new Set([...[...daily.values()].slice(0, KEEP_DAILY), ...[...weekly.values()].slice(0, KEEP_WEEKLY)]);
}

function auditDue(state, copied, filename) {
  if (copied || state.auditFailed || state.confirmed !== filename || !state.lastAuditAt) return true;
  return (Date.now() - new Date(state.lastAuditAt).getTime()) / 3600000 >= AUDIT_EVERY_HOURS;
}

async function run(config, state, progress) {
  assertMounted(config);
  const key = loadKey(), DEST = config.dest;
  mkdirSync(DEST, { recursive: true, mode: 0o700 });
  for (const name of readdirSync(DEST)) if (/^odin_backup_.*\.partial$/.test(name)) rmSync(path.join(DEST, name), { force: true });
  const unknown = readdirSync(DEST).filter(n => !n.startsWith(".") && statSync(path.join(DEST, n)).isFile() && !hasMagic(path.join(DEST, n)));
  if (unknown.length) throw checkFailed(`Unencrypted or unknown files on the NAS: ${unknown.join(", ")}`);
  progress();

  let latest;
  const answer = server(config, "latest");
  try {
    latest = JSON.parse(answer);
  } catch {
    throw new JobError("The ODIN server gave an unreadable answer");
  }
  progress();
  if (latest.integrity_state === "damaged") throw checkFailed("ODIN reports a damaged database; no new copy is possible");
  if (!latest.filename || !SERVER_NAME.test(latest.filename) || !/^[0-9a-f]{64}$/.test(latest.sha256 ?? "")) throw new JobError("ODIN has no verified backup to copy");
  const ageHours = (Date.now() - new Date(latest.created_at).getTime()) / 3600000;
  if (!(ageHours < MISSED_AFTER_HOURS)) throw new JobError(`Backup missed: ODIN's newest verified backup is ${Math.round(ageHours)} hours old`);

  const finalName = `${latest.filename}.enc`, final = path.join(DEST, finalName);
  let copied = false;
  if (!existsSync(final)) {
    const partial = `${final}.partial`;
    try {
      const sent = await pullEncrypted(config, latest.filename, finalName, partial, key, progress);
      if (sent.sha256 !== latest.sha256 || sent.bytes !== latest.size_bytes) throw checkFailed(`${latest.filename} doesn't match the server's receipt`);
      // The read-back: it proves what the NAS holds, not what was sent to it.
      await authenticate(partial, key, finalName, progress);
      renameSync(partial, final);
      syncDirectory(DEST);
      copied = true;
    } finally { rmSync(partial, { force: true }); }
  }

  const copies = readdirSync(DEST).filter(n => NAME.test(n));
  const updated = { confirmed: state.confirmed, lastAuditAt: state.lastAuditAt, auditFailed: false };
  const pruned = [];
  if (auditDue(state, copied, latest.filename)) {
    const good = [], bad = [];
    for (const name of copies) {
      try {
        const sha256 = await authenticate(path.join(DEST, name), key, name, progress);
        if (name === finalName && sha256 !== latest.sha256) bad.push(name);
        else good.push(name);
      } catch {
        bad.push(name);
      }
    }
    updated.lastAuditAt = new Date().toISOString();
    updated.auditFailed = bad.length > 0;
    // Only good copies count towards retention. A bad one is never deleted here, and never reported as ok.
    if (!bad.includes(finalName)) {
      const keep = retained(good);
      for (const name of good) if (!keep.has(name)) { rmSync(path.join(DEST, name)); pruned.push(name); }
      if (pruned.length) syncDirectory(DEST);
      // Forced above when this copy isn't confirmed yet, so ODIN only hears about a copy just proven.
      if (updated.confirmed !== latest.filename) {
        server(config, "confirm", latest.filename, latest.sha256);
        updated.confirmed = latest.filename;
      }
    }
    if (bad.length) {
      const error = checkFailed(`Copies on the NAS failed their check: ${bad.join(", ")}`);
      error.state = updated;
      throw error;
    }
  }
  return { state: updated, copied, pruned, copies: copies.length - pruned.length, newest: latest.filename };
}

// The child. It reports over IPC and exits. A failure here can only be an answer, never a crash the parent can't read.
async function worker() {
  let lastTick = 0;
  const progress = () => {
    const now = Date.now();
    if (now - lastTick >= 1000) { lastTick = now; process.send({ tick: true }); }
  };
  let outcome;
  try {
    const config = loadConfig();
    outcome = { ok: true, ...(await run(config, readState(), progress)) };
  } catch (error) {
    outcome = error instanceof JobError
      ? { ok: false, error: error.message, detail: error.detail, immediate: error.immediate, state: error.state }
      : { ok: false, error: GENERIC, detail: error instanceof Error ? error.message : String(error), immediate: false };
  }
  process.send({ outcome }, () => process.exit(0));
}

// Runs the child, kills it if it stops making progress, and returns what happened.
function supervise(stallSeconds, maxRunSeconds) {
  return new Promise(resolve => {
    const child = fork(SELF, ["worker"], { detached: true, stdio: ["ignore", "inherit", "inherit", "ipc"] });
    let lastProgress = Date.now(), finished = false;
    const started = Date.now();
    const finish = result => {
      if (finished) return;
      finished = true;
      clearInterval(watchdog);
      resolve(result);
    };
    const watchdog = setInterval(() => {
      const stalled = Date.now() - lastProgress > stallSeconds * 1000;
      const tooLong = Date.now() - started > maxRunSeconds * 1000;
      if (!stalled && !tooLong) return;
      // The whole group, so its ssh goes too. A child stuck on a hung mount can stay until the mount
      // answers, so don't wait for it: the result is recorded now and the next run starts on time.
      try { process.kill(-child.pid, "SIGKILL"); } catch { /* already gone */ }
      finish({ ok: false, error: "ODIN's off-server backup stalled and was stopped", detail: stalled ? `no progress for ${stallSeconds} s` : `still running after ${maxRunSeconds} s`, immediate: false });
    }, 250);
    child.on("message", message => {
      lastProgress = Date.now();
      if (message.outcome) finish(message.outcome);
    });
    child.on("error", error => finish({ ok: false, error: GENERIC, detail: error.message, immediate: false }));
    // Later than its last message, if it managed to send one.
    child.on("exit", (code, signal) => setTimeout(() => finish({ ok: false, error: GENERIC, detail: `worker ended early: ${signal ?? code}`, immediate: false }), 500));
  });
}

async function alert(config, message) {
  try {
    await fetch(config.ntfy, { method: "POST", body: message, signal: AbortSignal.timeout(10000), headers: { Title: "ODIN off-server backup FAILED", Priority: "high", Tags: "rotating_light" } });
  } catch { /* the status file and exit code still report it */ }
  if (process.platform === "darwin" && config.desktop_alert !== false) {
    try { execFileSync("/usr/bin/osascript", ["-e", `display notification ${JSON.stringify(message)} with title "ODIN off-server backup FAILED" sound name "Basso"`], { timeout: 10000 }); } catch { /* as above */ }
  }
}

function writeStatus(status) {
  try {
    writeJson(STATUS, { at: new Date().toISOString(), ...status });
  } catch (error) {
    process.stderr.write(`ODIN off-server backup could not write status.json: ${error.code ?? "error"}\n`);
  }
}

function saveState(state) {
  try {
    writeJson(STATE, state);
  } catch (error) {
    process.stderr.write(`ODIN off-server backup could not write state.json: ${error.code ?? "error"}\n`);
  }
}

async function runCommand() {
  mkdirSync(HOME, { recursive: true, mode: 0o700 });
  const state = readState();
  let config = null, outcome;
  try {
    config = loadConfig();
    outcome = await supervise(config.stall_seconds, config.max_run_seconds);
  } catch (error) {
    outcome = { ok: false, error: error.message, detail: error.detail, immediate: false };
  }
  if (outcome.ok) {
    saveState({ ...outcome.state, failures: 0 });
    writeStatus({ ok: true, copied: outcome.copied, pruned: outcome.pruned, copiesOnNas: outcome.copies, newest: outcome.newest });
    process.stdout.write(`ODIN off-server backup ok: ${outcome.copied ? "copied" : "already had"} ${outcome.newest}, ${outcome.copies} on NAS\n`);
    process.exit(0);
  }
  const failures = (state.failures ?? 0) + 1;
  const updated = { ...state, ...(outcome.state ?? {}), failures };
  const sinceAlert = state.lastAlertAt ? (Date.now() - new Date(state.lastAlertAt).getTime()) / 3600000 : Infinity;
  if ((outcome.immediate || failures >= ALERT_AFTER_FAILURES) && sinceAlert >= ALERT_REPEAT_HOURS && config) {
    await alert(config, outcome.error);
    updated.lastAlertAt = new Date().toISOString();
  }
  saveState(updated);
  writeStatus({ ok: false, error: outcome.error, detail: outcome.detail ?? null, failures });
  process.stderr.write(`ODIN off-server backup failed: ${outcome.error}\n`);
  // Not a normal return: a child stuck on a hung mount must not keep this process alive.
  process.exit(1);
}

// For a restore: writes the plaintext only after the tag has checked out, mode 0600. Keep the copy's name.
async function decrypt(file, output) {
  const key = loadKey(), name = path.basename(file);
  const sha256 = await authenticate(file, key, name);
  const envelope = readEnvelope(file);
  const temp = `${output}.partial`;
  try {
    await pipeline(createReadStream(file, { start: HEADER_BYTES, end: envelope.size - TAG_BYTES - 1 }), decipherFor(envelope, key, name), createWriteStream(temp, { mode: 0o600, flags: "wx" }));
    renameSync(temp, output);
  } finally { rmSync(temp, { force: true }); }
  process.stdout.write(`${path.basename(output)} sha256 ${sha256}\n`);
}

function initKey() {
  mkdirSync(SECRETS, { recursive: true, mode: 0o700 });
  chmodSync(SECRETS, 0o700);
  if (existsSync(KEY_FILE)) throw new Error("A backup key already exists; refusing to replace it");
  writeFileSync(KEY_FILE, `${randomBytes(32).toString("base64")}\n`, { mode: 0o600, flag: "wx" });
  process.stdout.write(`Wrote a new key to ${KEY_FILE}. Keep a second copy somewhere safe: without it no copy can be restored.\n`);
}

// Tells ODIN a copy is now expected, so a job that never manages one still raises an alert there.
function register() {
  server(loadConfig(), "expect");
  process.stdout.write("ODIN now expects off-server copies, and alerts if none is confirmed for 36 hours.\n");
}

function install() {
  const config = loadConfig();
  register();
  const bin = path.join(HOME, "bin");
  mkdirSync(bin, { recursive: true, mode: 0o700 });
  const script = path.join(bin, "odin-offsite-backup.mjs");
  copyFileSync(SELF, script);
  chmodSync(script, 0o700);
  const logs = path.join(os.homedir(), "Library/Logs/ODIN Offsite Backup");
  mkdirSync(logs, { recursive: true });
  const plist = path.join(os.homedir(), "Library/LaunchAgents", `${LABEL}.plist`);
  mkdirSync(path.dirname(plist), { recursive: true });
  const NODE = process.execPath;
  const xml = value => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;");
  writeFileSync(plist, `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array><string>${NODE}</string><string>${xml(script)}</string><string>run</string></array>
    <key>WorkingDirectory</key><string>${xml(bin)}</string>
    <key>StartInterval</key><integer>3600</integer>
    <key>StandardOutPath</key><string>${xml(path.join(logs, "offsite-backup.stdout.log"))}</string>
    <key>StandardErrorPath</key><string>${xml(path.join(logs, "offsite-backup.stderr.log"))}</string>
  </dict>
</plist>
`, { mode: 0o600 });
  const domain = `gui/${process.getuid()}`;
  try { execFileSync(config.launchctl, ["bootout", `${domain}/${LABEL}`], { stdio: "ignore" }); } catch { /* not loaded yet */ }
  execFileSync(config.launchctl, ["bootstrap", domain, plist]);
  process.stdout.write(`Installed ${LABEL}: hourly\n`);
}

function fail(error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}

const [command, ...args] = process.argv.slice(2);
if (command === "run") await runCommand();
else if (command === "worker") await worker();
else if (command === "decrypt" && args.length === 2) await decrypt(args[0], args[1]).catch(fail);
else if (command === "init-key" || command === "register" || command === "install") {
  try { ({ "init-key": initKey, register, install })[command](); } catch (error) { fail(error); }
}
else { process.stderr.write("Usage: odin-offsite-backup.mjs run|init-key|register|install|decrypt <file.enc> <output>\n"); process.exitCode = 2; }
