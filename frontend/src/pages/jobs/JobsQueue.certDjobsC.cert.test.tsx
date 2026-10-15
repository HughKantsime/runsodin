// cert-D jobsC: the real Jobs page, real RBAC module and real child components rendered against
// HTTP responses captured by tests/test_contracts/test_cert_D_jobsC_wire.py. Only the HTTP client
// boundary ('../../api') is replaced, and it returns the captured producer bodies.
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Jobs from './Jobs'
import { refreshPermissions } from '../../permissions'
import wire from '../../test-fixtures/certD-jobsC-wire.json'

const api = vi.hoisted(() => ({ list: vi.fn(), reorder: vi.fn(), update: vi.fn(), repeat: vi.fn(), cancel: vi.fn(), fail: vi.fn(), failure: vi.fn(), recent: vi.fn() }))
vi.mock('../../api', () => ({
  jobs: { list: api.list, recentTerminal: api.recent, reorder: api.reorder, update: api.update, repeat: api.repeat, cancel: api.cancel, fail: api.fail, create: vi.fn(), start: vi.fn(), complete: vi.fn(), delete: vi.fn(), dispatch: vi.fn(), get: vi.fn() },
  models: { list: vi.fn().mockResolvedValue([]) }, modelRevisions: { list: vi.fn().mockResolvedValue([]) },
  printers: { list: vi.fn(async () => wire.printers) },
  presets: { list: vi.fn().mockResolvedValue([]), create: vi.fn(), schedule: vi.fn(), delete: vi.fn() },
  getApprovalSetting: vi.fn().mockResolvedValue({ require_approval: false }), scheduler: { run: vi.fn() }, bulkOps: { jobs: vi.fn() },
  approveJob: vi.fn(), rejectJob: vi.fn(), resubmitJob: vi.fn(), updateJobFailure: api.failure,
}))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: null }) }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))

let current: any[] = []
const clients: QueryClient[] = []
async function signIn(me: any) {
  // Real permissions module, fed the captured /api/auth/me identity and /api/permissions body.
  vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => (url.endsWith('/auth/me') ? me : wire.permissions) })))
  await refreshPermissions()
}
async function mount(me: any = wire.me_operator) {
  await signIn(me)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], me); clients.push(client)
  const view = render(<QueryClientProvider client={client}><MemoryRouter><Jobs /></MemoryRouter></QueryClientProvider>)
  await screen.findByText('CertC adhoc pending A')
  return view
}
const row = (name: string) => screen.getAllByText(name, { exact: true }).map(e => e.closest('tr')).find(Boolean)!
const visibleQueue = () => Array.from(document.querySelectorAll('tbody tr')).map(r => r.querySelector('td:nth-child(3) .font-medium')?.textContent)
function field(label: string) { const l = within(screen.getByRole('dialog')).getByText(label, { selector: 'label', exact: true }); return l.parentElement!.querySelector('input,select,textarea') as HTMLElement }

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-10-12T12:00:00Z'))
  current = wire.jobs_before
  api.list.mockImplementation(async () => current)
  api.recent.mockResolvedValue(wire.recent_terminal)
  api.reorder.mockImplementation(async () => { current = wire.jobs_after_reorder; return wire.reorder_response })
  api.update.mockImplementation(async () => { current = wire.jobs_after; return wire.edit_response })
  api.repeat.mockResolvedValue(wire.repeat_response)
  api.failure.mockResolvedValue(wire.failure_response)
  api.cancel.mockRejectedValue(new Error(wire.cancel_printing_response.detail))
  api.fail.mockResolvedValue(wire.fail_printing_response)
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.useRealTimers(); vi.unstubAllGlobals(); vi.clearAllMocks() })

// ---- F04-4.3-02 tabs ----
it('F04-4.3-02 queue tabs are All Jobs, Awaiting Approval, Order Jobs, Ad-hoc with wire counts and exact row filtering', async () => {
  await mount()
  const tabs = screen.getAllByRole('button').filter(b => /^(All Jobs|Awaiting Approval|Order Jobs|Ad-hoc)/.test(b.textContent || ''))
  expect(tabs.map(t => t.textContent)).toEqual(['All Jobs16', 'Awaiting Approval1', 'Order Jobs1', 'Ad-hoc15'])
  fireEvent.click(screen.getByRole('button', { name: /Order Jobs/ }))
  expect(visibleQueue()).toEqual(['CertC order scheduled'])
  fireEvent.click(screen.getByRole('button', { name: /Ad-hoc/ }))
  expect(visibleQueue()).not.toContain('CertC order scheduled')
  expect(visibleQueue()).toHaveLength(15)
  fireEvent.click(screen.getByRole('button', { name: /All Jobs/ }))
  expect(visibleQueue()).toHaveLength(16)
})

