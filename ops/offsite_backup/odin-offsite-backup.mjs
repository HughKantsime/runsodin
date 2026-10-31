#!/usr/bin/env node
// Pulls ODIN's newest verified backup off the ODIN server and keeps it encrypted on a NAS.
// Standalone and dependency-free: launchd runs an installed copy, so it never depends on a checkout.
//
// The key and the SSH key live only in secrets/ on the machine running this, never on the ODIN server or
// the NAS. Plaintext never reaches a disk here: bytes go from SSH through the cipher to the NAS, then the
// file is read back, decrypted in memory and compared with the hash the server recorded when it verified
// and restore-tested that backup. Nothing is pruned until the new copy and every retained copy pass.
// Site details are in config.json beside secrets/. See docs/BACKUP_RESTORE_RUNBOOK.md.
// "install" and "init-key" change this machine, so they need the owner's go.
import { execFileSync, spawn } from "node:child_process";
import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";
import { chmodSync, closeSync, copyFileSync, createReadStream, createWriteStream, existsSync, mkdirSync, openSync, readFileSync, readSync, readdirSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { Transform, Writable } from "node:stream";
import { pipeline } from "node:stream/promises";

const LABEL = "app.odin.offsite-backup";
const HOME = process.env.ODIN_OFFSITE_HOME ?? path.join(os.homedir(), "Library/Application Support/ODIN Offsite Backup");
const SECRETS = path.join(HOME, "secrets");
const KEY_FILE = path.join(SECRETS, "backup.key");
const SSH_KEY = path.join(SECRETS, "ssh_ed25519");
const STATE = path.join(HOME, "state.json");
const STATUS = path.join(HOME, "status.json");

// { "target": "user@host", "mount": "/path", "share": "NAS name in the mount line", "ntfy": "url" }
// This repository is public, so none of these belong in it. "ssh" and "mount_bin" override the tools.
function loadConfig() {
  const config = JSON.parse(readFileSync(path.join(HOME, "config.json"), "utf8"));
  for (const field of ["target", "mount", "share", "ntfy"]) {
    if (typeof config[field] !== "string" || !config[field]) throw new Error(`config.json has no ${field}`);
  }
  return { ssh: "/usr/bin/ssh", mount_bin: "/sbin/mount", ...config, dest: path.join(config.mount, "ODIN Backups") };
}

const MAGIC = Buffer.from("ODINBAK1");
const HEADER_BYTES = MAGIC.length + 1 + 16 + 12;
const TAG_BYTES = 16;
const NAME = /^odin_backup_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_\d{6}\.db\.enc$/;
const SERVER_NAME = /^odin_backup_\d{8}_\d{6}_\d{6}\.db$/;
const KEEP_DAILY = 7, KEEP_WEEKLY = 4;
const MISSED_AFTER_HOURS = 26;
// Runs hourly, so one failed hour (ODIN restarting, the NAS waking up) stays quiet.
const ALERT_AFTER_FAILURES = 3, ALERT_REPEAT_HOURS = 6;

// A failed check on the data itself alerts at once, not after three hours.
class CheckFailed extends Error {}

function assertMounted(config) {
  const line = execFileSync(config.mount_bin, { encoding: "utf8" }).split("\n").find(l => l.includes(` on ${config.mount} (`));
  if (!line) throw new Error(`NAS is not mounted at ${config.mount}`);
  if (!line.includes("(smbfs") || !line.includes(config.share)) throw new Error(`${config.mount} is not the ${config.share} SMB share`);
}

function loadKey() {
  const key = Buffer.from(readFileSync(KEY_FILE, "utf8").trim(), "base64");
  if (key.length !== 32) throw new Error("Backup key must contain exactly 32 bytes");
  return key;
}

function hasMagic(file) {
  const fd = openSync(file, "r"), head = Buffer.alloc(MAGIC.length);
  try { readSync(fd, head, 0, MAGIC.length, 0); } finally { closeSync(fd); }
  return head.equals(MAGIC);
}

function sshArgs(config, ...command) {
  return ["-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15", config.target, command.join(" ")];
}

function server(config, ...command) {
  return execFileSync(config.ssh, sshArgs(config, ...command), { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], timeout: 60000 });
}

function fileTime(name) {
  const m = NAME.exec(name);
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])) : null;
}

