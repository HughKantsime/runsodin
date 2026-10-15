// Cert-E F13-13.3-03/04/05: real Alerts page + real API client replaying responses
// captured from the real backend (tests/test_contracts/test_cert_E_notif_wire.py).
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Alerts from './Alerts'
import { getCurrentUser } from '../../permissions'
import fixture from '../../test-fixtures/cert-E-notif-alerts.json'

vi.mock('../../permissions', async (importOriginal) => ({ ...(await importOriginal<any>()), getCurrentUser: vi.fn() }))

type Call = { method: string; path: string }
let calls: Call[] = []
let afterRead = false
const exchanges = fixture.exchanges as Record<string, any>
const TITLES = ['Print Failed: Synthetic hinge', 'Maintenance Due: P1S-10', 'Print Complete: Synthetic bracket']

function installWire() {
  calls = []; afterRead = false
  globalThis.fetch = vi.fn(async (input: any, init: any = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    const path = String(input).replace(/^\/api/, '')
    calls.push({ method, path })
    let key = `${method} ${path}`
    if (afterRead && key === 'GET /alerts?is_read=false&limit=25') key += ' (after read)'
    if (method === 'PATCH' && path.endsWith('/read')) afterRead = true
    const body = exchanges[key]
    return new Response(JSON.stringify(body ?? { detail: `unmapped ${key}` }), { status: body === undefined ? 500 : 200, headers: { 'Content-Type': 'application/json' } })
  }) as any
}

function show() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><Alerts /></MemoryRouter></QueryClientProvider>)
}

function visibleTitles() {
  return TITLES.filter(title => screen.queryByText(title))
}

beforeEach(() => { installWire(); vi.mocked(getCurrentUser).mockReturnValue({ username: 'admin', role: 'admin' } as any) })
afterEach(() => { cleanup(); vi.clearAllMocks() })

test('CertENotif: produced alerts render newest first with server-resolved printer names', async () => {
  show()
  await screen.findByText(TITLES[0])
  expect(calls).toContainEqual({ method: 'GET', path: '/alerts?limit=25' })
  const order = screen.getAllByText(/^(Print Failed|Maintenance Due|Print Complete):/).map(n => n.textContent)
  expect(order).toEqual(TITLES)
  expect(screen.getAllByText('P1S-09')).toHaveLength(2)
  expect(screen.getByText('3 unread')).toBeInTheDocument()
})

test.each([
  ['Critical', '/alerts?severity=critical&limit=25', [TITLES[0]]],
  ['Warning', '/alerts?severity=warning&limit=25', [TITLES[1]]],
  ['Info', '/alerts?severity=info&limit=25', [TITLES[2]]],
  ['Unread', '/alerts?is_read=false&limit=25', TITLES],
])('CertENotif: %s filter tab queries the server filter and shows its result', async (tab, path, expected) => {
  show()
  await screen.findByText(TITLES[0])
  fireEvent.click(screen.getByRole('button', { name: tab }))
  await waitFor(() => expect(calls.at(-1)).toEqual({ method: 'GET', path }))
  await waitFor(() => expect(visibleTitles()).toEqual(expected))
})

test('CertENotif: mark individual alert read sends PATCH and the unread filter then excludes it', async () => {
  show()
  const title = await screen.findByText(TITLES[0])
  const card = title.closest('.rounded-md.border') as HTMLElement
  fireEvent.click(within(card).getByRole('button', { name: 'Mark read' }))
  await waitFor(() => expect(calls).toContainEqual({ method: 'PATCH', path: `/alerts/${fixture.ids.print_failed}/read` }))
  await waitFor(() => expect(within(card).queryByRole('button', { name: 'Mark read' })).toBeNull())
  expect(screen.getByText('2 unread')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Unread' }))
  await waitFor(() => expect(visibleTitles()).toEqual([TITLES[1], TITLES[2]]))
})

test.each(['operator', 'admin'])('CertENotif: %s deletes an alert via confirmed DELETE receipt', async (role) => {
  vi.mocked(getCurrentUser).mockReturnValue({ username: role, role } as any)
  const changed = vi.fn(); window.addEventListener('odin:alerts-changed', changed)
  try {
    show()
    const title = await screen.findByText(TITLES[2])
    const card = title.closest('.rounded-md.border') as HTMLElement
    fireEvent.click(within(card).getByRole('button', { name: 'Delete alert' }))
    await waitFor(() => expect(screen.queryByText(TITLES[2])).toBeNull())
    expect(calls).toContainEqual({ method: 'DELETE', path: `/alerts/${fixture.ids.print_complete}` })
    expect(changed).toHaveBeenCalledTimes(1)
    expect(visibleTitles()).toEqual([TITLES[0], TITLES[1]])
  } finally { window.removeEventListener('odin:alerts-changed', changed) }
})

test('CertENotif: viewer gets no delete action on real alerts', async () => {
  vi.mocked(getCurrentUser).mockReturnValue({ username: 'student', role: 'viewer' } as any)
  show()
  await screen.findByText(TITLES[0])
  expect(screen.queryAllByRole('button', { name: 'Delete alert' })).toHaveLength(0)
  expect(screen.getAllByRole('button', { name: 'Mark read' })).toHaveLength(3)
})
