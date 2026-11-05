import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { emit, manageDirectory } from './output.mjs'

const root = new URL('./', import.meta.url)
export const foundry = JSON.parse(readFileSync(new URL('themes/foundry/tokens.json', root), 'utf8'))
const kebab = value => value.replace(/[A-Z]/g, letter => `-${letter.toLowerCase()}`)
export function leaves(obj, prefix = []) {
  return Object.entries(obj).flatMap(([key, value]) => typeof value === 'object' ? leaves(value, [...prefix, key]) : [[...prefix, key, value]])
}
const rgb = hex => [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16))

export function generateFoundry() {
  manageDirectory(fileURLToPath(new URL('generated/native/', root)))
  const t = foundry
  const vars = {}
  for (const leaf of leaves(t)) {
    const value = leaf.at(-1), path = leaf.slice(0, -1)
    vars[`--foundry-${path.map(kebab).join('-')}`] = typeof value === 'number' && path.at(-1) !== 'headingWeight' ? `${value}px` : value
  }
  const aliases = {
    'brand-primary': t.action.primary, 'brand-accent': t.action.primary, 'brand-on-primary': t.action.onPrimary,
    'brand-focus-ring': t.action.primary, 'brand-selection-bg': t.action.primary, 'brand-selection-text': t.action.onPrimary,
    'brand-content-bg': t.surface.ground, 'brand-card-bg': t.surface.panel, 'brand-surface': t.surface.raised,
    'brand-card-border': t.surface.border, 'brand-border': t.surface.border,
    'brand-input-bg': t.surface.raised, 'brand-input-border': t.surface.border, 'brand-input-text': t.text.primary,
    'brand-text-primary': t.text.primary, 'brand-text-secondary': t.text.secondary, 'brand-text-muted': t.text.muted,
    'brand-sidebar-bg': t.surface.ground, 'brand-sidebar-border': t.surface.border, 'brand-sidebar-text': t.text.secondary,
    'brand-sidebar-active-bg': t.surface.raised, 'brand-sidebar-active-text': t.action.primary,
    'brand-font-display': t.typography.sans, 'brand-font-body': t.typography.sans, 'brand-font-mono': t.typography.mono,
    'brand-card-shadow': 'none', 'brand-job-card-hover-shadow': 'none',
    'chart-card-bg': t.surface.panel, 'chart-grid': t.surface.border, 'chart-axis': t.text.muted,
    'chart-axis-line': t.surface.border, 'chart-tooltip-bg': t.surface.raised, 'chart-tooltip-border': t.surface.border,
  }
  const statuses = { printing: 'printing', completed: 'ready', failed: 'needsYou', warning: 'waitingOnYou', pending: 'queued', scheduled: 'queued' }
  for (const [key, target] of Object.entries(statuses)) aliases[`status-${key}`] = t.status[target].text
  const farm = { 50: t.text.primary, 100: t.text.primary, 200: t.text.primary, 300: t.text.secondary, 400: t.text.muted, 500: t.text.muted, 600: t.surface.border, 700: t.surface.border, 800: t.surface.raised, 900: t.surface.panel, 950: t.surface.ground }
  for (const [key, color] of Object.entries(farm)) aliases[`farm-${key}`] = rgb(color).join(' ')
  for (const [key, value] of Object.entries(aliases)) vars[`--${key}`] = value
  // Important is intentional: Foundry must win over BrandingContext's inline palette/fonts.
  // Nothing matches Classic or the light/high-contrast fallback.
  const selector = 'html[data-theme-family="foundry"]:not(.light):not(.high-contrast)'
  let css = `/* Generated from design/themes/foundry/tokens.json. Run node design/generate.mjs. */\n${selector} {\n${Object.entries(vars).map(([key, value]) => `  ${key}: ${value} !important;`).join('\n')}\n}\n`
  for (const [key] of Object.entries(t.status)) css += `${selector} .foundry-status[data-status="${key}"] {\n  color: var(--foundry-status-${kebab(key)}-foreground);\n  background: var(--foundry-status-${kebab(key)}-background);\n}\n`
  emit(fileURLToPath(new URL('../frontend/src/foundry-tokens.css', root)), css)

  const colors = leaves(t).filter(leaf => /^#[0-9a-f]{6}$/i.test(leaf.at(-1)))
  const assetRoot = new URL('generated/native/Foundry.xcassets/', root)
  const info = { author: 'xcode', version: 1 }
  emit(fileURLToPath(new URL('Contents.json', assetRoot)), JSON.stringify({ info }, null, 2) + '\n')
  const names = []
  for (const leaf of colors) {
    const hex = leaf.at(-1), path = leaf.slice(0, -1)
    const name = path.map(part => part[0].toUpperCase() + part.slice(1)).join('')
    names.push([path[0] + name.slice(path[0].length), name])
    const [red, green, blue] = rgb(hex).map(value => `0x${value.toString(16).padStart(2, '0').toUpperCase()}`)
    emit(fileURLToPath(new URL(`Foundry${name}.colorset/Contents.json`, assetRoot)), JSON.stringify({ colors: [{ idiom: 'universal', color: { 'color-space': 'srgb', components: { red, green, blue, alpha: '1.000' } } }], info }, null, 2) + '\n')
  }
  const swift = `// Generated from design/themes/foundry/tokens.json. Do not edit.\nimport SwiftUI\n\npublic enum ODINTheme {\n    public enum Foundry {\n        // Pass the bundle containing Foundry.xcassets (app or package).\n        public struct Palette {\n            public let bundle: Bundle\n            public init(bundle: Bundle = .main) { self.bundle = bundle }\n${names.map(([key, name]) => `            public var ${key}: Color { Color("Foundry${name}", bundle: bundle) }`).join('\n')}\n        }\n${['radius', 'spacing'].map(group => `        public enum ${group[0].toUpperCase() + group.slice(1)} {\n${Object.entries(t[group]).map(([key, value]) => `            public static let ${key}: CGFloat = ${value}`).join('\n')}\n        }`).join('\n')}\n        // Phase 3 must bundle the Plex font files in the native target.\n        public enum Typography {\n            public static let display = Font.custom("IBMPlexSans-SemiBold", size: ${t.typography.display}, relativeTo: .largeTitle)\n            public static let title = Font.custom("IBMPlexSans-SemiBold", size: ${t.typography.title}, relativeTo: .title2)\n            public static let body = Font.custom("IBMPlexSans", size: ${t.typography.body}, relativeTo: .body)\n            public static let mono = Font.custom("IBMPlexMono", size: ${t.typography.number}, relativeTo: .body).monospacedDigit()\n            public static let minimum: CGFloat = ${t.typography.minimum}\n        }\n    }\n}\n`
  emit(fileURLToPath(new URL('generated/native/ODINTheme.swift', root)), swift)
}
