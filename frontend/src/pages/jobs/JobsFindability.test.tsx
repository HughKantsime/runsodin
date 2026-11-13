// The Jobs page against the findability contract: Active by default, history paged with a total,
// search/filters on the server and in the URL, Job numbers, Held, row Cancel, deep links.
// Only the HTTP client boundary ('../../api') is replaced.
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import Jobs from './Jobs'

const api = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn(), cancel: vi.fn(), recent: vi.fn(), dispatch: vi.fn(), bulk: vi.fn() }))
vi.mock('../../api', () => ({
  jobs: { list: api.list, get: api.get, cancel: api.cancel, recentTerminal: api.recent, create: vi.fn(), update: vi.fn(), reorder: vi.fn(), dispatch: api.dispatch, start: vi.fn(), complete: vi.fn(), delete: vi.fn(), repeat: vi.fn() },
  models: { list: vi.fn().mockResolvedValue([]) }, modelRevisions: { list: vi.fn().mockResolvedValue([]) },
  printers: { list: vi.fn().mockResolvedValue([{ id: 7, name: 'Printer Alpha', is_active: true }, { id: 8, name: 'Printer Beta', is_active: true }]) },
  presets: { list: vi.fn().mockResolvedValue([]), create: vi.fn(), schedule: vi.fn(), delete: vi.fn() },
  getApprovalSetting: vi.fn().mockResolvedValue({ require_approval: false }), scheduler: { run: vi.fn() }, bulkOps: { jobs: api.bulk },
  approveJob: vi.fn(), rejectJob: vi.fn(), resubmitJob: vi.fn(), updateJobFailure: vi.fn(),
}))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 17 }) }))
vi.mock('../../permissions', () => ({ canDo: () => true, getCurrentUser: () => ({ id: 3, role: 'operator' }) }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))

const base = { priority: 3, quantity: 1, duration_hours: 2, colors_list: [], hold: false, is_locked: false, order_item_id: null, charged_to_org_id: 17, queue_position: null }
const clients: QueryClient[] = []
const ACTIVE = ['submitted', 'rejected', 'pending', 'scheduled', 'printing', 'paused']
const HISTORY = ['completed', 'failed', 'cancelled']
const call = (statuses: unknown, params: Record<string, unknown> = {}) =>
  [statuses, 17, expect.objectContaining(params), expect.any(Function)] as const

function Where() { const l = useLocation(); return <output data-testid="where">{l.pathname + l.search}</output> }
async function show(url = '/jobs', waitFor_: string | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 3, group_id: 17, role: 'operator' }); clients.push(client)
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <Where />
        <Routes>
          <Route path="/jobs" element={<Jobs />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>)
  if (waitFor_) await screen.findByText(waitFor_)
}
const row = (name: string) => screen.getByText(name, { exact: true }).closest('tr')!

beforeEach(() => {
  api.recent.mockResolvedValue([])
  api.cancel.mockResolvedValue({})
  api.list.mockImplementation(async () => [{ ...base, id: 21, item_name: 'Queued thing', status: 'pending' }])
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.clearAllMocks() })

it('opens on the Active queue: one request with several statuses, queue order, the largest page', async () => {
  await show('/jobs', 'Queued thing')
  expect(api.list).toHaveBeenCalledWith(...call(ACTIVE, { order: 'queue', limit: 500, offset: 0, q: '' }))
  expect((screen.getByRole('combobox', { name: 'Filter jobs by status' }) as HTMLSelectElement).value).toBe('')
})

