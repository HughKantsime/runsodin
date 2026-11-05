#!/usr/bin/env node
// Generate Classic CSS, scoped Foundry CSS and local native assets.
// Run: node design/generate.mjs            (all outputs)
//      node design/generate.mjs --check       (read-only stale-output check)
//      make tokens

import { readFileSync } from 'fs'
import { emit, finish } from './output.mjs'
import { generateFoundry } from './generate-foundry.mjs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dir = dirname(fileURLToPath(import.meta.url))
const tokens = JSON.parse(readFileSync(resolve(__dir, 'tokens.json'), 'utf8'))
// --local-only remains accepted for existing callers; all outputs now stay in this repo.

// ─── Explicit mapping: token path → CSS variable name ────────────────────────
// This is intentionally explicit. No magic flattening — every mapping is visible.

function get(obj, path) {
  return path.split('.').reduce((o, k) => o?.[k], obj)
}

// Dark mode: reads from top-level token keys
const DARK_MAP = {
  // Brand
  '--brand-primary':             'brand.primary',
  '--brand-accent':              'brand.accent',
  '--brand-on-primary':          'brand.onPrimary',
  // Sidebar
  '--brand-sidebar-bg':          'sidebar.dark.bg',
  '--brand-sidebar-border':      'sidebar.dark.border',
  '--brand-sidebar-text':        'sidebar.dark.text',
  '--brand-sidebar-active-bg':   'sidebar.dark.activeBg',
  '--brand-sidebar-active-text': 'sidebar.dark.activeText',
  // Surface
  '--brand-content-bg':          'surface.dark.bg',
  '--brand-card-bg':             'surface.dark.card',
  '--brand-card-border':         'surface.dark.border',
  '--brand-text-primary':        'text.dark.primary',
  '--brand-text-secondary':      'text.dark.secondary',
  '--brand-text-muted':          'text.dark.muted',
  '--brand-surface':             'surface.dark.surface',
  '--brand-input-bg':            'surface.dark.inputBg',
  '--brand-input-border':        'surface.dark.inputBorder',
  '--brand-input-text':          'text.dark.primary',  // alias
  // Chart
  '--chart-card-bg':             'chart.cardBg',
  '--chart-grid':                'chart.grid',
  '--chart-axis':                'chart.axis',
  '--chart-axis-line':           'chart.axisLine',
  '--chart-tooltip-bg':          'chart.tooltipBg',
  '--chart-tooltip-border':      'chart.tooltipBorder',
  '--chart-tooltip-shadow':      'chart.tooltipShadow',
  // Fonts
  '--brand-font-display':        'typography.fontDisplay',
  '--brand-font-body':           'typography.fontBody',
  '--brand-font-mono':           'typography.fontMono',
  // Chrome
  '--brand-focus-ring':          'brand.focusRing',
  '--brand-selection-bg':        'brand.selectionBg',
  '--brand-selection-text':      'brand.selectionText',
  '--brand-card-shadow':         'shadow.card',
  '--brand-job-card-hover-shadow': 'shadow.jobCardHover',
}

// Farm palette (space-separated RGB, read from top-level farm.*)
const FARM_SHADES = ['50','100','200','300','400','500','600','700','800','900','950']

// Status colors (read from top-level status.*)
const STATUS_KEYS = ['printing','completed','failed','warning','pending','scheduled']

