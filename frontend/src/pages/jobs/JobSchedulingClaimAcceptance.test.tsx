import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import Jobs from './Jobs'
import Timeline from './Timeline'
const api = vi.hoisted(() => ({ role: 'operator', grants: [] as string[], list: vi.fn(), update: vi.fn(), reorder: vi.fn(), preset: vi.fn(), schedule: vi.fn(), resubmit: vi.fn(), bulk: vi.fn(), timeline: vi.fn() }))
vi.mock('../../api', () => ({
  jobs: { list: api.list, reorder: api.reorder, create: vi.fn(), update: api.update, start: vi.fn(), complete: vi.fn(), cancel: vi.fn(), delete: vi.fn(), repeat: vi.fn(), dispatch: vi.fn() },
  models: { list: vi.fn().mockResolvedValue([{ id: 9, name: 'Fixture model', build_time_hours: 2, required_colors: ['black'] }]) }, modelRevisions: { list: vi.fn().mockResolvedValue([]) },
  printers: { list: vi.fn().mockResolvedValue([{ id: 7, name: 'Printer Alpha', is_active: true }, { id: 8, name: 'Printer Beta', is_active: true }]) },
  getApprovalSetting: vi.fn().mockResolvedValue({ require_approval: true }), presets: { list: vi.fn().mockResolvedValue([{ id: 20, name: 'Two hour fixture', item_name: 'Fixture preset part' }]), create: api.preset, schedule: api.schedule, delete: vi.fn() }, scheduler: { run: vi.fn() }, bulkOps: { jobs: api.bulk },
  approveJob: vi.fn(), rejectJob: vi.fn(), resubmitJob: api.resubmit, updateJobFailure: vi.fn(), timeline: { get: api.timeline }, jobActions: { move: vi.fn() }, printJobs: { get: vi.fn() },
}))
vi.mock('../../permissions', () => ({ canDo: (action: string) => api.role === 'operator' || api.role === 'admin' || api.grants.includes(action), getCurrentUser: () => ({ id: 3, role: api.role }) }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 17 }) }))
const base = { priority: 3, quantity: 1, duration_hours: 2, colors_list: [], hold: false, is_locked: false, order_item_id: null, charged_to_org_id: 17 }
const jobs = [
  { ...base, id: 1, item_name: 'Adhoc pending', status: 'pending' },
  { ...base, id: 2, item_name: 'Order scheduled', status: 'scheduled', order_item_id: 81 },
  { ...base, id: 3, item_name: 'Approval fixture', status: 'submitted', submitted_by: 3 },
  { ...base, id: 4, item_name: 'Own rejected', status: 'rejected', submitted_by: 3, rejected_reason: 'Use less material' },
  { ...base, id: 5, item_name: 'Foreign rejected', status: 'rejected', submitted_by: 4 },
]
const clients: QueryClient[] = []
function Route() { const l = useLocation(); return <output data-testid="query">{l.search}</output> }
function show(node: React.ReactNode) { const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); c.setQueryData(['auth', 'me'], { id:3, role:api.role, group_id:17 }); clients.push(c); return render(<QueryClientProvider client={c}><MemoryRouter><Route />{node}</MemoryRouter></QueryClientProvider>) }
async function queue() { const view = show(<Jobs />); await screen.findByText('Adhoc pending'); return view }
function row(name: string) { return screen.getByText(name, { exact: true }).closest('tr')! }
function field(label: string) { const l = within(screen.getByRole('dialog')).getByText(label, { selector: 'label', exact: true }); return l.parentElement!.querySelector('input,select,textarea')! }
beforeEach(() => {
  api.update.mockResolvedValue({})
  vi.clearAllMocks(); api.role = 'operator'; api.grants = []; api.list.mockResolvedValue(jobs); api.reorder.mockResolvedValue({}); api.preset.mockResolvedValue({ id: 30 }); api.schedule.mockResolvedValue({}); api.resubmit.mockResolvedValue({})
  vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-10-12T12:00:00Z'))
  api.timeline.mockImplementation(async start => { const d = new Date(start); return { slots: [
    { job_id: 11, printer_id: 7, printer_name: 'Printer Alpha', item_name: 'Alpha timeline job', status: 'scheduled', start: new Date(d.getTime()+3600000).toISOString(), end: new Date(d.getTime()+10800000).toISOString() },
    { job_id: 12, printer_id: 8, printer_name: 'Printer Beta', item_name: 'Beta timeline job', status: 'scheduled', start: new Date(d.getTime()+7200000).toISOString(), end: new Date(d.getTime()+14400000).toISOString() },
  ] } })
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.useRealTimers(); vi.restoreAllMocks() })
it('F04-4.3-02 actual tabs filter order versus ad-hoc rows and persist type in URL', async () => {
  await queue(); expect(screen.getByRole('button', { name: /All Jobs/ })).toHaveTextContent('5')
  fireEvent.click(screen.getByRole('button', { name: /Order Jobs/ })); expect(screen.getByText('Order scheduled')).toBeInTheDocument(); expect(screen.queryByText('Adhoc pending')).not.toBeInTheDocument(); expect(screen.getByTestId('query')).toHaveTextContent('type=order')
  fireEvent.click(screen.getByRole('button', { name: /Ad-hoc/ })); expect(screen.getByText('Adhoc pending')).toBeInTheDocument(); expect(screen.queryByText('Order scheduled')).not.toBeInTheDocument()
})
it('F04-4.3-02 status query preserves organization scope and case-insensitive name search filters visible rows', async () => {
  await queue(); fireEvent.change(screen.getByRole('combobox', { name: 'Filter jobs by status' }), { target: { value: 'pending' } }); await waitFor(() => expect(api.list).toHaveBeenCalledWith('pending',17))
  fireEvent.change(screen.getByPlaceholderText('Search jobs...'), { target: { value: 'ADHOC' } }); expect(screen.getByText('Adhoc pending')).toBeInTheDocument(); expect(screen.queryByText('Order scheduled')).not.toBeInTheDocument()
})
it('F04-4.3-01 operator drags a pending job onto scheduled job and submits exact reordered IDs', async () => {
  await queue(); const transfer = { effectAllowed: '', dropEffect: '', setData: vi.fn() }
  expect(row('Adhoc pending')).toHaveAttribute('draggable','true'); fireEvent.dragStart(row('Adhoc pending'), { dataTransfer: transfer }); fireEvent.dragOver(row('Order scheduled'), { dataTransfer: transfer }); fireEvent.drop(row('Order scheduled'), { dataTransfer: transfer })
  await waitFor(() => expect(api.reorder).toHaveBeenCalledWith([2,1])); expect(transfer.setData).toHaveBeenCalledWith('text/plain',1)
})
it('F04-4.3-01 CLAIM GAP: viewer has no queue reorder handles or draggable rows', async () => {
  api.role = 'viewer'; await queue(); expect(row('Adhoc pending')).not.toHaveAttribute('draggable','true')
})
it('F04-4.6-03 operator sees approval badge count and tab restricts rows to submitted jobs', async () => {
  await queue(); const tab = screen.getByRole('button', { name: /Awaiting Approval/ }); expect(tab).toHaveTextContent('1'); fireEvent.click(tab)
  expect(screen.getByText('Approval fixture')).toBeInTheDocument(); expect(screen.queryByText('Adhoc pending')).not.toBeInTheDocument(); expect(screen.getByRole('button', { name: 'Approve job' })).toBeInTheDocument()
})
it('F04-4.6-03 observes viewer approval tab visibility without inferring an exclusive role requirement', async () => {
  api.role = 'viewer'; await queue(); expect(screen.getByRole('button', { name: /Awaiting Approval/ })).toBeInTheDocument(); expect(screen.queryByRole('button', { name: 'Approve job' })).not.toBeInTheDocument()
})
it('F04-4.1-03/F04-4.6-05 original submitter can click Resubmit for exact rejected job', async () => {
  api.role = 'viewer'; api.grants = ['jobs.resubmit','jobs.edit']; await queue(); fireEvent.click(within(row('Own rejected')).getByRole('button', { name: 'Resubmit job' })); await waitFor(() => expect(api.resubmit.mock.calls[0][0]).toBe(4))
})
it('F04-4.1-03 CLAIM GAP: original-submitter resubmit control is absent on another user rejection', async () => {
  api.role = 'viewer'; api.grants = ['jobs.resubmit','jobs.edit']; await queue(); expect(within(row('Foreign rejected')).queryByRole('button', { name: 'Resubmit job' })).not.toBeInTheDocument()
})
it('F04-4.6-05 CLAIM GAP: original submitter can edit the rejected job before resubmitting', async () => {
  api.role = 'viewer'; api.grants = ['jobs.resubmit','jobs.edit']; await queue(); expect(within(row('Own rejected')).getByRole('button', { name: 'Edit job' })).toBeInTheDocument()
})
it('rejected viewer correction sends only submission fields and does not resubmit', async () => {
  api.role = 'viewer'; api.grants = []; await queue()
  expect(within(row('Foreign rejected')).queryByRole('button', { name: 'Edit job' })).not.toBeInTheDocument()
  fireEvent.click(within(row('Own rejected')).getByRole('button', { name: 'Edit job' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Notes' }), { target: { value: 'Corrected notes' } })
  expect(screen.queryByText('Assign Printer')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Save correction' }))
  await waitFor(() => expect(api.update).toHaveBeenCalledWith(4, { item_name:'Own rejected', quantity:1, notes:'Corrected notes', due_date:null }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(api.resubmit).not.toHaveBeenCalled()
})
it('failed rejected correction keeps the entered details and modal open', async () => {
  api.role = 'viewer'; api.update.mockRejectedValue(new Error('Submission changed'))
  await queue(); fireEvent.click(within(row('Own rejected')).getByRole('button', { name:'Edit job' }))
  fireEvent.change(screen.getByRole('textbox', { name:'Notes' }), { target:{ value:'Keep these notes' } })
  fireEvent.click(screen.getByRole('button', { name:'Save correction' }))
  expect(await within(screen.getByRole('dialog')).findByRole('alert')).toHaveTextContent('Submission changed')
  expect(screen.getByRole('textbox', { name:'Notes' })).toHaveValue('Keep these notes')
})
it('changed current actor cannot send a correction from an old modal', async () => {
  api.role = 'viewer'; await queue()
  fireEvent.click(within(row('Own rejected')).getByRole('button', { name:'Edit job' }))
  clients[clients.length - 1].setQueryData(['auth','me'], { id:3, role:'operator', group_id:17 })
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(api.update).not.toHaveBeenCalled()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})
it('account change retires an in-flight correction and suppresses its late UI effect', async () => {
  let finish!: (value: any) => void
  api.role = 'viewer'; api.update.mockImplementation(() => new Promise(resolve => { finish = resolve }))
  await queue(); fireEvent.click(within(row('Own rejected')).getByRole('button', { name:'Edit job' }))
  fireEvent.click(screen.getByRole('button', { name:'Save correction' }))
  await waitFor(() => expect(api.update).toHaveBeenCalledTimes(1))
  clients[clients.length - 1].setQueryData(['auth','me'], { id:4, role:'viewer' })
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  const count = api.list.mock.calls.length
  finish({})
  await new Promise(resolve => setTimeout(resolve, 0))
  expect(api.list.mock.calls.length).toBe(count)
  expect(api.resubmit).not.toHaveBeenCalled()
})
it('cache-only account switch before opening cannot borrow the old singleton owner', async () => {
  api.role = 'viewer'; await queue()
  clients[clients.length - 1].setQueryData(['auth','me'], { id:4, role:'viewer', group_id:17 })
  await waitFor(() => expect(within(row('Own rejected')).queryByRole('button', { name:'Edit job' })).not.toBeInTheDocument())
  expect(api.update).not.toHaveBeenCalled()
})
it('canceling a correction sends no PATCH', async () => {
  api.role = 'viewer'; await queue()
  fireEvent.click(within(row('Own rejected')).getByRole('button', { name:'Edit job' }))
  const cancel = within(screen.getByRole('dialog')).getByRole('button', { name:'Cancel' })
  expect(cancel).toHaveAttribute('type', 'button')
  fireEvent.click(cancel)
  expect(api.update).not.toHaveBeenCalled()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})
it('F04-4.2-08 quick schedule invokes preset by ID and create form saves a named normalized preset', async () => {
  await queue(); fireEvent.click(screen.getByTitle('Schedule: Fixture preset part')); await waitFor(() => expect(api.schedule.mock.calls[0][0]).toBe(20))
  fireEvent.click(screen.getByRole('button', { name: 'New Job' })); fireEvent.change(field('Model (optional)'), { target: { value: '9' } }); fireEvent.change(field('Printer Tags (optional)'), { target: { value: ' Lab, Production, ' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save Preset' })); fireEvent.change(screen.getByPlaceholderText('Preset name'), { target: { value: ' Fixture two hours ' } }); fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
  await waitFor(() => expect(api.preset.mock.calls[0][0]).toEqual({ name: 'Fixture two hours', model_id: 9, item_name: 'Fixture model', priority: 3, duration_hours: 2, colors_required: 'black', required_tags: ['Lab','Production'], notes: null }))
})
it('F04-4.8-01 timeline renders assigned job blocks on each actual printer row', async () => {
  const view = show(<Timeline />); await screen.findAllByText('Alpha timeline job')
  const a = view.container.querySelector('.timeline-grid[data-printer-id="7"]')!; const b = view.container.querySelector('.timeline-grid[data-printer-id="8"]')!
  expect(a).toHaveTextContent('Alpha timeline job'); expect(a).not.toHaveTextContent('Beta timeline job'); expect(b).toHaveTextContent('Beta timeline job')
})
it('F04-4.8-02 horizontal time axis maps hour offset/duration into block geometry and responds to zoom', async () => {
  const view = show(<Timeline />); await screen.findAllByText('Alpha timeline job')
  const grid = view.container.querySelector('.timeline-grid[data-printer-id="7"]')!; const block = within(grid as HTMLElement).getByText('Alpha timeline job').parentElement!.parentElement!
  expect(block).toHaveStyle({ left: '80px', width: '158px' }); expect(screen.getAllByText('01:00').length).toBeGreaterThan(0)
  fireEvent.click(view.container.querySelector('.lucide-zoom-in')!.closest('button')!); expect(block).toHaveStyle({ left: '100px', width: '198px' })
})

it('F04-4.3-01 CLAIM GAP: viewer drag does not submit a reorder request', async () => {
  api.role = 'viewer'; await queue(); const transfer = { effectAllowed: '', dropEffect: '', setData: vi.fn() }
  fireEvent.dragStart(row('Adhoc pending'), { dataTransfer: transfer }); fireEvent.drop(row('Order scheduled'), { dataTransfer: transfer }); expect(api.reorder).not.toHaveBeenCalled()
})

it('bulk priority toolbar sends canonical set_priority for the selected row', async () => {
  await queue(); fireEvent.click(within(row('Adhoc pending')).getByRole('checkbox')); fireEvent.click(screen.getByRole('button', { name: 'Priority 1' }));
  await waitFor(() => expect(api.bulk).toHaveBeenCalledExactlyOnceWith([1], 'set_priority', { priority: 1 }));
})
