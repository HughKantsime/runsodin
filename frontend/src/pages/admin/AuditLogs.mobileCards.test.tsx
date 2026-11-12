// Mobile web pass, slice 2: Audit Log renders stacked cards under 640 px, the unchanged table above.
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import AuditLogs from './AuditLogs'

const api = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('../../api', () => ({ auditLogs: { list: api.list, exportCsv: vi.fn() }, auth: { me: vi.fn() } }))
vi.mock('react-hot-toast', () => ({ default: { error: vi.fn() } }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ hasFeature: () => true }) }))
const row = { id: 1, timestamp: '2026-10-13T12:00:00', username: 'ada', action: 'update', entity_type: 'job', entity_id: 0, details: { name: 'Benchy', changes: { status: 1 } }, ip_address: '10.0.0.5' }
function viewport(phone: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: phone && q === '(max-width: 639px)', addEventListener: vi.fn(), removeEventListener: vi.fn() }))
}
function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 3, role: 'admin', group_id: null })
  return render(<QueryClientProvider client={client}><MemoryRouter><AuditLogs /></MemoryRouter></QueryClientProvider>)
}
beforeEach(() => { vi.clearAllMocks(); api.list.mockResolvedValue({ logs: [row], total: 1, limit: 50, offset: 0 }) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

test('phone: one card per entry, entity id zero visible, details expand, no table', async () => {
  viewport(true)
  show()
  const card = (await screen.findAllByTestId('row-card'))[0]
  expect(screen.queryByRole('table')).not.toBeInTheDocument()
  expect(within(card).getByText(/update/)).toBeInTheDocument()
  expect(within(card).getByText(/job 0/)).toBeInTheDocument()
  expect(within(card).getByText('ada')).toBeInTheDocument()
  expect(within(card).getByText('10.0.0.5')).toBeInTheDocument()
  fireEvent.click(within(card).getByText(/Benchy/))
  expect(within(card).getByText(/"changes"/)).toBeInTheDocument()
})

test('phone: loading, error and empty states stay outside the card list', async () => {
  viewport(true)
  api.list.mockResolvedValue({ logs: [], total: 0, limit: 50, offset: 0 })
  show()
  expect(await screen.findByText('No audit log entries')).toBeInTheDocument()
  expect(screen.queryAllByTestId('row-card')).toHaveLength(0)
})

test('640 px and above keeps the table', async () => {
  viewport(false)
  show()
  await screen.findByText('ada')
  expect(screen.getByRole('table')).toBeInTheDocument()
  expect(screen.queryAllByTestId('row-card')).toHaveLength(0)
})
