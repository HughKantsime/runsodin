// Cert-E (rep): F10-10.7-03, F10-10.9-03, F10-10.9-04, F10-10.9-06.
// Real Settings page -> real System tab -> real SystemTab, ReportScheduleManager
// and ChargebackReport, real api client (fetchAPI). Only window.fetch is stubbed,
// and it replays producer fixtures captured from the real backend routers:
//   tests/test_contracts/test_cert_E_rep_chargebacks.py (cert-E-rep-chargeback-wire.json)
//   tests/test_contracts/test_cert_E_rep_schedules.py  (cert-E-rep-schedules-wire.json)
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Toaster } from 'react-hot-toast'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Settings from './Settings'
import chargebackWire from '../../test-fixtures/cert-E-rep-chargeback-wire.json'
import scheduleWire from '../../test-fixtures/cert-E-rep-schedules-wire.json'

vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, hasFeature: () => true }) }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 3 }), clearSensitiveBrowserState: vi.fn() }))
// Unrelated panels (not under certification) are replaced to keep the wire map small.
vi.mock('../../components/admin/GeneralTab', () => ({ default: () => <div>General boundary</div> }))
vi.mock('../../components/admin/BackupRestore', () => ({ default: () => null }))
vi.mock('../../components/admin/TroubleshootingPanel', () => ({ default: () => null }))
vi.mock('../../components/admin/DataRetentionSettings', () => ({ default: () => null }))

type Call = { url: string; method: string; body: unknown }
let calls: Call[] = []
let runResponse: { status: number; body: unknown } = scheduleWire.run_sent

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

beforeEach(() => {
  calls = []
  runResponse = scheduleWire.run_sent
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo, init: RequestInit = {}) => {
    const url = String(input)
    const method = (init.method || 'GET').toUpperCase()
    calls.push({ url, method, body: init.body ? JSON.parse(String(init.body)) : undefined })
    const id = scheduleWire.list[0].id
    if (url.startsWith('/api/reports/chargebacks')) {
      const q = new URLSearchParams(url.split('?')[1] || '')
      const captured = q.get('start_date') === chargebackWire.request.start_date && q.get('end_date') === chargebackWire.request.end_date
      return json(200, captured ? chargebackWire.rows : [])
    }
    if (url === '/api/report-schedules' && method === 'GET') return json(200, scheduleWire.list)
    if (url === `/api/report-schedules/${id}` && method === 'PATCH') return json(200, scheduleWire.toggle)
    if (url === `/api/report-schedules/${id}/run` && method === 'POST') return json(runResponse.status, runResponse.body)
    if (url === '/api/backups') return json(200, [])
    return json(200, {})
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.clearAllMocks() })

async function openSystemTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/settings']}><Settings /><Toaster /></MemoryRouter></QueryClientProvider>)
  expect(screen.queryByRole('heading', { name: 'Cost Chargebacks' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'System' }))
}