// Decrypts in memory. Throws if the tag doesn't match; returns the plaintext's hash and size.
async function authenticate(file, key) {
  const size = statSync(file).size, fd = openSync(file, "r");
  if (size < HEADER_BYTES + TAG_BYTES) { closeSync(fd); throw new CheckFailed(`${path.basename(file)} is too short`); }
  const header = Buffer.alloc(HEADER_BYTES), tag = Buffer.alloc(TAG_BYTES);
  try { readSync(fd, header, 0, HEADER_BYTES, 0); readSync(fd, tag, 0, TAG_BYTES, size - TAG_BYTES); } finally { closeSync(fd); }
  if (!header.subarray(0, MAGIC.length).equals(MAGIC)) throw new CheckFailed(`${path.basename(file)} has no encryption header`);
  const decipher = createDecipheriv("aes-256-gcm", key, header.subarray(HEADER_BYTES - 12));
  decipher.setAAD(header); decipher.setAuthTag(tag);
  const hash = createHash("sha256");
  let bytes = 0;
  const source = size > HEADER_BYTES + TAG_BYTES ? createReadStream(file, { start: HEADER_BYTES, end: size - TAG_BYTES - 1 }) : [];
  try {
    await pipeline(source, decipher, new Writable({ write(chunk, _e, done) { hash.update(chunk); bytes += chunk.length; done(); } }));
  } catch (error) {
    throw new CheckFailed(`${path.basename(file)} failed authentication: ${error.code ?? error.message}`);
  }
  return { sha256: hash.digest("hex"), bytes };
}

// SSH stdout -> hash of what the server sent -> cipher -> file. Plaintext is never on a disk here.
async function pullEncrypted(config, name, target, key) {
  const salt = randomBytes(16), iv = randomBytes(12);
  const header = Buffer.concat([MAGIC, Buffer.from([0]), salt, iv]);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(header);
  const received = createHash("sha256");
  let bytes = 0;
  const tally = new Transform({ transform(chunk, _e, done) { received.update(chunk); bytes += chunk.length; done(null, chunk); } });
  const ssh = spawn(config.ssh, sshArgs(config, "send", name), { stdio: ["ignore", "pipe", "ignore"] });
  const exited = new Promise(resolve => ssh.on("close", resolve));
  writeFileSync(target, header, { mode: 0o600, flag: "wx" });
  await pipeline(ssh.stdout, tally, cipher, createWriteStream(target, { flags: "a", mode: 0o600 }));
  const code = await exited;
  if (code !== 0) throw new Error(`Server refused to send ${name} (exit ${code})`);
  writeFileSync(target, cipher.getAuthTag(), { flag: "a" });
  return { sha256: received.digest("hex"), bytes };
}

// The newest per day for KEEP_DAILY days and per ISO week for KEEP_WEEKLY weeks, as on the server.
export function retained(names) {
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

async function run(config) {
  assertMounted(config);
  const key = loadKey(), DEST = config.dest;
  mkdirSync(DEST, { recursive: true, mode: 0o700 });
  for (const name of readdirSync(DEST)) if (/^odin_backup_.*\.partial$/.test(name)) rmSync(path.join(DEST, name), { force: true });
  const unknown = readdirSync(DEST).filter(n => !n.startsWith(".") && !hasMagic(path.join(DEST, n)));
  if (unknown.length) throw new CheckFailed(`Unencrypted or unknown files on the NAS: ${unknown.join(", ")}`);

  const latest = JSON.parse(server(config, "latest"));
  if (latest.integrity_state === "damaged") throw new CheckFailed("ODIN reports a damaged database; no new copy is possible");
  if (!latest.filename || !SERVER_NAME.test(latest.filename) || !/^[0-9a-f]{64}$/.test(latest.sha256 ?? "")) throw new Error("ODIN has no verified backup to copy");
  const ageHours = (Date.now() - new Date(latest.created_at).getTime()) / 3600000;
  if (!(ageHours < MISSED_AFTER_HOURS)) throw new Error(`Backup missed: ODIN's newest verified backup is ${Math.round(ageHours)} hours old`);

  const finalName = `${latest.filename}.enc`, final = path.join(DEST, finalName);
  const state = existsSync(STATE) ? JSON.parse(readFileSync(STATE, "utf8")) : {};
  let copied = false;
  if (!existsSync(final)) {
    const partial = `${final}.partial`;
    try {
      const sent = await pullEncrypted(config, latest.filename, partial, key);
      if (sent.sha256 !== latest.sha256 || sent.bytes !== latest.size_bytes) throw new CheckFailed(`${latest.filename} doesn't match the server's receipt`);
      const stored = await authenticate(partial, key);
      if (stored.sha256 !== latest.sha256) throw new CheckFailed(`The NAS copy of ${latest.filename} differs after decrypting`);
      renameSync(partial, final);
      copied = true;
    } finally { rmSync(partial, { force: true }); }
  }

  // Every retained copy has to decrypt, or the run alerts and prunes nothing.
  const copies = readdirSync(DEST).filter(n => NAME.test(n));
  if (copied) for (const name of copies) await authenticate(path.join(DEST, name), key);
  const keep = retained(copies), pruned = [];
  if (copied) for (const name of copies) if (!keep.has(name)) { rmSync(path.join(DEST, name)); pruned.push(name); }
  if (state.confirmed !== latest.filename) { server(config, "confirm", latest.filename, latest.sha256); state.confirmed = latest.filename; }
  return { state, copied, pruned, copies: copies.length - pruned.length, newest: latest.filename };
}

async function alert(config, message) {
  try {
    await fetch(config.ntfy, { method: "POST", body: message, signal: AbortSignal.timeout(10000), headers: { Title: "ODIN off-server backup FAILED", Priority: "high", Tags: "rotating_light" } });
  } catch { /* the status file and exit code still report it */ }
  if (process.platform === "darwin") {
    try { execFileSync("/usr/bin/osascript", ["-e", `display notification ${JSON.stringify(message)} with title "ODIN off-server backup FAILED" sound name "Basso"`], { timeout: 10000 }); } catch { /* as above */ }
  }
}

function writeStatus(status) { writeFileSync(STATUS, `${JSON.stringify({ at: new Date().toISOString(), ...status }, null, 2)}\n`, { mode: 0o600 }); }
function saveState(state) { writeFileSync(STATE, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 }); }

