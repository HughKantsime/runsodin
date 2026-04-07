#!/usr/bin/env node
// odin/design/generate.mjs
// Reads tokens.json → writes design-tokens.css (x2) + ODINTheme.swift
// Run: node design/generate.mjs   (from odin/ root)
//      make tokens

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dir  = dirname(fileURLToPath(import.meta.url))
const tokens = JSON.parse(readFileSync(resolve(__dir, 'tokens.json'), 'utf8'))
const ts     = new Date().toISOString()

// Pass --local-only to skip sibling-repo outputs (for CI inside odin repo)
const LOCAL_ONLY = process.argv.includes('--local-only')

// ─── helpers ─────────────────────────────────────────────────────────────────

function write(absPath, content, { siblingRepo } = {}) {
  if (siblingRepo) {
    // Sibling repo root = two dirs above __dir (odin/ → workspace/)
    const repoRoot = resolve(__dir, '../..', siblingRepo)
    if (LOCAL_ONLY || !existsSync(repoRoot)) {
      console.log('  skipped (not present):', absPath.replace(resolve(__dir, '../..'), ''))
      return
    }
  }
  mkdirSync(dirname(absPath), { recursive: true })
  writeFileSync(absPath, content, 'utf8')
  console.log('  wrote', absPath.replace(resolve(__dir, '../..'), ''))
}

function cssVars(map, indent = '  ') {
  return Object.entries(map)
    .map(([k, v]) => `${indent}${k}: ${v};`)
    .join('\n')
}

// hex "#RRGGBB" → "R G B" (space-separated for Tailwind alpha modifiers)
function hexToRgb(hex) {
  const h = hex.replace('#', '')
  const r = parseInt(h.slice(0,2), 16)
  const g = parseInt(h.slice(2,4), 16)
  const b = parseInt(h.slice(4,6), 16)
  return `${r} ${g} ${b}`
}

// hex → Swift labeled args e.g. "0.769, green: 0.478, blue: 0.102"
// Usage in template: SwiftUI.Color(red: ${hexToSwift(hex)}, opacity: 1)
function hexToSwift(hex) {
  const h = hex.replace('#', '')
  const r = (parseInt(h.slice(0,2), 16) / 255).toFixed(3)
  const g = (parseInt(h.slice(2,4), 16) / 255).toFixed(3)
  const b = (parseInt(h.slice(4,6), 16) / 255).toFixed(3)
  return `${r}, green: ${g}, blue: ${b}`
}

// ─── 1. odin web app: frontend/src/design-tokens.css ─────────────────────────

const webCss = `/* Auto-generated from odin/design/tokens.json — do not edit manually */
/* Source: odin/design/tokens.json  |  Generator: odin/design/generate.mjs */
/* Generated: ${ts} */

/* ── Dark mode defaults (always active) ──────────────────────────────────── */
:root {
${cssVars(tokens.dark)}
}

/* ── Light mode overrides ────────────────────────────────────────────────── */
html.light {
${cssVars(tokens.light)}
}

/* ── High-contrast overrides ─────────────────────────────────────────────── */
.high-contrast {
${cssVars(tokens.highContrast)}
}
`

write(resolve(__dir, '../frontend/src/design-tokens.css'), webCss)

// ─── 2. odin-site: src/design-tokens.css ─────────────────────────────────────

const siteCss = `/* Auto-generated from odin/design/tokens.json — do not edit manually */
/* Source: odin/design/tokens.json  |  Generator: odin/design/generate.mjs */
/* Generated: ${ts} */

/* Tailwind v4 @theme — consumed by odin-site/src/index.css */
@theme {
${cssVars(tokens.siteTheme)}
}
`

write(resolve(__dir, '../../odin-site/src/design-tokens.css'), siteCss, { siblingRepo: 'odin-site' })

// ─── 3. odin-native: ODINTheme.swift ─────────────────────────────────────────

