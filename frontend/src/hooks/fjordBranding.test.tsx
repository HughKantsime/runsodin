import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { BrandingProvider, useBranding, applyBrandingCSS, brandingColors } from '../BrandingContext'
import { applyTheme } from './themePreferences'
import { applyUserAccent } from './serverTheme'
import ThemePicker from '../components/layout/ThemePicker'
// @ts-ignore -- node is available in Vitest
import { readFileSync } from 'node:fs'
import postcss from 'postcss'
import { contrastRatio } from '../utils/contrast'
vi.mock('./serverTheme', async original => ({ ...await original<typeof import('./serverTheme')>(), loadServerTheme: vi.fn(), saveServerMode: vi.fn() }))
const root = document.documentElement
let branding: ReturnType<typeof useBranding>
function Probe() { branding = useBranding(); return <ThemePicker /> }
function tokens(mode: string) {
  const sheet = postcss.parse(readFileSync('src/fjord-tokens.css', 'utf8'))
  const rule = sheet.nodes.find(n => n.type === 'rule' && n.selector.includes('.light') === (mode === 'light')) as postcss.Rule
  const v = Object.fromEntries(rule.nodes.filter(n => n.type === 'decl').map(n => [n.prop, n]))
  const resolve = (key: string): string => (v[key].important ? v[key].value : root.style.getPropertyValue(key) || v[key].value).replace(/var\((--[\w-]+)\)/g, (_, k) => resolve(k))
  return resolve
}
beforeEach(() => { localStorage.clear(); root.className = ''; root.removeAttribute('style'); delete root.dataset.userAccent; delete root.dataset.themeFamily })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); root.removeAttribute('style'); delete root.dataset.userAccent })
it.each(['light', 'dark'].flatMap(mode => ['stock', 'operator', 'user'].map(kind => [mode, kind])))('renders and previews %s %s colors', async (mode, kind) => {
  localStorage.setItem('odin-theme-family', 'fjord'); localStorage.setItem('odin-theme', mode)
  const custom = kind === 'operator' ? { primary_color: mode === 'light' ? '#345678' : '#6099cf', accent_color: '#abcdef' } : {}
  if (kind === 'user') applyUserAccent({ accent_color: '#123456' })
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => custom })))
  render(<BrandingProvider><Probe /></BrandingProvider>)
  await waitFor(() => expect(branding.brandingResolved).toBe(true))
  const v = tokens(mode)
  if (kind === 'stock') for (const key of ['--brand-primary', '--brand-accent', '--brand-sidebar-active-text']) expect(root.style.getPropertyValue(key), key).toBe('')
  if (kind === 'operator') {
    expect(root.style.getPropertyValue('--brand-primary')).toBe(custom.primary_color)
    expect(root.style.getPropertyValue('--brand-accent')).toBe(custom.accent_color)
  }
  if (kind === 'user') expect(root.style.getPropertyValue('--brand-accent')).toBe('#123456')
  for (const [fg, bg] of [['--brand-on-primary','--brand-primary'], ['--fjord-on-accent','--brand-accent'], ['--brand-sidebar-active-text','--brand-sidebar-active-bg']]) expect(contrastRatio(v(fg), v(bg))).toBeGreaterThanOrEqual(4.5)
  expect(contrastRatio(v('--brand-primary'), v('--brand-card-bg'))).toBeGreaterThanOrEqual(3)
  expect(screen.getByLabelText(`Fjord ${mode} primary`)).toHaveStyle({ background: v('--brand-primary'), color: v('--brand-on-primary') })
  expect(screen.getByLabelText(`Fjord ${mode} accent`)).toHaveStyle({ background: v('--brand-accent') })
})
it.each(['light', 'dark'] as const)('personal accent wins over operator branding and clears back to Fjord in %s', async mode => {
  localStorage.setItem('odin-theme-family', 'fjord'); localStorage.setItem('odin-theme', mode)
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({}) })))
  render(<BrandingProvider><Probe /></BrandingProvider>)
  await waitFor(() => expect(branding.brandingResolved).toBe(true))
  applyUserAccent({ accent_color: '#123456' })
  applyBrandingCSS({ ...branding, accent_color: '#abcdef' })
  expect(root.style.getPropertyValue('--brand-accent')).toBe('#123456')
  await waitFor(() => expect(screen.getByLabelText(`Fjord ${mode} accent`)).toHaveStyle({ background: '#123456' }))
  applyUserAccent(null)
  await waitFor(() => expect(root.style.getPropertyValue('--brand-accent')).toBe(''))
  expect(screen.getByLabelText(`Fjord ${mode} accent`)).toHaveStyle({ background: tokens(mode)('--brand-accent') })
})
it('releases stock inline colors on a family-only switch and restores Classic on exit', async () => {
  applyTheme('dark', 'classic')
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({}) })))
  render(<BrandingProvider><Probe /></BrandingProvider>)
  await waitFor(() => expect(root.style.getPropertyValue('--brand-primary')).toBe('#dc9a3e'))
  applyTheme('dark', 'fjord')
  await waitFor(() => expect(root.style.getPropertyValue('--brand-primary')).toBe(''))
  applyTheme('dark', 'classic')
  await waitFor(() => expect(root.style.getPropertyValue('--brand-primary')).toBe('#dc9a3e'))
})

it.each(['#777777', '#888', '#123456', '#abcdef'])('custom brand fills retain 4.5:1 at %s', fill => {
  const colors = brandingColors({ primary_color: fill, accent_color: fill }, false, true)
  const bg = fill.replace(/^#([a-f\d])([a-f\d])([a-f\d])$/i, '#$1$1$2$2$3$3')
  for (const text of [colors['--brand-on-primary'], colors['--fjord-on-accent']]) expect(contrastRatio(text!, bg)).toBeGreaterThanOrEqual(4.5)
})
