#!/usr/bin/env node
// Check legal revision dates or record a deliberate text change.
// Usage: node ops/legal-revision.mjs --check
//        node ops/legal-revision.mjs --record <file> <YYYY-MM-DD>
// Files: TERMS_OF_SERVICE.md or PRIVACY_POLICY.md (relative to the repository).
// --record writes only the selected page and ops/release_control/legal_revisions.json.
// Exit codes: 0 success, 1 revision or file error, 2 invalid arguments.
import { createHash } from 'crypto';
import { readFileSync, writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const LEGAL = ['TERMS_OF_SERVICE.md', 'PRIVACY_POLICY.md'];
const RECORD = 'ops/release_control/legal_revisions.json';
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
  'October', 'November', 'December'];
const fail = (msg, code = 1) => { console.error(`legal-revision: ${msg}`); process.exit(code); };

function humanDate(date, name, code = 1) {
  if (typeof date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    fail(`${name}: date must be YYYY-MM-DD`, code);
  }
  const parsed = new Date(`${date}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== date) {
    fail(`${name}: date must be a valid calendar date`, code);
  }
  return `${MONTHS[parsed.getUTCMonth()]} ${parsed.getUTCDate()}, ${date.slice(0, 4)}`;
}

function readPage(name) {
  const bytes = readFileSync(join(ROOT, name));
  // Latin-1 keeps string offsets equal to byte offsets, including non-ASCII text.
  const text = bytes.toString('latin1');
  if (text.includes('release-date placeholder')) fail(`${name}: remove the release-date placeholder`);
  const lines = [...text.matchAll(/^\*\*Last Updated:\*\*[^\r\n]*(?:\r?\n|$)/gm)];
  if (lines.length !== 1) fail(`${name}: expected exactly one Last Updated line`);
  const line = lines[0];
  const before = bytes.subarray(0, line.index);
  const after = bytes.subarray(line.index + line[0].length);
  const body_sha256 = createHash('sha256').update(before).update(after).digest('hex');
  const ending = /\r?\n$/.exec(line[0])?.[0] || '';
  return { bytes, before, after, body_sha256, ending, line: line[0].slice(0, line[0].length - ending.length) };
}

const args = process.argv.slice(2);
if (args.length === 1 && args[0] === '--help') {
  console.log('Usage: node ops/legal-revision.mjs --check | --record <file> <YYYY-MM-DD>');
  process.exit(0);
}
const check = args.length === 1 && args[0] === '--check';
const record = args.length === 3 && args[0] === '--record';
if (!check && !record) fail('use --check or --record <file> <YYYY-MM-DD>', 2);
const [, name, date] = args;
if (record && !LEGAL.includes(name)) fail(`file must be ${LEGAL.join(' or ')}`, 2);
const human = record ? humanDate(date, name, 2) : null;

try {
  const saved = readFileSync(join(ROOT, RECORD), 'utf8');
  const revisions = JSON.parse(saved);
  if (!revisions || typeof revisions !== 'object' || Array.isArray(revisions)) {
    fail(`${RECORD}: expected legal revision records`);
  }
  if (check) {
    for (const name of LEGAL) {
      const page = readPage(name);
      const revision = revisions[name];
      if (!revision) fail(`${name}: missing legal revision record`);
      const expected = humanDate(revision.last_updated, name);
      if (page.body_sha256 !== revision.body_sha256) fail(`${name}: text does not match its recorded revision; use --record after reviewing the change`);
      if (page.line !== `**Last Updated:** ${expected}`) fail(`${name}: Last Updated must be ${expected}`);
    }
    console.log('legal-revision: both legal pages match their recorded revisions');
  } else {
    const page = readPage(name);
    const updated = Buffer.concat([page.before, Buffer.from(`**Last Updated:** ${human}${page.ending}`), page.after]);
    revisions[name] = { last_updated: date, body_sha256: page.body_sha256 };
    const output = JSON.stringify(revisions, null, 2) + '\n';
    if (!updated.equals(page.bytes)) writeFileSync(join(ROOT, name), updated);
    if (output !== saved) writeFileSync(join(ROOT, RECORD), output);
    console.log(`legal-revision: ${name} recorded for ${date}`);
  }
} catch (error) {
  fail(error.message);
}