// Mode override mapping: token path within modes.X → CSS var name
// These use the SAME var names as dark mode but read from modes.light / modes.highContrast
const MODE_MAP = {
  // Brand
  'brand.primary':       '--brand-primary',
  'brand.accent':        '--brand-accent',
  'brand.onPrimary':     '--brand-on-primary',
  'brand.focusRing':     '--brand-focus-ring',
  'brand.selectionBg':   '--brand-selection-bg',
  'brand.selectionText': '--brand-selection-text',
  // Surface (no .dark. segment in mode overrides)
  'surface.bg':          '--brand-content-bg',
  'surface.card':        '--brand-card-bg',
  'surface.surface':     '--brand-surface',
  'surface.border':      '--brand-card-border',
  'surface.inputBg':     '--brand-input-bg',
  'surface.inputBorder': '--brand-input-border',
  // Text
  'text.primary':        '--brand-text-primary',
  'text.secondary':      '--brand-text-secondary',
  'text.muted':          '--brand-text-muted',
  'text.input':          '--brand-input-text',
  // Sidebar
  'sidebar.bg':          '--brand-sidebar-bg',
  'sidebar.border':      '--brand-sidebar-border',
  'sidebar.text':        '--brand-sidebar-text',
  'sidebar.activeBg':    '--brand-sidebar-active-bg',
  'sidebar.activeText':  '--brand-sidebar-active-text',
  // Chart
  'chart.cardBg':        '--chart-card-bg',
  'chart.grid':          '--chart-grid',
  'chart.axis':          '--chart-axis',
  'chart.axisLine':      '--chart-axis-line',
  'chart.tooltipBg':     '--chart-tooltip-bg',
  'chart.tooltipBorder': '--chart-tooltip-border',
  'chart.tooltipShadow': '--chart-tooltip-shadow',
  // Shadow
  'shadow.card':         '--brand-card-shadow',
  'shadow.jobCardHover': '--brand-job-card-hover-shadow',
  // Typography
  'typography.fontDisplay': '--brand-font-display',
  'typography.fontBody':    '--brand-font-body',
  'typography.fontMono':    '--brand-font-mono',
}

// ─── Build CSS blocks ────────────────────────────────────────────────────────

function buildDarkBlock() {
  const lines = []
  const missing = []

  for (const [varName, path] of Object.entries(DARK_MAP)) {
    const val = get(tokens, path)
    if (val === undefined) { missing.push(`${varName} ← ${path}`); continue }
    lines.push(`  ${varName}: ${val};`)
  }
  // Farm
  for (const shade of FARM_SHADES) {
    const val = get(tokens, `farm.${shade}`)
    if (val === undefined) { missing.push(`--farm-${shade} ← farm.${shade}`); continue }
    lines.push(`  --farm-${shade}: ${val};`)
  }
  // Status
  for (const key of STATUS_KEYS) {
    const val = get(tokens, `status.${key}`)
    if (val === undefined) { missing.push(`--status-${key} ← status.${key}`); continue }
    lines.push(`  --status-${key}: ${val};`)
  }

  if (missing.length) {
    console.error('ERROR: Missing token paths for dark mode:')
    missing.forEach(m => console.error(`  ${m}`))
    process.exit(1)
  }

  return lines.join('\n')
}

function buildModeBlock(modeName) {
  const mode = get(tokens, `modes.${modeName}`)
  if (!mode) {
    console.error(`ERROR: modes.${modeName} not found in tokens.json`)
    process.exit(1)
  }

  const lines = []

  // Walk MODE_MAP and emit only vars that have overrides in this mode
  for (const [path, varName] of Object.entries(MODE_MAP)) {
    const val = get(mode, path)
    if (val !== undefined) lines.push(`  ${varName}: ${val};`)
  }
  // Farm overrides
  if (mode.farm) {
    for (const shade of FARM_SHADES) {
      if (mode.farm[shade] !== undefined) lines.push(`  --farm-${shade}: ${mode.farm[shade]};`)
    }
  }
  // Status overrides
  if (mode.status) {
    for (const key of STATUS_KEYS) {
      if (mode.status[key] !== undefined) lines.push(`  --status-${key}: ${mode.status[key]};`)
    }
  }

  return lines.join('\n')
}

// ─── Generate CSS ────────────────────────────────────────────────────────────

const css = `/* Auto-generated from design/tokens.json — do not edit manually */
/* Generator: design/generate.mjs | Run 'make tokens' to regenerate */

/* ── Dark mode defaults ──────────────────────────────────────────────────── */
:root {
${buildDarkBlock()}
}

/* ── High-contrast overrides (shop floor monitors, TV dashboards) ─────── */
.high-contrast {
${buildModeBlock('highContrast')}
}

/* ── Light mode overrides ────────────────────────────────────────────────── */
html.light {
${buildModeBlock('light')}
}
`

const cssPath = resolve(__dir, '../frontend/src/design-tokens.css')
emit(cssPath, css)
generateFoundry()
finish()