const s = tokens.swift
const c = s.colors
const st = s.status

function swiftColorLine(name, hex, pad) {
  const padded = name.padEnd(pad)
  return `        public static let ${padded}= SwiftUI.Color(red: ${hexToSwift(hex)}, opacity: 1)`
}

function swiftFallbackLine(name, hex, pad) {
  const padded = (name + 'Fallback').padEnd(pad + 8)
  return `    public static var ${padded}= SwiftUI.Color(red: ${hexToSwift(hex)}, opacity: 1)`
}

const colorEntries = Object.entries(c)
const namePad = Math.max(...colorEntries.map(([k]) => k.length)) + 1

const swiftFile = `// ODINTheme.swift — ODIN Design System tokens for SwiftUI
// Auto-generated from odin/design/tokens.json — do not edit manually
// Source: odin/design/tokens.json  |  Generator: odin/design/generate.mjs
// Generated: ${ts}
//
// Usage:
//   .foregroundStyle(ODINTheme.Color.accent)
//   .font(ODINTheme.Font.monoBold)
//   .padding(ODINTheme.Spacing.md)

import SwiftUI

public enum ODINTheme {

    // MARK: - Colors
    // Loads from asset catalog (Assets.xcassets/ODINColors) for dark/light adaptation.
    // Fallback computed values below for tests and Xcode previews.

    public enum Color {
        /// Primary accent — brand amber (${c.accent})
        public static let accent        = SwiftUI.Color("ODINAccent", bundle: .module)
        /// Lighter accent (${c.accentLight})
        public static let accentLight   = SwiftUI.Color("ODINAccentLight", bundle: .module)
        /// Destructive actions — red (${c.destructive})
        public static let destructive   = SwiftUI.Color("ODINDestructive", bundle: .module)
        /// Warning — amber (${c.warning})
        public static let warning       = SwiftUI.Color("ODINWarning", bundle: .module)
        /// Success — green (${c.success})
        public static let success       = SwiftUI.Color("ODINSuccess", bundle: .module)
        /// Elevated surface — card background (${c.surface})
        public static let surface       = SwiftUI.Color("ODINSurface", bundle: .module)
        /// App background (${c.background})
        public static let background    = SwiftUI.Color("ODINBackground", bundle: .module)
        /// Primary text (${c.textPrimary})
        public static let textPrimary   = SwiftUI.Color("ODINTextPrimary", bundle: .module)
        /// Secondary text / muted (${c.textSecondary})
        public static let textSecondary = SwiftUI.Color("ODINTextSecondary", bundle: .module)
        /// Separator / border (${c.separator})
        public static let separator     = SwiftUI.Color("ODINSeparator", bundle: .module)

        // ── Semantic status (computed, no asset catalog entry needed) ──────

        public static func printerStatus(_ status: PrinterStatus) -> SwiftUI.Color {
            switch status {
            case .printing: return success
            case .paused:   return warning
            case .idle:     return textSecondary
            case .error:    return destructive
            case .offline:  return textSecondary.opacity(0.5)
            }
        }

        public static func jobStatus(_ status: JobStatus) -> SwiftUI.Color {
            switch status {
            case .printing:             return success
            case .paused:               return warning
            case .pending, .scheduled:  return accent
            case .completed:            return success.opacity(0.7)
            case .failed:               return destructive
            case .cancelled:            return textSecondary
            }
        }

        public static func alertSeverity(_ severity: AlertSeverity) -> SwiftUI.Color {
            switch severity {
            case .info:     return accent
            case .warning:  return warning
            case .critical: return destructive
            }
        }

        // ── Status palette (matches web --status-* vars) ──────────────────

        public enum Status {
            public static let printing  = SwiftUI.Color(red: ${hexToSwift(st.printing)}, opacity: 1)
            public static let completed = SwiftUI.Color(red: ${hexToSwift(st.completed)}, opacity: 1)
            public static let failed    = SwiftUI.Color(red: ${hexToSwift(st.failed)}, opacity: 1)
            public static let warning   = SwiftUI.Color(red: ${hexToSwift(st.warning)}, opacity: 1)
            public static let pending   = SwiftUI.Color(red: ${hexToSwift(st.pending)}, opacity: 1)
            public static let scheduled = SwiftUI.Color(red: ${hexToSwift(st.scheduled)}, opacity: 1)
        }
    }

    // MARK: - Typography
    // Native apps use system fonts; IBM Plex is web-only.

    public enum Font {
        /// Display headline — large title, monospaced
        public static let display   = SwiftUI.Font.system(.largeTitle, design: .default, weight: .bold)
        /// Section headline
        public static let headline  = SwiftUI.Font.system(.headline, design: .default, weight: .semibold)
        /// Body copy
        public static let body      = SwiftUI.Font.system(.body, design: .default)
        /// Caption / metadata
        public static let caption   = SwiftUI.Font.system(.caption, design: .default)
        /// Monospaced numeric readouts
        public static let mono      = SwiftUI.Font.system(.body, design: .monospaced)
        /// Bold monospaced — large telemetry values
        public static let monoBold  = SwiftUI.Font.system(.title2, design: .monospaced, weight: .bold)
        /// Small monospaced — status bar values
        public static let monoSmall = SwiftUI.Font.system(.caption, design: .monospaced)
    }

    // MARK: - Spacing

    public enum Spacing {
${Object.entries(s.spacing).map(([k, v]) => `        public static let ${k.padEnd(3)}: CGFloat = ${v}`).join('\n')}
    }

    // MARK: - Corner Radius

    public enum Radius {
${Object.entries(s.radius).map(([k, v]) => `        public static let ${k.padEnd(4)}: CGFloat = ${v}`).join('\n')}
    }
}

// MARK: - Asset Catalog Fallbacks
// Used in unit tests and Xcode previews where the bundle asset catalog
// may not be loaded. Values match the dark-mode defaults in tokens.json.

public extension ODINTheme.Color {
    static var accentFallback:        SwiftUI.Color { SwiftUI.Color(red: ${hexToSwift(c.accent)}, opacity: 1) }
    static var accentLightFallback:   SwiftUI.Color { SwiftUI.Color(red: ${hexToSwift(c.accentLight)}, opacity: 1) }
    static var destructiveFallback:   SwiftUI.Color { SwiftUI.Color(red: ${hexToSwift(c.destructive)}, opacity: 1) }
    static var warningFallback:       SwiftUI.Color { SwiftUI.Color(red: ${hexToSwift(c.warning)}, opacity: 1) }
    static var successFallback:       SwiftUI.Color { SwiftUI.Color(red: ${hexToSwift(c.success)}, opacity: 1) }
    static var surfaceFallback:       SwiftUI.Color { SwiftUI.Color(red: ${hexToSwift(c.surface)}, opacity: 1) }
    static var backgroundFallback:    SwiftUI.Color { SwiftUI.Color(red: ${hexToSwift(c.background)}, opacity: 1) }
    static var textPrimaryFallback:   SwiftUI.Color { SwiftUI.Color(red: ${hexToSwift(c.textPrimary)}, opacity: 1) }
    static var textSecondaryFallback: SwiftUI.Color { SwiftUI.Color(red: ${hexToSwift(c.textSecondary)}, opacity: 1) }
    static var separatorFallback:     SwiftUI.Color { SwiftUI.Color(red: ${hexToSwift(c.separator)}, opacity: 1) }
}
`

write(
  resolve(__dir, '../../odin-native/ODINCore/Sources/ODINCore/DesignSystem/ODINTheme.swift'),
  swiftFile,
  { siblingRepo: 'odin-native' }
)

console.log(`\nDone. Generated from tokens.json at ${ts}`)
