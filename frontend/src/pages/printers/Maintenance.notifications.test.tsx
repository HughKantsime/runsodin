// Maintenance page: back-dated service date, implicit default task, failure rate.
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Maintenance from './Maintenance'

vi.mock('../../api', async () => {
  const real = await vi.importActual<typeof import('../../api/printers')>('../../api/printers')
  return { maintenance: real.maintenance, fetchAPI: vi.fn().mockResolvedValue({}) }
})
vi.mock('../../permissions', () => ({ canDo: () => true, clearSensitiveBrowserState: vi.fn() }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))

const ymd = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
const daysFromToday = (n: number) => { const d = new Date(); d.setDate(d.getDate() + n); return ymd(d) }

const status = [{
  printer_id: 9, printer_name: 'P1S-09', printer_model: 'Bambu Lab P1S',
  total_print_hours: 2100, total_print_count: 80, prints_since_maintenance: 3, hours_since_maintenance: 12,
  overall_status: 'overdue',
  failure_rate: {
    last_30_days: { completed: 3, failed: 1, rate_percent: 25 },
    last_90_days: { completed: 3, failed: 3, rate_percent: 50 },
  },
  tasks: [{
    task_id: null, task_name: 'General service', is_default: true, description: null,
    interval_print_hours: 2000, interval_days: null, hours_since_service: 2100, days_since_service: 400,
    last_serviced: null, last_by: null, status: 'overdue', progress_percent: 105,
    next_due_print_hours: 2000, hours_until_due: -100, next_due_date: null,
  }],
}]

const transport = vi.fn()
const clients: QueryClient[] = []
const posted: any[] = []
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

beforeEach(() => {
  vi.clearAllMocks(); posted.length = 0
  vi.stubGlobal('fetch', transport)
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  transport.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === '/api/maintenance/status') return json(status)
    if (url === '/api/maintenance/logs' && init?.method === 'POST') { posted.push(JSON.parse(String(init.body))); return json({ id: 1, message: 'Maintenance logged', print_hours_at_service: 2100 }) }
    if (url.startsWith('/api/maintenance/logs')) return json([])
    if (url === '/api/maintenance/tasks') return json([])
    throw new Error('unexpected ' + url)
  })
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.unstubAllGlobals() })

function renderPage() {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c)
  render(<QueryClientProvider client={c}><MemoryRouter><Maintenance /></MemoryRouter></QueryClientProvider>)
}

async function openLogModal() {
  fireEvent.click(await screen.findByRole('button', { name: 'Log Service' }))
  const heading = await screen.findByRole('heading', { name: 'Log Maintenance' })
  return heading.closest('div.bg-\\[var\\(--brand-card-bg\\)\\]') as HTMLElement
}

describe('implicit default task and failure rate', () => {
  it('shows the default task with a "default" badge and the 30/90 day failure rate', async () => {
    renderPage()
    expect(await screen.findByText('General service')).toBeInTheDocument()
    expect(screen.getByText('default')).toBeInTheDocument()
    expect(screen.getByText('30d: 1 of 4 failed (25%) · 90d: 3 of 6 failed (50%)')).toBeInTheDocument()
  })

  it('says "no prints" for a window without completed or failed prints', async () => {
    const empty = { completed: 0, failed: 0, rate_percent: null }
    transport.mockImplementation(async (url: string) => {
      if (url === '/api/maintenance/status') return json([{ ...status[0], failure_rate: { last_30_days: empty, last_90_days: empty } }])
      return json([])
    })
    renderPage()
    expect(await screen.findByText('30d: no prints · 90d: no prints')).toBeInTheDocument()
  })
})

describe('log form date', () => {
  it('defaults to today and then sends no performed_at (the server stamps now)', async () => {
    renderPage()
    const modal = await openLogModal()
    const date = within(modal).getByLabelText('Date performed') as HTMLInputElement
    expect(date.value).toBe(ymd(new Date()))
    fireEvent.click(within(modal).getByRole('button', { name: 'Log Service' }))
    await waitFor(() => expect(posted).toEqual([
      { printer_id: 9, task_id: null, task_name: 'General service', performed_by: null, cost: 0, downtime_minutes: 0, notes: null },
    ]))
  })

  it('sends a past date as performed_at', async () => {
    renderPage()
    const modal = await openLogModal()
    const past = daysFromToday(-3)
    fireEvent.change(within(modal).getByLabelText('Date performed'), { target: { value: past } })
    fireEvent.click(within(modal).getByRole('button', { name: 'Log Service' }))
    await waitFor(() => expect(posted).toHaveLength(1))
    // Noon local time on that day, sent with its offset (never a bare date read as UTC midnight).
    const [y, m, d] = past.split('-').map(Number)
    expect(posted[0].performed_at).toBe(new Date(y, m - 1, d, 12, 0, 0).toISOString())
  })

  it('refuses a future date and a date before 2020', async () => {
    renderPage()
    const modal = await openLogModal()
    const date = within(modal).getByLabelText('Date performed')
    const submit = within(modal).getByRole('button', { name: 'Log Service' })
    fireEvent.change(date, { target: { value: daysFromToday(1) } })
    expect(submit).toBeDisabled()
    fireEvent.change(date, { target: { value: '2019-12-31' } })
    expect(submit).toBeDisabled()
    fireEvent.change(date, { target: { value: '2020-01-01' } })
    expect(submit).not.toBeDisabled()
    expect(posted).toEqual([])
  })
})