// ---- F04-4.3-03 at-risk ----
it('F04-4.3-03 overdue active jobs get the red tint; future-due and completed-overdue rows do not', async () => {
  await mount()
  for (const name of ['CertC adhoc pending A', 'CertC printing']) expect(row(name)).toHaveClass('bg-red-950/30', 'border-l-red-500')
  for (const name of ['CertC adhoc pending B', 'CertC completed overdue', 'CertC order scheduled']) expect(row(name)).not.toHaveClass('bg-red-950/30')
  expect(within(row('CertC adhoc pending A')).getByText('2d overdue')).toHaveClass('text-red-400')
})

// ---- F04-4.3-01 drag reorder ----
it('F04-4.3-01 operator drags B onto A and the exact captured reorder body is sent', async () => {
  await mount()
  const transfer = { effectAllowed: '', dropEffect: '', setData: vi.fn() }
  expect(row('CertC adhoc pending B')).toHaveAttribute('draggable', 'true')
  expect(row('CertC completed overdue')).not.toHaveAttribute('draggable')
  fireEvent.dragStart(row('CertC adhoc pending B'), { dataTransfer: transfer })
  fireEvent.dragOver(row('CertC adhoc pending A'), { dataTransfer: transfer })
  fireEvent.drop(row('CertC adhoc pending A'), { dataTransfer: transfer })
  await waitFor(() => expect(api.reorder).toHaveBeenCalledExactlyOnceWith(wire.reorder_request.job_ids))
  await waitFor(() => expect(api.list.mock.calls.length).toBeGreaterThan(1))
})
it('F04-4.3-01 viewer (captured RBAC) gets no drag handles and a synthetic drop sends nothing', async () => {
  await mount(wire.me_viewer)
  expect(row('CertC adhoc pending B')).not.toHaveAttribute('draggable')
  const transfer = { effectAllowed: '', dropEffect: '', setData: vi.fn() }
  fireEvent.dragStart(row('CertC adhoc pending B'), { dataTransfer: transfer }); fireEvent.drop(row('CertC adhoc pending A'), { dataTransfer: transfer })
  expect(api.reorder).not.toHaveBeenCalled()
})
// GET /jobs lists by queue_position (now serialized) and the Jobs page defaults to that queue order.
it('F04-4.3-01 after the drop the refetched queue shows B above A', async () => {
  await mount()
  const transfer = { effectAllowed: '', dropEffect: '', setData: vi.fn() }
  fireEvent.dragStart(row('CertC adhoc pending B'), { dataTransfer: transfer }); fireEvent.drop(row('CertC adhoc pending A'), { dataTransfer: transfer })
  await waitFor(() => expect(api.list.mock.calls.length).toBeGreaterThan(1))
  await waitFor(() => { const q = visibleQueue(); expect(q.indexOf('CertC adhoc pending B')).toBeLessThan(q.indexOf('CertC adhoc pending A')) }, { timeout: 500 })
})

