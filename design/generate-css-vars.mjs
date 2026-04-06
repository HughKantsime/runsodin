#!/usr/bin/env node
// generate-css-vars.mjs — Reads design/tokens.json and outputs:
//   1. design/generated-theme.css  (CSS custom properties)
//   2. design/generated-tailwind-theme.mjs (JS export for Tailwind config)

import { readFileSync, writeFileSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const tokens = JSON.parse(readFileSync(join(__dirname, 'tokens.json'), 'utf-8'));

// --- CSS custom properties ---
const cssLines = [':root {'];

function flattenToVars(obj, prefix = '') {
  for (const [key, value] of Object.entries(obj)) {
    if (key === '$schema') continue;
    const varName = prefix ? `${prefix}-${key}` : key;
    if (typeof value === 'object' && !Array.isArray(value)) {
      flattenToVars(value, varName);
    } else {
      cssLines.push(`  --${varName}: ${value};`);
    }
  }
}

flattenToVars(tokens);
cssLines.push('}', '');

writeFileSync(join(__dirname, 'generated-theme.css'), cssLines.join('\n'));
console.log('✓ generated-theme.css');

// --- Tailwind theme export ---
const twLines = [
  '// Auto-generated from tokens.json — do not edit manually',
  `// Generated: ${new Date().toISOString()}`,
  '',
  `const tokens = ${JSON.stringify(tokens, null, 2)};`,
  '',
  'export default tokens;',
  '',
];

writeFileSync(join(__dirname, 'generated-tailwind-theme.mjs'), twLines.join('\n'));
console.log('✓ generated-tailwind-theme.mjs');
