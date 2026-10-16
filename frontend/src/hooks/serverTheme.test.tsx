// F16-16.1-03 / 16.1-04: the real ThemeToggle + useTheme consume and update the
// per-user server theme. Only the network boundary (fetch) is stubbed; the
// backend half is pinned by tests/test_contracts/test_cert_G_ux_backend.py.
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import ThemeToggle from '../components/layout/ThemeToggle'
import { resetServerThemeForTests } from './serverTheme'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
let serverTheme: Record<string, unknown>
let transport: ReturnType<typeof vi.fn>

beforeEach(() => {
  resetServerThemeForTests()
  localStorage.clear()
  const root = document.documentElement
  root.classList.remove('light'); root.style.removeProperty('--brand-accent'); delete root.dataset.userAccent
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  transport = vi.fn(async (url: string, init?: RequestInit) => {
    if (url !== '/api/auth/me/theme') return json({ detail: 'unexpected' }, 500)
    if (init?.method === 'PUT') { serverTheme = JSON.parse(init.body as string); return json(serverTheme) }
    return json(serverTheme)
  })
  vi.stubGlobal('fetch', transport)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const accent = () => document.documentElement.style.getPropertyValue('--brand-accent')

it('applies the account theme saved on another device (mode and accent)', async () => {
  serverTheme = { accent_color: '#ff0000', sidebar_style: 'light', background: 'default', mode: 'light' }
  render(<ThemeToggle />)
  await screen.findByRole('button', { name: 'Switch to dark mode' })
  expect(document.documentElement.classList.contains('light')).toBe(true)
  expect(localStorage.getItem('odin-theme')).toBe('light')
  expect(accent()).toBe('#ff0000')
  expect(document.documentElement.dataset.userAccent).toBe('#ff0000')
})

it('the server default accent is not a personal override; invalid accents are never applied', async () => {
  serverTheme = { accent_color: '#6366f1', sidebar_style: 'dark', background: 'default' }
  render(<ThemeToggle />)
  await waitFor(() => expect(transport).toHaveBeenCalled())
  await screen.findByRole('button', { name: 'Switch to light mode' })
  expect(accent()).toBe('')
  cleanup(); resetServerThemeForTests()
  serverTheme = { accent_color: 'red;background:url(x)' }
  render(<ThemeToggle />)
  await waitFor(() => expect(transport).toHaveBeenCalledTimes(2))
  expect(accent()).toBe('')
})

it('a toggle is saved to the account, keeping the other stored preferences', async () => {
  serverTheme = { accent_color: '#0ea5e9', sidebar_style: 'light', background: 'default' }
  render(<ThemeToggle />)
  await waitFor(() => expect(transport).toHaveBeenCalledTimes(1))
  fireEvent.click(screen.getByRole('button', { name: 'Switch to light mode' }))
  await waitFor(() => expect(transport).toHaveBeenCalledTimes(2))
  const [url, init] = transport.mock.calls[1]
  expect(url).toBe('/api/auth/me/theme')
  expect(init.method).toBe('PUT')
  expect(JSON.parse(init.body)).toEqual({ accent_color: '#0ea5e9', sidebar_style: 'light', background: 'default', mode: 'light' })
  expect(document.documentElement.classList.contains('light')).toBe(true)
})

it('a toggle made while the account theme is still loading is not overridden by it', async () => {
  serverTheme = { accent_color: '#6366f1', sidebar_style: 'dark', background: 'default', mode: 'dark' }
  let release: () => void = () => {}
  const gate = new Promise<void>(r => { release = r })
  const inner = transport.getMockImplementation()!
  transport.mockImplementation(async (url: string, init?: RequestInit) => {
    if (!init?.method || init.method === 'GET') await gate
    return inner(url, init)
  })
  render(<ThemeToggle />)
  fireEvent.click(screen.getByRole('button', { name: 'Switch to light mode' }))
  expect(document.documentElement.classList.contains('light')).toBe(true)
  release()
  await waitFor(() => expect(transport.mock.calls.some(([, i]) => i?.method === 'PUT')).toBe(true))
  await new Promise(r => setTimeout(r, 20))
  expect(document.documentElement.classList.contains('light')).toBe(true)
  expect(localStorage.getItem('odin-theme')).toBe('light')
})