it('History is newest first, 50 at a time, says how many there are, and Load more fetches the next page', async () => {
  const history = (offset: number, limit: number) => Array.from({ length: Math.min(limit, 212 - offset) }, (_, i) => ({ ...base, id: 1000 + offset + i, item_name: `Done ${offset + i}`, status: 'completed' }))
  api.list.mockImplementation(async (statuses, _org, params, onTotal) => {
    if (statuses?.includes('completed')) { onTotal?.(212); return history(params.offset, params.limit) }
    return [{ ...base, id: 21, item_name: 'Queued thing', status: 'pending' }]
  })
  await show('/jobs', 'Queued thing')
  fireEvent.change(screen.getByRole('combobox', { name: 'Filter jobs by status' }), { target: { value: 'history' } })
  await screen.findByText('Done 0')
  expect(api.list).toHaveBeenCalledWith(...call(HISTORY, { order: 'recent', limit: 50, offset: 0 }))
  expect(screen.getByText('Showing 50 of 212')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
  await screen.findByText('Done 99')
  expect(api.list).toHaveBeenCalledWith(...call(HISTORY, { limit: 50, offset: 50 }))
  expect(screen.getByText('Showing 100 of 212')).toBeInTheDocument()
})

it('a list that is complete says so and offers no Load more', async () => {
  api.list.mockImplementation(async (_s, _o, _p, onTotal) => { onTotal?.(1); return [{ ...base, id: 21, item_name: 'Queued thing', status: 'pending' }] })
  await show('/jobs', 'Queued thing')
  expect(screen.getByText('Showing 1 of 1')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
})

it('each row shows its job number, the queue position reads "Queue N", and held jobs carry a Held badge', async () => {
  api.list.mockImplementation(async () => [
    { ...base, id: 218, item_name: 'Bracket', status: 'pending', queue_position: 2, hold: true },
    { ...base, id: 219, item_name: 'Plain job', status: 'pending' },
  ])
  await show('/jobs', 'Bracket')
  expect(within(row('Bracket')).getByText('Job 218')).toBeInTheDocument()
  expect(within(row('Bracket')).getByText('Queue 3')).toBeInTheDocument()
  expect(within(row('Bracket')).queryByText('#3')).not.toBeInTheDocument()
  expect(within(row('Bracket')).getByText('Held')).toBeInTheDocument()
  expect(within(row('Plain job')).getByText('Job 219')).toBeInTheDocument()
  expect(within(row('Plain job')).queryByText('Held')).not.toBeInTheDocument()
})

it('search is sent to the server once typing pauses, and lands in the URL', async () => {
  await show('/jobs', 'Queued thing')
  fireEvent.change(screen.getByPlaceholderText('Search jobs...'), { target: { value: 'job 218' } })
  await waitFor(() => expect(api.list).toHaveBeenCalledWith(...call(ACTIVE, { q: 'job 218' })))
  await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('q=job+218'))
})

it('filters and search are restored from the URL, and changing the printer updates it', async () => {
  await show('/jobs?status=history&q=bracket&printer=unassigned&held=1')
  await waitFor(() => expect(api.list).toHaveBeenCalledWith(...call(HISTORY, { q: 'bracket', unassigned: true, held: true, printerId: null, order: 'recent' })))
  expect((screen.getByPlaceholderText('Search jobs...') as HTMLInputElement).value).toBe('bracket')
  expect((screen.getByRole('combobox', { name: 'Filter jobs by status' }) as HTMLSelectElement).value).toBe('history')
  expect((screen.getByRole('combobox', { name: 'Filter jobs by printer' }) as HTMLSelectElement).value).toBe('unassigned')
  expect((screen.getByRole('checkbox', { name: 'Held only' }) as HTMLInputElement).checked).toBe(true)
  fireEvent.change(screen.getByRole('combobox', { name: 'Filter jobs by printer' }), { target: { value: '7' } })
  await waitFor(() => expect(api.list).toHaveBeenCalledWith(...call(HISTORY, { printerId: 7, unassigned: false })))
  expect(screen.getByTestId('where')).toHaveTextContent('printer=7')
  fireEvent.click(screen.getByRole('checkbox', { name: 'Held only' }))
  await waitFor(() => expect(screen.getByTestId('where')).not.toHaveTextContent('held=1'))
})

it('pending and submitted rows can be cancelled from the row, through the confirm dialog', async () => {
  api.list.mockImplementation(async () => [
    { ...base, id: 21, item_name: 'Queued thing', status: 'pending' },
    { ...base, id: 22, item_name: 'Awaiting approval thing', status: 'submitted', submitted_by: 4 },
    { ...base, id: 23, item_name: 'Finished thing', status: 'completed' },
  ])
  await show('/jobs', 'Queued thing')
  expect(within(row('Queued thing')).getByRole('button', { name: 'Cancel job' })).toBeInTheDocument()
  expect(within(row('Finished thing')).queryByRole('button', { name: 'Cancel job' })).not.toBeInTheDocument()
  fireEvent.click(within(row('Awaiting approval thing')).getByRole('button', { name: 'Cancel job' }))
  expect(api.cancel).not.toHaveBeenCalled() // nothing happens until the dialog is confirmed
  await screen.findByText(/Cancel this job\?/)
  const confirm = screen.getAllByRole('button').find(b => b.textContent === 'Cancel Job')!
  fireEvent.click(confirm)
  await waitFor(() => expect(api.cancel).toHaveBeenCalled())
  expect(api.cancel.mock.calls[0][0]).toBe(22)
})

it.each(['/jobs?job=999'])('%s opens a job that is outside the current filter', async url => {
  api.get.mockResolvedValue({ ...base, id: 999, item_name: 'Far away job', status: 'pending' })
  await show(url)
  await screen.findByText('Far away job')
  expect(api.get).toHaveBeenCalledWith(999)
  expect(within(row('Far away job')).getByText('Job 999')).toBeInTheDocument()
  const dialog = await screen.findByRole('dialog')
  expect(dialog).toHaveTextContent('Edit Job')
  expect(dialog).toHaveTextContent('Job 999')
})

it('a deep-linked job that is already listed is not fetched again', async () => {
  await show('/jobs?job=21', 'Queued thing')
  await screen.findByRole('dialog')
  expect(api.get).not.toHaveBeenCalled()
})

it('a deep link to a job that is missing or hidden says only that it was not found', async () => {
  api.get.mockRejectedValue(new Error('The requested resource was not found.'))
  await show('/jobs?job=404', 'Queued thing')
  expect(await screen.findByText('Job 404 was not found, or you cannot see it.')).toBeInTheDocument()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

it('a deep-linked job outside the filter keeps the dispatch safeguards of an ordinary row', async () => {
  // Regression (Codex sign-off 11-12): the extra row had no isDispatching/dispatchFenced, so
  // Dispatch stayed enabled while a dispatch was in flight and could be sent again.
  api.get.mockResolvedValue({ ...base, id: 999, item_name: 'Far away job', status: 'scheduled', printer_id: 7, printer: { id: 7, name: 'Printer Alpha' } })
  api.dispatch.mockReturnValue(new Promise(() => {})) // never answers: the dispatch stays in flight
  await show('/jobs?job=999')
  await screen.findByText('Far away job')
  fireEvent.click(within(row('Far away job')).getByRole('button', { name: 'Dispatch to printer' }))
  await screen.findByText(/will be uploaded to/)
  fireEvent.click(screen.getAllByRole('button').find(b => b.textContent === 'Dispatch' && b.getAttribute('aria-label') !== 'Dispatch to printer')!)
  await waitFor(() => expect(api.dispatch).toHaveBeenCalledTimes(1))
  const busy = await within(row('Far away job')).findByRole('button', { name: 'Dispatching...' })
  expect(busy).toBeDisabled()
})

it('a selection above the server limit of 100 is sent in batches', async () => {
  // Regression (Codex sign-off 11-12): the Active view loads up to 500 jobs, Select all sent
  // them in one request and the server refused it.
  api.list.mockImplementation(async () => Array.from({ length: 135 }, (_, i) => ({ ...base, id: 1000 + i, item_name: `Bulk job ${i}`, status: 'pending' })))
  api.bulk.mockResolvedValue({ affected: 1, skipped: 0 })
  await show('/jobs', 'Bulk job 0')
  fireEvent.click(screen.getByRole('checkbox', { name: 'Select all jobs' }))
  await screen.findByText('135 selected')
  fireEvent.click(screen.getAllByRole('button').find(b => b.textContent === 'Priority 1')!)
  await waitFor(() => expect(api.bulk).toHaveBeenCalledTimes(2))
  expect(api.bulk.mock.calls.map(c => c[0].length)).toEqual([100, 35])
  expect(new Set(api.bulk.mock.calls.flatMap(c => c[0])).size).toBe(135)
  expect(api.bulk.mock.calls.every(c => c[1] === 'set_priority')).toBe(true)
})

it('the extra row of a deep-linked job shows that it is selected', async () => {
  // Regression (Codex sign-off 11-12): its checkbox stayed unchecked while bulk actions included it.
  api.get.mockResolvedValue({ ...base, id: 999, item_name: 'Far away job', status: 'pending' })
  await show('/jobs?job=999')
  await screen.findByText('Far away job')
  const box = within(row('Far away job')).getByRole('checkbox') as HTMLInputElement
  expect(box.checked).toBe(false)
  fireEvent.click(box)
  await waitFor(() => expect((within(row('Far away job')).getByRole('checkbox') as HTMLInputElement).checked).toBe(true))
  expect(await screen.findByText('1 selected')).toBeInTheDocument()
  // Third sign-off note: one listed row plus the extra row. Selecting only the extra row is
  // not "all"; Select all then takes both, and again clears both.
  const all = screen.getByRole('checkbox', { name: 'Select all jobs' }) as HTMLInputElement
  expect(all.checked).toBe(false)
  fireEvent.click(all)
  expect(await screen.findByText('2 selected')).toBeInTheDocument()
  expect((screen.getByRole('checkbox', { name: 'Select all jobs' }) as HTMLInputElement).checked).toBe(true)
  fireEvent.click(screen.getByRole('checkbox', { name: 'Select all jobs' }))
  await waitFor(() => expect(screen.queryByText('2 selected')).not.toBeInTheDocument())
})