async function runCommand() {
  mkdirSync(HOME, { recursive: true, mode: 0o700 });
  let config = null;
  try {
    config = loadConfig();
    const result = await run(config);
    saveState({ confirmed: result.state.confirmed, failures: 0 });
    writeStatus({ ok: true, copied: result.copied, pruned: result.pruned, copiesOnNas: result.copies, newest: result.newest });
    process.stdout.write(`ODIN off-server backup ok: ${result.copied ? "copied" : "already had"} ${result.newest}, ${result.copies} on NAS\n`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const state = existsSync(STATE) ? JSON.parse(readFileSync(STATE, "utf8")) : {};
    state.failures = (state.failures ?? 0) + 1;
    const sinceAlert = state.lastAlertAt ? (Date.now() - new Date(state.lastAlertAt).getTime()) / 3600000 : Infinity;
    const loud = error instanceof CheckFailed || state.failures >= ALERT_AFTER_FAILURES;
    if (loud && sinceAlert >= ALERT_REPEAT_HOURS) { await alert(config ?? { ntfy: "" }, message); state.lastAlertAt = new Date().toISOString(); }
    saveState(state);
    writeStatus({ ok: false, error: message, failures: state.failures });
    process.stderr.write(`ODIN off-server backup failed: ${message}\n`);
    process.exitCode = 1;
  }
}

// For a restore: writes the plaintext only after the tag has checked out, mode 0600.
async function decrypt(file, output) {
  const key = loadKey();
  const { sha256 } = await authenticate(file, key);
  const size = statSync(file).size, fd = openSync(file, "r"), header = Buffer.alloc(HEADER_BYTES), tag = Buffer.alloc(TAG_BYTES);
  try { readSync(fd, header, 0, HEADER_BYTES, 0); readSync(fd, tag, 0, TAG_BYTES, size - TAG_BYTES); } finally { closeSync(fd); }
  const decipher = createDecipheriv("aes-256-gcm", key, header.subarray(HEADER_BYTES - 12));
  decipher.setAAD(header); decipher.setAuthTag(tag);
  const temp = `${output}.partial`;
  try {
    await pipeline(createReadStream(file, { start: HEADER_BYTES, end: size - TAG_BYTES - 1 }), decipher, createWriteStream(temp, { mode: 0o600, flags: "wx" }));
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

function install() {
  const bin = path.join(HOME, "bin");
  mkdirSync(bin, { recursive: true, mode: 0o700 });
  const script = path.join(bin, "odin-offsite-backup.mjs");
  copyFileSync(new URL(import.meta.url).pathname, script);
  chmodSync(script, 0o700);
  const logs = path.join(os.homedir(), "Library/Logs/ODIN Offsite Backup");
  mkdirSync(logs, { recursive: true });
  const plist = path.join(os.homedir(), "Library/LaunchAgents", `${LABEL}.plist`);
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
  try { execFileSync("/bin/launchctl", ["bootout", `${domain}/${LABEL}`], { stdio: "ignore" }); } catch { /* not loaded yet */ }
  execFileSync("/bin/launchctl", ["bootstrap", domain, plist]);
  process.stdout.write(`Installed ${LABEL}: hourly\n`);
}

function fail(error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}

const [command, ...args] = process.argv.slice(2);
if (command === "run") await runCommand();
else if (command === "decrypt" && args.length === 2) await decrypt(args[0], args[1]).catch(fail);
else if (command === "init-key") { try { initKey(); } catch (error) { fail(error); } }
else if (command === "install") install();
else if (command !== undefined) { process.stderr.write("Usage: odin-offsite-backup.mjs run|init-key|install|decrypt <file.enc> <output>\n"); process.exitCode = 2; }
