import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import postcss from 'postcss'
import { classicSource, digest } from '../../../../tests/foundry_browser/classic-preservation.mjs'
import baseline from '../../../../tests/foundry_browser/classic-baseline.json'
import routes from '../../../../tests/foundry_browser/routes.json'

const root = path.resolve(import.meta.dirname, '../../../..')
const source = fs.readFileSync(path.join(root, 'frontend/src/App.tsx'), 'utf8')
const read = (file: string) => fs.readFileSync(path.join(root, file), 'utf8')

it('covers every router entry, standalone route, redirect and catch-all in screenshots', () => {
  const declared = [...new Set([
    ...[...source.matchAll(/<Route path="([^"]+)"/g)].map(m => m[1]),
    ...[...source.matchAll(/location.pathname === '([^']+)'/g)].map(m => m[1]),
  ])].sort()
  expect(routes.map(r => r.route).sort()).toEqual(declared)
  expect(routes).toHaveLength(37)
  for (const { route } of routes) expect(source).toContain(`<FoundryPage name="${route}">`)
  expect(routes.filter(r => r.finalPath !== r.url).map(r => r.route)).toEqual(['/admin', '/permissions', '/branding'])
})

describe('Classic source preservation against Part A commit 3d372ba2', () => {
  it.each(Object.entries(baseline.files))('%s preserves original handlers, data, classes, styles and branches', (file, hash) => {
    expect(digest(classicSource(file, read(file)))).toBe(hash)
  })
})

it.each(['frontend/src/foundry.css', 'frontend/src/foundry-pages.css'])('%s cannot style Classic, light or high contrast', file => {
  const css = postcss.parse(read(file))
  for (const rule of css.nodes.filter(n => n.type !== 'comment')) {
    expect(rule.type).toBe('rule')
    expect((rule as postcss.Rule).selector).toBe('html[data-theme-family="foundry"]:not(.light):not(.high-contrast)')
  }
})

it('uses the required primitive radii, minimum type, mono numbers and phone layouts', () => {
  const css = read('frontend/src/foundry-pages.css')
  expect(css).toMatch(/\[data-foundry-ui="card"\][^{]*\{[^}]*border-radius: 12px/)
  expect(css).toMatch(/\[data-foundry-ui="button"\][^{]*\{[^}]*border-radius: 8px/)
  expect(css).toContain('font-size: 13px !important')
  expect(css).toContain("font-family: 'IBM Plex Mono', monospace; font-variant-numeric: tabular-nums")
  expect(css).toContain('min-height: 44px; min-width: 44px')
  expect(css).toContain('.f-detail-stack .f-slots { grid-template-columns: repeat(2, minmax(0, 1fr))')
  expect(read('frontend/src/foundry.css')).toContain('.f-fleet[data-density="compact"] > .f-comfortable { display: grid; }')
})
