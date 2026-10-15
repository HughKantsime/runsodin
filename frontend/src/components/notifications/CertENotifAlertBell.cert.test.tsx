// Cert-E F13-13.3-01: real AlertBell (desktop toolbar + mobile header) fed by the
// real API client replaying responses captured from the real backend.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import AlertBell from './AlertBell'
import MobileHeader from '../layout/MobileHeader'
import fixture from '../../test-fixtures/cert-E-notif-alerts.json'

vi.mock('../shared/GlobalSearch', () => ({ default: () => null }))
vi.mock('../layout/ThemeToggle', () => ({ default: () => null }))
vi.mock('../../BrandingContext', () => ({ useBranding: () => ({ app_name: 'O.D.I.N.', logo_url: null }) }))

const exchanges = fixture.exchanges as Record<string, any>
let calls: { method: string; path: string }[] = []
let countIndex = 0

beforeEach(() => {
  calls = []; countIndex = 0
  globalThis.fetch = vi.fn(async (input: any, init: any = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    const path = String(input).replace(/^\/api/, '')
    calls.push({ method, path })
    let body = exchanges[`${method} ${path}`]
    if (path === '/alerts/unread-count') body = fixture.unread_counts[countIndex]
    if (method === 'PATCH' && path.endsWith('/read')) countIndex = 1
    return new Response(JSON.stringify(body ?? { detail: 'unmapped' }), { status: body === undefined ? 500 : 200, headers: { 'Content-Type': 'application/json' } })
  }) as any
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

test('CertENotif: bell shows the server unread count and opening lists recent alerts', async () => {
  render(<MemoryRouter><AlertBell /></MemoryRouter>)
  const bell = await screen.findByRole('button', { name: 'Notifications, 3 unread' })
  expect(within(bell).getByText('3')).toBeInTheDocument()
  fireEvent.click(bell)
  await screen.findByText('Print Failed: Synthetic hinge')
  expect(calls).toContainEqual({ method: 'GET', path: '/alerts?limit=10' })
  expect(screen.getByText('Maintenance Due: P1S-10')).toBeInTheDocument()
  expect(screen.getByText('Print Complete: Synthetic bracket')).toBeInTheDocument()
})

test('CertENotif: clicking an unread bell entry marks it read and decrements to the server count', async () => {
  render(<MemoryRouter><AlertBell /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: 'Notifications, 3 unread' }))
  fireEvent.click(await screen.findByText('Print Failed: Synthetic hinge'))
  await waitFor(() => expect(calls).toContainEqual({ method: 'PATCH', path: `/alerts/${fixture.ids.print_failed}/read` }))
  const expected = fixture.unread_counts[1].unread_count
  await screen.findByRole('button', { name: `Notifications, ${expected} unread` })
  // A later refresh from the server agrees with the local decrement.
  window.dispatchEvent(new Event('odin:alerts-changed'))
  await waitFor(() => expect(calls.filter(c => c.path === '/alerts/unread-count').length).toBe(2))
  expect(screen.getByRole('button', { name: `Notifications, ${expected} unread` })).toBeInTheDocument()
})

test('CertENotif: bell is part of the page header', async () => {
  render(<MemoryRouter><MobileHeader onMenuClick={() => {}} /></MemoryRouter>)
  const header = screen.getByRole('banner')
  expect(await within(header).findByRole('button', { name: 'Notifications, 3 unread' })).toBeInTheDocument()
})