// ---- F04-4.3-04 edit modal ----
it('F04-4.3-04 Edit job modal prefills from the wire row and submits the exact body the backend accepted; list shows the persisted edit', async () => {
  await mount()
  fireEvent.click(within(row('CertC adhoc pending B')).getByRole('button', { name: 'Edit job' }))
  expect(screen.getByRole('dialog')).toHaveTextContent('Edit Job')
  expect(field('Item Name *')).toHaveValue('CertC adhoc pending B'); expect(field('Due Date')).toHaveValue('2026-10-20')
  fireEvent.change(field('Item Name *'), { target: { value: 'CertC adhoc pending B edited' } })
  fireEvent.change(field('Priority'), { target: { value: '1' } })
  fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '2' } })
  fireEvent.change(field('Assign Printer'), { target: { value: '9' } })
  fireEvent.change(field('Due Date'), { target: { value: '2026-10-21' } })
  fireEvent.change(field('Notes'), { target: { value: 'Synthetic edited notes' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
  await waitFor(() => expect(api.update).toHaveBeenCalledExactlyOnceWith(202, wire.edit_request))
  await screen.findAllByText('CertC adhoc pending B edited')
  const r = row('CertC adhoc pending B edited')
  expect(within(r).getByText('P1')).toBeInTheDocument(); expect(within(r).getByText('P1S-09')).toBeInTheDocument(); expect(within(r).getByText('Synthetic edited notes')).toBeInTheDocument()
})

// ---- F04-4.4 Print Again ----
it('F04-4.4-01 Print again on a failed row is one click and calls repeat for that exact job', async () => {
  await mount()
  fireEvent.click(within(row('CertC failed clog')).getByRole('button', { name: 'Print again' }))
  await waitFor(() => expect(api.repeat).toHaveBeenCalledExactlyOnceWith(206))
})
it('F04-4.4-01 Print again is offered on a completed row and repeats that exact job', async () => {
  await mount()
  fireEvent.click(within(row('CertC completed overdue')).getByRole('button', { name: 'Print again' }))
  await waitFor(() => expect(api.repeat).toHaveBeenCalledExactlyOnceWith(205))
})

// ---- F04-4.5-01 failure logging ----
it('F04-4.5-01 failed-row failure dialog offers the backend reasons and submits reason + free-text notes', async () => {
  await mount()
  fireEvent.click(within(row('CertC failed clog')).getByRole('button', { name: 'Add failure reason' }))
  const dialog = screen.getByRole('dialog'); expect(dialog).toHaveTextContent('Print Failure')
  const select = within(dialog).getByLabelText(/Failure Reason/) as HTMLSelectElement
  expect(Array.from(select.options).slice(1).map(o => [o.value, o.textContent])).toEqual(wire.failure_reasons.map((r: any) => [r.value, r.label]))
  expect(within(dialog).getByRole('button', { name: 'Mark as Failed' })).toBeDisabled()
  fireEvent.change(select, { target: { value: wire.failure_request.fail_reason } })
  fireEvent.change(within(dialog).getByLabelText(/Notes/), { target: { value: wire.failure_request.fail_notes } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Mark as Failed' }))
  await waitFor(() => expect(api.failure).toHaveBeenCalledExactlyOnceWith(206, 'clog', 'Synthetic nozzle clog at layer 12'))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})
// JobResponse serializes fail_reason/fail_notes, so the refetched row shows the saved reason.
it('F04-4.5-01 saved failure reason and notes are shown on the queue row after refetch', async () => {
  current = wire.jobs_after
  await mount()
  expect(within(row('CertC failed clog')).getByText(/clog: Synthetic nozzle clog at layer 12/)).toBeInTheDocument()
})
// Mark job failed calls POST /jobs/{id}/fail (captured producer response) and then opens the reason dialog;
// it never goes through /cancel, which the real route refuses for printing jobs.
it('F04-4.5-01 Mark job failed on a printing row fails the job and opens the failure-reason dialog', async () => {
  await mount()
  fireEvent.click(within(row('CertC printing')).getByRole('button', { name: 'Mark job failed' }))
  await waitFor(() => expect(api.fail.mock.calls.map(c => c[0])).toEqual([204]))
  expect(api.cancel).not.toHaveBeenCalled()
  expect(wire.fail_printing_response.status).toBe('failed')
  const dialog = await screen.findByRole('dialog', {}, { timeout: 500 })
  expect(dialog).toHaveTextContent('Print Failure')
  fireEvent.change(within(dialog).getByLabelText(/Failure Reason/), { target: { value: 'clog' } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Mark as Failed' }))
  await waitFor(() => expect(api.failure).toHaveBeenCalledWith(204, 'clog', ''))
})

// ---- F04-4.7 time tracking ----
it('F04-4.7-01/02 completed row shows the estimate and the actual duration from the wire timestamps', async () => {
  await mount()
  const r = row('CertC completed overdue')
  expect(within(r).getByText('2h')).toBeInTheDocument()
  expect(within(r).getByTitle('Actual duration')).toHaveTextContent('2h 30m actual')
  expect(within(row('CertC adhoc pending A')).queryByTitle('Actual duration')).not.toBeInTheDocument()
})

// ---- F04-4.9-01 recently completed ----
it('F04-4.9-01 Recently Completed grid shows the newest 8 terminal outcomes with thumbnails, durations and outcome icons', async () => {
  await mount()
  const section = screen.getByRole('region', { name: 'Recent completed and failed jobs' })
  const cards = within(section).getAllByRole('article')
  expect(cards).toHaveLength(8)
  expect(cards.map(c => c.querySelector('span[title]')?.textContent)).toEqual(wire.recent_terminal.map((j: any) => j.item_name))
  expect(within(cards[0]).getByRole('img', { name: 'Thumbnail for CertC failed clog' }).getAttribute('src')).toBe(wire.recent_terminal[0].thumbnail_data)
  expect(within(cards[1]).getByRole('img', { name: 'Thumbnail for CertC completed overdue' })).toBeInTheDocument()
  expect(within(cards[2]).getByLabelText('Thumbnail unavailable')).toBeInTheDocument()
  expect(within(cards[0]).getByLabelText('Failed')).toBeInTheDocument(); expect(within(cards[1]).getByLabelText('Completed')).toBeInTheDocument()
  expect(cards[1]).toHaveTextContent('P1S-09 · 2.5h'); expect(cards[0]).toHaveTextContent('P1S-10 · 45m'); expect(cards[2]).toHaveTextContent('P1S-09 · 8h')
  expect(cards[1].querySelector('time')).toHaveAttribute('dateTime', '2026-10-11T12:30:00Z')
  expect(within(section).queryByText('CertC history 0')).not.toBeInTheDocument() // 9th..11th oldest excluded
})
