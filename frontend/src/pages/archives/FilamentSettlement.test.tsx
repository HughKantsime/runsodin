import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Profiler } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import FilamentAccounting from './FilamentSettlement'
import { archives, spools } from '../../api'
import type { Archive, SettlementPreview } from '../../types'
const permissions = vi.hoisted(() => ({ allowed: true }))
vi.mock('../../permissions', () => ({ canDo: () => permissions.allowed }))
vi.mock('../../api', () => ({ archives: { settlementPreview: vi.fn(), settleFilament: vi.fn(), get: vi.fn() }, spools: { list: vi.fn() } }))
const cause = { code: 'no_bound_spool', title: 'No spool was bound to the slot this print used', next_step: 'Bind spools on the Printers page; settle this print' }
const item = { id: 1, accounting_state: 'unresolved', settleable: true, accounting_cause: cause } as Archive
let data: SettlementPreview
function mount(content = <FilamentAccounting archive={item} />) { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>{content}</QueryClientProvider>) }
function openForm() {
  mount()
  fireEvent.click(screen.getByRole('button', { name: 'Settle filament' }))
}
async function open() {
  openForm()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeEnabled())
}
beforeEach(() => {
  permissions.allowed = true; vi.clearAllMocks()
  data = { archive_id: 1, job_id: 2, accounting_state: 'unresolved', accounting_reason: 'no_bound_spool', cause, settleable: true, not_settleable: null,
    grams: { measured: 40, sliced_estimate: 42 }, suggested: [{ spool_id: 17, slot_number: 1, grams: 40, spool_label: 'Test spool', remaining_g: 100 }], manual_usage: [], prior_usage: null, balance_set_after_print: [] }
  vi.mocked(archives.settlementPreview).mockImplementation(async () => data)
  vi.mocked(spools.list).mockResolvedValue([{ id: 17, color_name: 'Blue' }] as any)
  vi.mocked(archives.settleFilament).mockResolvedValue({ deductions: [] })
  vi.mocked(archives.get).mockResolvedValue({ ...item, accounting_state: 'accounted', accounting_settled: { by: 'Operator', at: '2026-11-16T12:00:00Z' }, consumption: [{ spool_id: 17, grams_used: 40 }] })
})
afterEach(cleanup)
test.each([
  ['added', 'This spool was added after this print ended'],
  ['unknown_balance_time', 'The balance time cannot be read'],
  ['edited', 'Its balance was set during or after this print and may already include this use'],
])('explains %s balance evidence and requires an override to deduct', async (how, message) => {
  data.balance_set_after_print = [{ spool_id: 17, at: how === 'added' ? '2026-11-16T12:00:00Z' : null, how }]
  openForm()
  await screen.findByText(new RegExp(message))
  fireEvent.change(screen.getByLabelText('Deduction'), { target: { value: '' } })
  expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox', { name: 'Deduct anyway from spool 17' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await waitFor(() => expect(archives.settleFilament).toHaveBeenCalledWith(1, expect.objectContaining({ deduct_despite_later_balance: [17] })))
})
test('cause is readable and settlement button respects permission', () => {
  permissions.allowed = false; mount(); expect(screen.getByText(cause.title)).toBeInTheDocument(); expect(screen.queryByRole('button', { name: 'Settle filament' })).not.toBeInTheDocument()
})
test('Confirm stays disabled until the suggested spool has its own preview', async () => {
  const pending = new Promise<SettlementPreview>(() => {})
  vi.mocked(archives.settlementPreview).mockImplementation(async (_id, ids) => ids?.length ? pending : data)
  const enabledStates: boolean[] = []
  mount(<Profiler id="settlement" onRender={() => {
    const confirm = screen.queryByRole<HTMLButtonElement>('button', { name: 'Confirm settlement' })
    if (confirm) enabledStates.push(!confirm.disabled)
  }}><FilamentAccounting archive={item} /></Profiler>)
  fireEvent.click(screen.getByRole('button', { name: 'Settle filament' }))
  await screen.findByText('Refreshing filament details…')
  expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeDisabled()
  expect(enabledStates).not.toContain(true)
})
test('prefills and settles a spool, then shows who settled it', async () => {
  await open(); expect(screen.getByLabelText('Grams 1')).toHaveValue('40.0')
  fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await screen.findByText(/Filament settled by Operator/)
  expect(screen.getByRole('status')).toHaveFocus()
  expect(archives.settleFilament).toHaveBeenCalledWith(1, expect.objectContaining({ entries: [{ spool_id: 17, grams: 40 }], no_deduction: null }))
})
test('every manual row must be reviewed and counted use is submitted', async () => {
  data.manual_usage = [{ usage_id: 9, spool_id: 17, grams: 10, used_at: '2026-11-16T11:00:00Z', notes: 'Manual use' }]
  openForm()
  const yes = await screen.findByRole('radio', { name: 'Yes' })
  const confirm = screen.getByRole('button', { name: 'Confirm settlement' })
  expect(confirm).toBeDisabled()
  fireEvent.click(yes)
  await waitFor(() => expect(confirm).toBeEnabled())
  fireEvent.click(confirm)
  await waitFor(() => expect(archives.settleFilament).toHaveBeenCalledWith(1, expect.objectContaining({ counted_usage_ids: [9] })))
})
test('no further deduction plus counted use explains how to enter a zero-net settlement', async () => {
  data.manual_usage = [{ usage_id: 9, spool_id: 17, grams: 40, used_at: '2026-11-16T11:00:00Z', notes: '' }]
  openForm()
  await screen.findByLabelText('Grams 1')
  fireEvent.change(screen.getByLabelText('Deduction'), { target: { value: 'recorded_by_hand' } }); fireEvent.click(screen.getByLabelText('Yes'))
  expect(screen.getByText(/only the difference is deducted/)).toBeInTheDocument(); expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeDisabled()
})
test('no-job explanation and no-deduction flow', async () => {
  data.job_id = null; await open(); expect(screen.getByText(/Manual use cannot be matched/)).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Deduction'), { target: { value: 'recorded_by_hand' } }); fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await waitFor(() => expect(archives.settleFilament).toHaveBeenCalledWith(1, expect.objectContaining({ entries: [], no_deduction: 'recorded_by_hand' })))
})
test('later balance defaults to no deduction and requires explicit override', async () => {
  data.balance_set_after_print = [{ spool_id: 17, at: '2026-11-16T12:00:00Z', how: 'weighed' }]
  await open(); expect(screen.getByLabelText('Deduction')).toHaveValue('balance_already_updated')
  fireEvent.click(screen.getByRole('checkbox')); expect(screen.getByLabelText('Deduction')).toHaveValue('')
  await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await waitFor(() => expect(archives.settleFilament).toHaveBeenCalledWith(1, expect.objectContaining({ deduct_despite_later_balance: [17] })))
})
test('server refusal is shown and refreshed prior usage can be accepted', async () => {
  vi.mocked(archives.get).mockResolvedValue(item)
  vi.mocked(archives.settleFilament).mockImplementationOnce(async () => {
    data = { ...data, prior_usage: 'marker' }; throw Object.assign(new Error('Filament was already deducted. Keep the existing deduction.'), { status: 409 })
  })
  await open(); fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await screen.findByText('Filament was already deducted. Keep the existing deduction.')
  await waitFor(() => expect(screen.getByRole('button', { name: 'Keep the existing deduction' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Keep the existing deduction' }))
  await waitFor(() => expect(archives.settleFilament).toHaveBeenLastCalledWith(1, expect.objectContaining({ entries: [], no_deduction: 'accept_existing' })))
})
test('committed settlement remains clear if archive refresh fails', async () => {
  vi.mocked(archives.get).mockRejectedValueOnce(new Error('Unavailable'))
  await open(); fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await screen.findByText('Filament was settled, but the updated archive could not be loaded.')
  expect(screen.queryByRole('button', { name: 'Confirm settlement' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh archive' })); await screen.findByText(/Filament settled by Operator/)
  expect(archives.settleFilament).toHaveBeenCalledTimes(1)
})
test('ineligible history explains refusal without a confirm button', async () => {
  data.settleable = false; data.not_settleable = 'Imported history has no print to settle.'
  openForm(); await screen.findByText(data.not_settleable)
  expect(screen.queryByRole('button', { name: 'Confirm settlement' })).not.toBeInTheDocument()
})

test('prior usage discovered while a blank spool is selected remains confirmable', async () => {
  data.suggested = []
  data.grams = { measured: null, sliced_estimate: null }
  vi.mocked(archives.settlementPreview).mockImplementation(async (_id, ids) => ids?.length ? { ...data, prior_usage: 'marker' } : data)
  openForm()
  const picker = await screen.findByLabelText('Spool 1')
  await screen.findByRole('option', { name: /^#17\b/ })
  fireEvent.change(picker, { target: { value: '17' } })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Keep the existing deduction' })).toBeEnabled())
})

test('mixed later balances never default to discarding the other spool', async () => {
  data.suggested.push({ spool_id: 18, slot_number: 2, grams: 20, spool_label: 'Other', remaining_g: 100 })
  data.balance_set_after_print = [{ spool_id: 17, at: '2026-11-16T12:00:00Z', how: 'weighed' }]
  vi.mocked(spools.list).mockResolvedValue([{ id: 17 }, { id: 18 }] as any)
  openForm()
  await waitFor(() => expect(screen.getByLabelText('Spool 2')).toHaveValue('18'))
  expect(screen.getByLabelText('Deduction')).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox'))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await waitFor(() => expect(archives.settleFilament).toHaveBeenCalledWith(1, expect.objectContaining({ entries: [{ spool_id: 17, grams: 40 }, { spool_id: 18, grams: 20 }] })))
})
test.each(['loading', 'failure', 'missing'])('cannot submit a hidden spool while options are %s', async state => {
  if (state === 'loading') vi.mocked(spools.list).mockReturnValue(new Promise(() => {}))
  if (state === 'failure') vi.mocked(spools.list).mockRejectedValue(new Error('Unavailable'))
  if (state === 'missing') vi.mocked(spools.list).mockResolvedValue([])
  openForm()
  expect(await screen.findByRole('button', { name: 'Confirm settlement' })).toBeDisabled()
  await screen.findByText(/spool.*load|available spool|Loading spools/i)
  expect(archives.settleFilament).not.toHaveBeenCalled()
})
test('prior usage accepts a truthful Yes for additional manual use', async () => {
  data.prior_usage = 'marker'
  data.manual_usage = [{ usage_id: 9, spool_id: 17, grams: 10, used_at: '2026-11-16T11:00:00Z', notes: '' }]
  openForm()
  fireEvent.click(await screen.findByLabelText('Yes'))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Keep the existing deduction' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Keep the existing deduction' }))
  await waitFor(() => expect(archives.settleFilament).toHaveBeenCalledWith(1, expect.objectContaining({ counted_usage_ids: [9], no_deduction: 'accept_existing' })))
})
test.each(['refused', 'lost'])('refreshes the archive after a %s response and Back cannot reopen stale settlement', async kind => {
  const error = Object.assign(new Error(kind === 'lost' ? 'Network error' : 'This print is already settled'), kind === 'refused' ? { status: 409 } : {})
  vi.mocked(archives.settleFilament).mockRejectedValue(error)
  await open(); fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await waitFor(() => expect(archives.get).toHaveBeenCalledWith(1))
  if (kind === 'lost') await screen.findByText('ODIN could not confirm the result. Reload this print before trying again.')
  await waitFor(() => expect(screen.getByRole('button', { name: 'Back' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Back' }))
  expect(screen.queryByRole('button', { name: 'Settle filament' })).not.toBeInTheDocument()
})
test('removing a suggested spool removes its override', async () => {
  data.suggested.push({ spool_id: 18, slot_number: 2, grams: 20, spool_label: 'Other', remaining_g: 100 })
  data.balance_set_after_print = [{ spool_id: 17, at: '2026-11-16T12:00:00Z', how: 'weighed' }]
  vi.mocked(spools.list).mockResolvedValue([{ id: 17 }, { id: 18 }] as any)
  openForm()
  fireEvent.change(await screen.findByLabelText('Deduction'), { target: { value: '' } })
  fireEvent.click(await screen.findByRole('button', { name: 'Remove spool 1' }))
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
})
test.each([[.1, .2, '0.3'], [.04, .04, '0.1']] as const)('rounds accumulated suggestions %s + %s and accepts comma decimals', async (first, second, expected) => {
  data.suggested[0].grams = first
  data.suggested.push({ ...data.suggested[0], grams: second }, { ...data.suggested[0], grams: null })
  await open(); expect(screen.getByLabelText('Grams 1')).toHaveValue(expected)
  fireEvent.change(screen.getByLabelText('Grams 1'), { target: { value: '40,5' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await waitFor(() => expect(archives.settleFilament).toHaveBeenCalledWith(1, expect.objectContaining({ entries: [{ spool_id: 17, grams: 40.5 }] })))
})
test.each(['10001', 'abc', '0'])('invalid grams %s have a plain explanation', async grams => {
  await open(); fireEvent.change(screen.getByLabelText('Grams 1'), { target: { value: grams } })
  expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeDisabled()
  expect(screen.getByText(/Enter grams greater than zero and no more than 10000/)).toBeInTheDocument()
})
test('duplicate spools are refused in the form', async () => {
  await open(); fireEvent.click(screen.getByRole('button', { name: 'Add spool' }))
  fireEvent.change(screen.getByLabelText('Spool 2'), { target: { value: '17' } })
  fireEvent.change(screen.getByLabelText('Grams 2'), { target: { value: '5' } })
  expect(screen.getByText(/Choose each spool only once/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeDisabled()
})
test('preview failure offers retry and Back', async () => {
  vi.mocked(archives.settlementPreview).mockRejectedValue(new Error('Unavailable'))
  openForm()
  await screen.findByText(/Could not load filament details/)
  expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Confirm settlement' })).not.toBeInTheDocument()
})
test('uses a named dialog and restores focus on Back', async () => {
  mount(); const trigger = screen.getByRole('button', { name: 'Settle filament' }); trigger.focus(); fireEvent.click(trigger)
  const dialog = await screen.findByRole('dialog', { name: 'Settle filament' })
  expect(dialog).toContainElement(document.activeElement as HTMLElement)
  fireEvent.click(screen.getByRole('button', { name: 'Back' }))
  expect(trigger).toHaveFocus()
})
test('settled line distinguishes total use from deducted now and shows no-deduction reason', () => {
  render(<QueryClientProvider client={new QueryClient()}><FilamentAccounting archive={{ ...item, accounting_state: 'accounted', accounting_settled: { by: 'Operator', at: '2026-11-16T12:00:00Z' }, consumption: [{ spool_id: 17, grams_used: 40, deducted_now_g: 30 }, { spool_id: null, grams_used: null, reason: 'recorded_by_hand' }, { spool_id: null, grams_used: null, reason: 'accept_existing' }] }} /></QueryClientProvider>)
  expect(screen.getByText('40.0 g from spool 17; 30.0 g deducted now')).toBeInTheDocument()
  expect(screen.getByText('No further deduction: recorded by hand')).toBeInTheDocument()
  expect(screen.getByText('No further deduction: existing deduction kept')).toBeInTheDocument()
})
test('picker identifies filament by brand and name', async () => {
  vi.mocked(spools.list).mockResolvedValue([{ id: 17, filament_brand: 'Brand', filament_name: 'PLA', color_name: 'Blue' }] as any)
  await open(); expect(screen.getByRole('option', { name: '#17 Brand PLA · Blue' })).toBeInTheDocument()
})
test('Back cannot offer settlement when the failure refresh also fails', async () => {
  vi.mocked(archives.settleFilament).mockRejectedValue(new Error('Connection lost'))
  vi.mocked(archives.get).mockRejectedValue(new Error('Still offline'))
  await open(); fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await waitFor(() => expect(archives.get).toHaveBeenCalled())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Back' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Back' }))
  expect(screen.queryByRole('button', { name: 'Settle filament' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Reload this print' })).toBeInTheDocument()
})
