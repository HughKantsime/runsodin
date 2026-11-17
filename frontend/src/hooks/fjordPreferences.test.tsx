import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
// @ts-ignore -- Vitest provides Node; the browser tsconfig has no Node types.
import { readFileSync } from 'node:fs'
import { useTheme } from './useTheme'
import { applyTheme, getFamily, getMode, useFoundry } from './themePreferences'
import ThemeToggle from '../components/layout/ThemeToggle'
import ThemeSettings from '../components/admin/ThemeSettings'
import StatusBadge from '../components/ui/StatusBadge'
import { applyUserAccent } from './serverTheme'
import { applyBrandingCSS } from '../BrandingContext'

vi.mock('./serverTheme', async importOriginal => ({
  ...await importOriginal<typeof import('./serverTheme')>(), loadServerTheme: vi.fn(), saveServerMode: vi.fn(),
}))
import { saveServerMode } from './serverTheme'
const bootstrap = readFileSync('public/theme-init.js', 'utf8')
beforeEach(() => {
  localStorage.clear()
  document.documentElement.className = ''
  document.documentElement.removeAttribute('style')
  delete document.documentElement.dataset.themeFamily
  delete document.documentElement.dataset.fjordDensity
  delete document.documentElement.dataset.userAccent
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.clearAllMocks()
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('round-trips Fjord locally, synchronizes consumers, and keeps the family when toggling light/dark', () => {
  const first = renderHook(useTheme), second = renderHook(useTheme)
  act(() => first.result.current.setFamily('fjord'))
  expect(localStorage.getItem('odin-theme-family')).toBe('fjord')
  expect(second.result.current.family).toBe('fjord')
  expect(saveServerMode).not.toHaveBeenCalled()
  act(() => first.result.current.toggleTheme())
  expect(second.result.current.theme).toBe('light')
  expect(second.result.current.effectiveFamily).toBe('fjord')
  act(() => second.result.current.toggleTheme())
  expect(first.result.current.effectiveFamily).toBe('fjord')
  first.unmount(); second.unmount()
  expect(renderHook(useTheme).result.current.family).toBe('fjord')
})
it.each(['dark', 'light', 'high-contrast'] as const)('resolves Fjord in %s identically before first paint and in React', mode => {
  localStorage.setItem('odin-theme-family', 'fjord'); localStorage.setItem('odin-theme', mode)
  localStorage.setItem('odin-foundry-density', 'compact')
  Function(bootstrap)()
  const before = (document.documentElement.cloneNode(false) as HTMLElement).outerHTML
  const { result } = renderHook(useTheme)
  expect((document.documentElement.cloneNode(false) as HTMLElement).outerHTML).toBe(before)
  expect(result.current.effectiveFamily).toBe(mode === 'high-contrast' ? 'classic' : 'fjord')
  expect(result.current.family).toBe('fjord')
  expect(renderHook(useFoundry).result.current).toBe(false)
})
it('restores Fjord after high contrast and follows family and density changes from another tab', () => {
  const { result } = renderHook(useTheme)
  act(() => { result.current.setFamily('fjord'); result.current.setTheme('high-contrast') })
  expect(result.current.effectiveFamily).toBe('classic')
  act(() => result.current.setTheme('light'))
  expect(result.current.effectiveFamily).toBe('fjord')
  act(() => { localStorage.setItem('odin-foundry-density', 'compact'); window.dispatchEvent(new StorageEvent('storage', { key: 'odin-foundry-density' })) })
  expect(document.documentElement.dataset.fjordDensity).toBe('compact')
  act(() => { localStorage.setItem('odin-theme-family', 'unknown'); window.dispatchEvent(new StorageEvent('storage', { key: 'odin-theme-family' })) })
  expect(result.current.family).toBe('classic')
  expect(document.documentElement.dataset.fjordDensity).toBeUndefined()
})
it('unknown stored family falls back to Classic in the bootstrap and preferences', () => {
  localStorage.setItem('odin-theme-family', 'unknown')
  Function(bootstrap)()
  expect(document.documentElement.dataset.themeFamily).toBe('classic')
  expect(getFamily()).toBe('classic')
  applyTheme(getMode(), getFamily())
  expect(document.documentElement.dataset.themeFamily).toBe('classic')
})
it.each([ThemeToggle, ThemeSettings])('offers Fjord, its two previews, density and high-contrast explanation', Component => {
  render(<Component />)
  const toggle = screen.queryByRole('button', { name: 'Theme settings' })
  if (toggle) fireEvent.click(toggle)
  fireEvent.change(screen.getByLabelText('Theme family'), { target: { value: 'fjord' } })
  expect(screen.getByRole('img', { name: 'Fjord preview: light and dark' })).toBeVisible()
  fireEvent.change(screen.getByLabelText('Density'), { target: { value: 'compact' } })
  expect(document.documentElement.dataset.fjordDensity).toBe('compact')
  fireEvent.change(screen.getByLabelText('Appearance'), { target: { value: 'high-contrast' } })
  expect(screen.getByRole('status')).toHaveTextContent('Using Classic high contrast')
  fireEvent.change(screen.getByLabelText('Appearance'), { target: { value: 'light' } })
  expect(document.documentElement.dataset.themeFamily).toBe('fjord')
})
it('uses Classic badge markup and wording in Fjord', () => {
  applyTheme('light', 'fjord')
  const { container } = render(<StatusBadge status="printing" variant="badge" />)
  expect(container.firstChild).toHaveClass('bg-[var(--status-printing)]/8')
  expect(screen.getByText('printing')).toBeVisible()
  expect(container.querySelector('.foundry-status')).toBeNull()
})
it.each(['light', 'dark'] as const)('keeps personal accent and operator brand overrides in %s', mode => {
  applyTheme(mode, 'fjord')
  applyUserAccent({ accent_color: '#123456' })
  applyBrandingCSS({ primary_color: '#345678', accent_color: '#654321', font_body: 'Inter', font_display: 'Inter', font_mono: 'monospace' })
  expect(document.documentElement.style.getPropertyValue('--brand-accent')).toBe('#123456')
  expect(document.documentElement.style.getPropertyValue('--brand-primary')).toBe('#345678')
  expect(document.documentElement.style.getPropertyValue('--brand-font-mono')).toBe('monospace')
})
