import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import ThemeToggle from '../components/layout/ThemeToggle'
import { useTheme } from './useTheme'
import { applyTheme, getFamily, getMode } from './themePreferences'

vi.mock('./serverTheme', () => ({ loadServerTheme: vi.fn(), saveServerMode: vi.fn(), THEME_EVENT: 'odin-theme-change' }))
import { loadServerTheme, saveServerMode } from './serverTheme'
const bootstrap = readFileSync('public/theme-init.js', 'utf8')
beforeEach(() => {
  localStorage.clear(); document.documentElement.className = ''; delete document.documentElement.dataset.themeFamily
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.clearAllMocks()
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('defaults to Classic and applies the root attribute', () => {
  const { result } = renderHook(useTheme)
  expect(result.current.family).toBe('classic'); expect(result.current.theme).toBe('dark')
  expect(document.documentElement.dataset.themeFamily).toBe('classic')
})
it('persists family, updates every mounted consumer, and restores on remount without saving it to the account', () => {
  const first = renderHook(useTheme), second = renderHook(useTheme)
  act(() => first.result.current.setFamily('foundry'))
  expect(second.result.current.family).toBe('foundry')
  expect(document.documentElement.dataset.themeFamily).toBe('foundry')
  expect(localStorage.getItem('odin-theme-family')).toBe('foundry')
  expect(saveServerMode).not.toHaveBeenCalled()
  expect(loadServerTheme).toHaveBeenCalledTimes(2) // Existing mount behavior only.
  first.unmount(); second.unmount()
  expect(renderHook(useTheme).result.current.family).toBe('foundry')
})
it.each(['light', 'high-contrast'] as const)('uses Classic for %s without forgetting Foundry, then restores Foundry in dark', mode => {
  const { result, unmount } = renderHook(useTheme)
  act(() => { result.current.setFamily('foundry'); result.current.setTheme(mode) })
  expect(document.documentElement.dataset.themeFamily).toBe('classic')
  expect(document.documentElement.classList.contains(mode)).toBe(true)
  expect(result.current.family).toBe('foundry')
  unmount()
  const next = renderHook(useTheme)
  expect(next.result.current.theme).toBe(mode)
  act(() => next.result.current.setTheme('dark'))
  expect(document.documentElement.dataset.themeFamily).toBe('foundry')
  expect(document.documentElement.classList.contains(mode)).toBe(false)
})
it('offers both families, three modes and an explicit fallback explanation', () => {
  render(<ThemeToggle />)
  expect(screen.getAllByRole('button')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Theme settings' }))
  fireEvent.change(screen.getByLabelText('Theme family'), { target: { value: 'foundry' } })
  fireEvent.change(screen.getByLabelText('Appearance'), { target: { value: 'high-contrast' } })
  expect(screen.getByText(/Choose Dark to return to Foundry/)).toBeVisible()
  expect(saveServerMode).not.toHaveBeenCalled()
})
it('follows another tab and falls back for invalid stored values', () => {
  localStorage.setItem('odin-theme-family', 'invalid'); localStorage.setItem('odin-theme', 'invalid')
  const { result } = renderHook(useTheme)
  expect(result.current.family).toBe('classic')
  act(() => { localStorage.setItem('odin-theme-family', 'foundry'); window.dispatchEvent(new StorageEvent('storage', { key: 'odin-theme-family' })) })
  expect(result.current.family).toBe('foundry')
})
it.each(['classic', 'foundry', 'invalid'])('bootstrap matches hook resolution before React for family %s in all modes', family => {
  for (const mode of ['dark', 'light', 'high-contrast', 'invalid']) {
    localStorage.setItem('odin-theme-family', family); localStorage.setItem('odin-theme', mode)
    Function(bootstrap)()
    const before = document.documentElement.outerHTML
    applyTheme(getMode(), getFamily())
    expect(document.documentElement.outerHTML).toBe(before)
  }
})
it('bootstrap and hook tolerate denied storage and use the OS preference', () => {
  vi.spyOn(localStorage, 'getItem').mockImplementation(() => { throw new Error('denied') })
  vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('denied') })
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  expect(() => Function(bootstrap)()).not.toThrow()
  const { result } = renderHook(useTheme)
  expect(result.current.theme).toBe('light')
  act(() => { result.current.setFamily('foundry'); result.current.setTheme('dark') })
  expect(document.documentElement.dataset.themeFamily).toBe('foundry')
})
