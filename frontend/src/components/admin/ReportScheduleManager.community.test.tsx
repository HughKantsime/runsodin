// On Community the scheduled-report form is locked with the same upgrade notice as the other
// Pro sections of Settings, instead of being submittable and then refused by the backend
// (POST /report-schedules and /run need the Pro "email_notifications" feature).
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import ReportScheduleManager from './ReportScheduleManager'

const api = vi.hoisted(() => ({ list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn(), run: vi.fn() }))
vi.mock('../../api', () => ({ reportSchedules: { list: api.list, create: api.create, update: api.update, delete: api.remove, runNow: api.run } }))
const lic = vi.hoisted(() => ({ features: [] as string[] }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ hasFeature: (f: string) => lic.features.includes(f) }) }))

beforeEach(() => {
  api.list.mockResolvedValue([{ id: 7, name: 'Kept weekly report', report_type: 'job_summary', frequency: 'weekly', recipients: [], is_active: true }])
})
afterEach(() => { cleanup(); vi.clearAllMocks() })
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><ReportScheduleManager /></QueryClientProvider>)
}

test('Community: no New Schedule form, the upgrade notice, existing schedules still listed', async () => {
  lic.features = []
  mount()
  expect(await screen.findByText('Kept weekly report')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /New Schedule/ })).not.toBeInTheDocument()
  expect(screen.getByText(/Scheduled email reports requires an O\.D\.I\.N\./)).toBeInTheDocument()
  const run = screen.getByRole('button', { name: 'Run "Kept weekly report" now' })
  expect(run).toBeDisabled()
  expect(run).toHaveAttribute('title', 'Requires a Pro license (email reports)')
})

test('Pro: the New Schedule form is available', async () => {
  lic.features = ['email_notifications', 'analytics']
  mount()
  expect(await screen.findByRole('button', { name: /New Schedule/ })).toBeInTheDocument()
  expect(screen.queryByText(/Scheduled email reports requires/)).not.toBeInTheDocument()
  expect(await screen.findByRole('button', { name: 'Run "Kept weekly report" now' })).toBeEnabled()
})