test('CertERep F10-10.7-03 chargeback report lives in Settings -> System and renders captured per-user rows', async () => {
  await openSystemTab()
  expect(await screen.findByRole('heading', { name: 'Cost Chargebacks' })).toBeInTheDocument()
  expect(await screen.findByText('No chargeback data for this period.')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Start Date'), { target: { value: '2026-09-01' } })
  fireEvent.change(screen.getByLabelText('End Date'), { target: { value: '2026-09-30' } })
  const manager = (await screen.findByText('manager')).closest('tr')!
  expect(within(manager).getAllByRole('cell').map(c => c.textContent)).toEqual(['manager', '2', '7.0', '24g', 'EUR 10.00'])
  const student = screen.getByText('student').closest('tr')!
  expect(within(student).getAllByRole('cell').map(c => c.textContent)).toEqual(['student', '2', '3.5', '16g', 'EUR 5.00'])
  expect(calls.some(c => c.url === '/api/reports/chargebacks?start_date=2026-09-01&end_date=2026-09-30')).toBe(true)
  expect(screen.getAllByRole('columnheader').map(h => h.textContent)).toEqual(expect.arrayContaining(['User', 'Jobs', 'Hours', 'Grams', 'Cost']))
})

test('CertERep F10-10.9-04 schedule management lives in Settings -> System: list, create, delete', async () => {
  await openSystemTab()
  expect(await screen.findByRole('heading', { name: 'Scheduled Reports' })).toBeInTheDocument()
  expect(await screen.findByText('Cert weekly')).toBeInTheDocument()
  expect(screen.getByText('job summary · weekly · ops@example.test')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'New Schedule' }))
  const types = Array.from((screen.getByLabelText('Report Type') as HTMLSelectElement).options).map(o => o.value)
  expect(types).toEqual(['fleet_utilization', 'job_summary', 'filament_consumption', 'failure_analysis', 'chargeback_summary'])
  const freqs = Array.from((screen.getByLabelText('Frequency') as HTMLSelectElement).options).map(o => o.value)
  expect(freqs).toEqual(['daily', 'weekly', 'monthly'])
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Cert weekly' } })
  fireEvent.change(screen.getByLabelText('Report Type'), { target: { value: 'job_summary' } })
  fireEvent.change(screen.getByLabelText('Recipients (comma-separated emails)'), { target: { value: 'ops@example.test' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create Schedule' }))
  // Same body the producer test POSTed to the real router (accepted with 200).
  await waitFor(() => expect(calls.find(c => c.method === 'POST' && c.url === '/api/report-schedules')?.body)
    .toEqual({ name: 'Cert weekly', report_type: 'job_summary', frequency: 'weekly', recipients: ['ops@example.test'] }))
  fireEvent.click(screen.getByRole('button', { name: 'Delete schedule' }))
  await waitFor(() => expect(calls.some(c => c.method === 'DELETE' && c.url === `/api/report-schedules/${scheduleWire.list[0].id}`)).toBe(true))
})

test('CertERep F10-10.9-03 toggle active/inactive sends exact PATCH and shows recipients', async () => {
  await openSystemTab()
  await screen.findByText('Cert weekly')
  expect(screen.getByText(/ops@example\.test/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Pause schedule' }))
  await waitFor(() => expect(calls.find(c => c.method === 'PATCH')).toEqual({ url: `/api/report-schedules/${scheduleWire.list[0].id}`, method: 'PATCH', body: { is_active: false } }))
  expect(calls.some(c => c.url.endsWith('/run'))).toBe(false)
})

test('CertERep F10-10.9-06 Run Now button triggers immediate run and reports success', async () => {
  await openSystemTab()
  await screen.findByText('Cert weekly')
  fireEvent.click(screen.getByRole('button', { name: 'Run "Cert weekly" now' }))
  await waitFor(() => expect(calls.filter(c => c.method === 'POST' && c.url === `/api/report-schedules/${scheduleWire.list[0].id}/run`)).toHaveLength(1))
  expect(await screen.findByText('Report sent')).toBeInTheDocument()
})

test('CertERep F10-10.9-06 Run Now surfaces an unconfigured SMTP refusal', async () => {
  runResponse = scheduleWire.run_unconfigured
  await openSystemTab()
  await screen.findByText('Cert weekly')
  fireEvent.click(screen.getByRole('button', { name: 'Run "Cert weekly" now' }))
  expect(await screen.findByText('Failed to send report: Report generation failed. Check report configuration.')).toBeInTheDocument()
})

// Fixed (was: fetchAPI wrapped the object 502 detail {message,sent_count,failed_count} as "[object Object]").
test('CertERep F10-10.9-06 Run Now partial delivery shows the backend message, not [object Object]', async () => {
  runResponse = scheduleWire.run_partial
  await openSystemTab()
  await screen.findByText('Cert weekly')
  fireEvent.click(screen.getByRole('button', { name: 'Run "Cert weekly" now' }))
  await screen.findByText(/^Failed to send report:/)
  expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument()
  expect(screen.getByText(/Report was not delivered to every recipient/)).toBeInTheDocument()
  expect(screen.getByText(/Report was not delivered to every recipient/).textContent).toBe('Failed to send report: Report was not delivered to every recipient (sent 1, failed 1)')
})
