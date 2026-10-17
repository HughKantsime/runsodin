#!/usr/bin/env node
// Set the release date that CHANGELOG.md, TERMS_OF_SERVICE.md and PRIVACY_POLICY.md carry as a
// placeholder, all three together, and remove the placeholder marker.
// Usage: node ops/set-release-date.mjs [--root DIR] <version> <YYYY-MM-DD>
// Called by ops/bump-version.sh. Fails (and writes nothing) unless the newest marked CHANGELOG
// release is <version> and both legal pages carry the marker.
import { readFileSync, writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const MARKER = '<!-- release-date placeholder: set by ops/bump-version.sh -->';
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
  'October', 'November', 'December'];

const args = process.argv.slice(2);
let root = join(dirname(fileURLToPath(import.meta.url)), '..');
if (args[0] === '--root') { root = args[1]; args.splice(0, 2); }
const [version, date] = args;
const fail = (msg) => { console.error(`set-release-date: ${msg}`); process.exit(1); };
if (!/^\d+\.\d+\.\d+$/.test(version || '')) fail('version must be X.Y.Z');
const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date || '');
if (!m) fail('date must be YYYY-MM-DD');
const human = `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}, ${m[1]}`;

const read = (name) => readFileSync(join(root, name), 'utf8');
const out = {};

const changelog = read('CHANGELOG.md');
const head = new RegExp(`^## \\[(\\d+\\.\\d+\\.\\d+)\\] - \\d{4}-\\d{2}-\\d{2}\\n\\n${MARKER.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\n`, 'm');
const hm = head.exec(changelog);
if (!hm) fail('CHANGELOG.md has no release block with the placeholder marker');
if (hm[1] !== version) fail(`the placeholder release in CHANGELOG.md is ${hm[1]}, not ${version}`);
out['CHANGELOG.md'] = changelog.replace(head, `## [${version}] - ${date}\n\n`);

for (const name of ['TERMS_OF_SERVICE.md', 'PRIVACY_POLICY.md']) {
  const text = read(name);
  const line = new RegExp(`^\\*\\*Last Updated:\\*\\* [^\\n]*? ${MARKER.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`, 'm');
  if (!line.test(text)) fail(`${name} has no Last Updated line with the placeholder marker`);
  out[name] = text.replace(line, `**Last Updated:** ${human}`);
}

for (const [name, text] of Object.entries(out)) writeFileSync(join(root, name), text);
console.log(`set-release-date: ${version} dated ${date} in CHANGELOG.md, TERMS_OF_SERVICE.md and PRIVACY_POLICY.md`);
