// cert-D ops: F17 Maintenance UI against the producer wire captured by
// tests/test_contracts/test_cert_D_ops_maintenance.py (actual GET/POST maintenance HTTP).
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import Maintenance from './Maintenance'
import Dashboard from '../dashboard/Dashboard'
import { I18nProvider } from '../../contexts/I18nContext'
import wire from '../../test-fixtures/certDops-maintenance-status-wire.json'

vi.mock('../../api', async () => {
  const real = await vi.importActual<typeof import('../../api/printers')>('../../api/printers')
  return {
    maintenance: real.maintenance,
    fetchAPI: vi.fn().mockResolvedValue({}), stats: { get: vi.fn() }, jobs: { list: vi.fn() }, printers: { list: vi.fn() },
    printJobs: { list: vi.fn() }, alerts: { summary: vi.fn() },
  }
})
vi.mock('../../permissions', () => ({ canDo: () => true, clearSensitiveBrowserState: vi.fn() }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))

const transport = vi.fn()
const clients: QueryClient[] = []
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
function Route() { const l = useLocation(); return <output data-testid="route">{l.pathname}</output> }
beforeEach(() => { vi.clearAllMocks(); vi.stubGlobal('fetch', transport); vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) })
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.unstubAllGlobals() })

describe('F17 dashboard widget shows upcoming and overdue tasks from the real status contract', () => {
  it('renders the overdue and due-soon tasks, hides the healthy one, and links to Maintenance', async () => {
    const c = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } }); clients.push(c)
    c.setQueryData(['stats'], { jobs: {} }); c.setQueryData(['jobs', 'active'], []); c.setQueryData(['print-jobs'], [])
    c.setQueryData(['printers'], [])
    c.setQueryData(['maintenance-status'], wire.status)
    render(<QueryClientProvider client={c}><MemoryRouter><I18nProvider><Route /><Dashboard /></I18nProvider></MemoryRouter></QueryClientProvider>)
    const widget = screen.getByRole('heading', { name: 'Maintenance Due' }).parentElement!
    expect(within(widget).getByText('Synthetic service')).toBeInTheDocument()
    expect(within(widget).getByText('certDops due soon')).toBeInTheDocument()
    expect(within(widget).queryByText('certDops fine')).not.toBeInTheDocument()
    expect(within(widget).getAllByText('OVERDUE')).toHaveLength(1)
    expect(within(widget).getByText('0d since service · 28h printed')).toBeInTheDocument()
    expect(within(widget).getByText('8d since service')).toBeInTheDocument()
    fireEvent.click(within(widget).getByText('certDops due soon'))
    expect(screen.getByTestId('route')).toHaveTextContent('/maintenance')
  })
})

describe('F17 Maintenance page: care counter display and completing a task', () => {
  it('shows total print hours, logs service with the producer-accepted body, then shows the reset task', async () => {
    let statusCalls = 0
    const posted: unknown[] = []
    transport.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === '/api/maintenance/status') return json(statusCalls++ === 0 ? wire.status : wire.status_after)
      if (url === '/api/maintenance/logs' && init?.method === 'POST') { posted.push(JSON.parse(String(init.body))); return json(wire.complete_response) }
      if (url.startsWith('/api/maintenance/logs')) return json([])
      if (url === '/api/maintenance/tasks') return json([])
      throw new Error('unexpected ' + url)
    })
    const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c)
    render(<QueryClientProvider client={c}><MemoryRouter><Maintenance /></MemoryRouter></QueryClientProvider>)
    expect(await screen.findByText('118h printed')).toBeInTheDocument()
    expect(screen.getByText(/1 overdue/)).toBeInTheDocument()
    // Printers needing attention start expanded (overall_status !== 'ok').
    expect(await screen.findByText('28h / 10h')).toBeInTheDocument()
    expect(screen.getByText('8d / 10d')).toBeInTheDocument()
    const taskRow = screen.getByText('28h / 10h').closest('.flex.items-center.justify-between') as HTMLElement
    fireEvent.click(within(taskRow).getByRole('button', { name: 'Log Service' }))
    const modal = (await screen.findByRole('heading', { name: 'Log Maintenance' })).closest('div.bg-\\[var\\(--brand-card-bg\\)\\]') as HTMLElement
    fireEvent.click(within(modal).getByRole('button', { name: 'Log Service' }))
    await waitFor(() => expect(posted).toEqual([wire.complete_request]))
    expect(await screen.findByText('0h / 10h')).toBeInTheDocument()
    expect(screen.queryByText(/1 overdue/)).not.toBeInTheDocument()
  })
})
