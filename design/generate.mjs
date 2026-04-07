#!/usr/bin/env node
// design/generate.mjs — Reads tokens.json, writes design-tokens.css (+ ODINTheme.swift if sibling exists)
// Run: node design/generate.mjs            (all outputs)
//      node design/generate.mjs --local-only  (CSS only, skip sibling repos)
//      make tokens

import { readFileSync, writeFileSync, existsSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dir = dirname(fileURLToPath(import.meta.url))
const tokens = JSON.parse(readFileSync(resolve(__dir, 'tokens.json'), 'utf8'))
const LOCAL_ONLY = process.argv.includes('--local-only')

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
writeFileSync(cssPath, css, 'utf8')
console.log(`  wrote frontend/src/design-tokens.css (${css.split('\n').length} lines)`)

// ─── ODINTheme.swift (odin-native sibling) ───────────────────────────────────

const nativeDir = resolve(__dir, '../../odin-native/ODINCore/Sources/ODINCore/DesignSystem')
if (!LOCAL_ONLY && existsSync(nativeDir)) {
  function hexToSwift(hex) {
    const h = hex.replace('#', '')
    const r = (parseInt(h.slice(0,2), 16) / 255).toFixed(3)
    const g = (parseInt(h.slice(2,4), 16) / 255).toFixed(3)
    const b = (parseInt(h.slice(4,6), 16) / 255).toFixed(3)
    return `${r}, green: ${g}, blue: ${b}`
  }

  const c = {
    accent: tokens.brand.primary,
    accentLight: tokens.brand.accent,
    destructive: tokens.status.failed,
    warning: tokens.status.warning,
    success: tokens.status.completed,
    surface: tokens.surface.dark.surface,
    background: tokens.surface.dark.bg,
    textPrimary: tokens.text.dark.primary,
    textSecondary: tokens.text.dark.secondary,
    separator: tokens.surface.dark.border,
  }

  const st = tokens.status

  const swift = `// ODINTheme.swift — ODIN Design System tokens for SwiftUI
// Auto-generated from odin/design/tokens.json — do not edit manually
// Generator: odin/design/generate.mjs | Run 'make tokens' to regenerate

import SwiftUI

public enum ODINTheme {

    // MARK: - Colors
    public enum Color {
        public static let accent        = SwiftUI.Color("ODINAccent", bundle: .module)
        public static let accentLight   = SwiftUI.Color("ODINAccentLight", bundle: .module)
        public static let destructive   = SwiftUI.Color("ODINDestructive", bundle: .module)
        public static let warning       = SwiftUI.Color("ODINWarning", bundle: .module)
        public static let success       = SwiftUI.Color("ODINSuccess", bundle: .module)
        public static let surface       = SwiftUI.Color("ODINSurface", bundle: .module)
        public static let background    = SwiftUI.Color("ODINBackground", bundle: .module)
        public static let textPrimary   = SwiftUI.Color("ODINTextPrimary", bundle: .module)
        public static let textSecondary = SwiftUI.Color("ODINTextSecondary", bundle: .module)
        public static let separator     = SwiftUI.Color("ODINSeparator", bundle: .module)

        public enum Status {
            public static let printing  = SwiftUI.Color(red: ${hexToSwift(st.printing)}, opacity: 1)
            public static let completed = SwiftUI.Color(red: ${hexToSwift(st.completed)}, opacity: 1)
            public static let failed    = SwiftUI.Color(red: ${hexToSwift(st.failed)}, opacity: 1)
            public static let warning   = SwiftUI.Color(red: ${hexToSwift(st.warning)}, opacity: 1)
            public static let pending   = SwiftUI.Color(red: ${hexToSwift(st.pending)}, opacity: 1)
            public static let scheduled = SwiftUI.Color(red: ${hexToSwift(st.scheduled)}, opacity: 1)
        }
    }

    // MARK: - Typography (system fonts — IBM Plex is web-only)
    public enum Font {
        public static let display   = SwiftUI.Font.system(.largeTitle, design: .default, weight: .bold)
        public static let headline  = SwiftUI.Font.system(.headline, design: .default, weight: .semibold)
        public static let body      = SwiftUI.Font.system(.body, design: .default)
        public static let caption   = SwiftUI.Font.system(.caption, design: .default)
        public static let mono      = SwiftUI.Font.system(.body, design: .monospaced)
        public static let monoBold  = SwiftUI.Font.system(.title2, design: .monospaced, weight: .bold)
        public static let monoSmall = SwiftUI.Font.system(.caption, design: .monospaced)
    }

    // MARK: - Spacing
    public enum Spacing {
        public static let xs:  CGFloat = 4
        public static let sm:  CGFloat = 8
        public static let md:  CGFloat = 12
        public static let lg:  CGFloat = 16
        public static let xl:  CGFloat = 24
        public static let xxl: CGFloat = 32
    }

    // MARK: - Corner Radius
    public enum Radius {
        public static let sm:   CGFloat = 8
        public static let md:   CGFloat = 12
        public static let lg:   CGFloat = 16
        public static let pill: CGFloat = 999
    }
}

// MARK: - Asset Catalog Fallbacks (tests + Xcode previews)
public extension ODINTheme.Color {
${Object.entries(c).map(([name, hex]) => {
    if (!hex.startsWith('#')) return `    // ${name}: non-hex value, skipped`
    const padded = (name + 'Fallback').padEnd(22)
    return `    static var ${padded}: SwiftUI.Color { SwiftUI.Color(red: ${hexToSwift(hex)}, opacity: 1) }`
  }).join('\n')}
}
`

  const swiftPath = resolve(nativeDir, 'ODINTheme.swift')
  writeFileSync(swiftPath, swift, 'utf8')
  console.log(`  wrote odin-native ODINTheme.swift`)
} else if (!LOCAL_ONLY) {
  console.log('  skipped odin-native (not found)')
}

console.log('\nDone. Generated from tokens.json')
