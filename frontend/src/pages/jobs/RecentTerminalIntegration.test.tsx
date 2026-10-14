import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Jobs from './Jobs'
const api = vi.hoisted(() => ({ list: vi.fn(), recent: vi.fn(), bulk: vi.fn(), success: vi.fn() }))
vi.mock('../../api', () => ({
  jobs: { list: api.list, recentTerminal: api.recent, create: vi.fn(), update: vi.fn(), reorder: vi.fn(), dispatch: vi.fn(), start: vi.fn(), complete: vi.fn(), cancel: vi.fn(), delete: vi.fn(), repeat: vi.fn() },
  models: { list: vi.fn().mockResolvedValue([]) }, modelRevisions: { list: vi.fn().mockResolvedValue([]) }, printers: { list: vi.fn().mockResolvedValue([]) }, presets: { list: vi.fn().mockResolvedValue([]), create: vi.fn(), schedule: vi.fn() }, getApprovalSetting: vi.fn().mockResolvedValue({ require_approval: false }), scheduler: { run: vi.fn() }, bulkOps: { jobs: api.bulk }, approveJob: vi.fn(), rejectJob: vi.fn(), resubmitJob: vi.fn(), updateJobFailure: vi.fn()
}))
vi.mock('react-hot-toast', () => ({ default: { success: api.success, error: vi.fn() } }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 17 }) }))
vi.mock('../../permissions', () => ({ canDo: () => false, getCurrentUser: () => ({ id: 3, role: 'viewer' }) }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
test('dedicated recent projection survives queue status and search filters', async () => {
  api.list.mockResolvedValue([{ id: 20, item_name: 'Pending queue row', status: 'pending', quantity: 1, hold: false, is_locked: false, colors_list: [], priority: 3 }])
  api.recent.mockResolvedValue(Array.from({ length: 8 }, (_, i) => ({ id: 100 - i, item_name: `Recent terminal ${i}`, status: 'completed', actual_end: '2026-10-12T12:00:00Z', duration_seconds: 0, printer_name: null, model_name: null, fail_reason: null, thumbnail_data: null })))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 3, group_id: 17, role: 'viewer' })
  render(<QueryClientProvider client={client}><MemoryRouter><Jobs /></MemoryRouter></QueryClientProvider>)
  await screen.findByText('Recent terminal 0')
  expect(screen.getAllByText(/^Recent terminal/)).toHaveLength(8)
  expect(api.recent).toHaveBeenCalledExactlyOnceWith(17)
  fireEvent.change(screen.getByPlaceholderText('Search jobs...'), { target: { value: 'No queue match' } })
  expect(screen.queryByText('Pending queue row')).not.toBeInTheDocument()
  expect(screen.getAllByText(/^Recent terminal/)).toHaveLength(8)
  fireEvent.change(screen.getByRole('combobox', { name: 'Filter jobs by status' }), { target: { value: 'pending' } })
  await waitFor(() => expect(api.list).toHaveBeenCalledWith('pending', 17))
  expect(api.recent).toHaveBeenCalledExactlyOnceWith(17)
  expect(screen.getAllByText(/^Recent terminal/)).toHaveLength(8)
  client.clear()
})

test('another principal in the same organization never receives the prior recent cache', async () => {
  api.list.mockResolvedValue([])
  let resolveSecond: (value: any[]) => void = () => {}
  const second = new Promise<any[]>(resolve => { resolveSecond = resolve })
  const privateJob = { id: 1, item_name: 'Private prior account outcome', status: 'completed', actual_end: null, duration_seconds: null, printer_name: null, model_name: null, fail_reason: null, thumbnail_data: null }
  api.recent.mockResolvedValueOnce([privateJob]).mockImplementation(() => second)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 3, group_id: 17, role: 'viewer' })
  render(<QueryClientProvider client={client}><MemoryRouter><Jobs /></MemoryRouter></QueryClientProvider>)
  await screen.findByText('Private prior account outcome')
  const { act } = await import('@testing-library/react')
  act(() => client.setQueryData(['auth', 'me'], { id: 4, group_id: 17, role: 'viewer' }))
  await waitFor(() => expect(screen.queryByText('Private prior account outcome')).not.toBeInTheDocument())
  expect(screen.getByRole('status')).toHaveTextContent('Loading recent jobs')
  act(() => resolveSecond([]))
  await screen.findByText('No completed or failed jobs.')
  client.clear()
})

test('bulk reschedule reports zero/skipped truthfully and prevents duplicate pending requests', async () => {
  api.list.mockResolvedValue([{ id: 20, item_name: 'Scheduled queue row', status: 'scheduled', quantity: 1, hold: false, is_locked: false, colors_list: [], priority: 3 }])
  api.recent.mockResolvedValue([])
  let finish: (value: unknown) => void = () => {}
  api.bulk.mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 3, group_id: 17, role: 'viewer' })
  render(<QueryClientProvider client={client}><MemoryRouter><Jobs /></MemoryRouter></QueryClientProvider>)
  await screen.findByText('Scheduled queue row')
  fireEvent.click(screen.getByRole('checkbox', { name: 'Select job Scheduled queue row' }))
  fireEvent.click(screen.getByRole('button', { name: 'Reschedule' }))
  await waitFor(() => expect(api.bulk).toHaveBeenCalledExactlyOnceWith([20], 'reschedule', undefined))
  expect(screen.getByRole('button', { name: 'Reschedule' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Reschedule' }))
  expect(api.bulk).toHaveBeenCalledTimes(1)
  const { act } = await import('@testing-library/react')
  act(() => finish({ affected: 0, skipped: 1 }))
  await waitFor(() => expect(api.success).toHaveBeenCalledWith('0 job(s) returned to queue; 1 unchanged'))
  expect(api.success).not.toHaveBeenCalledWith('Bulk reschedule completed')
  client.clear()
})

test('recent error Retry refetches the dedicated query successfully', async () => {
  api.list.mockResolvedValue([])
  api.recent.mockRejectedValueOnce(new Error('Synthetic unavailable')).mockResolvedValueOnce([{ id: 1, item_name: 'Outcome after retry', status: 'completed', actual_end: null, duration_seconds: 0, printer_name: null, model_name: null, fail_reason: null, thumbnail_data: null }])
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 3, group_id: 17, role: 'viewer' })
  render(<QueryClientProvider client={client}><MemoryRouter><Jobs /></MemoryRouter></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
  await screen.findByText('Outcome after retry')
  expect(api.recent).toHaveBeenCalledTimes(2)
  client.clear()
})
