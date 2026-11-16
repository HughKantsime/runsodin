import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import FilamentAccounting from './FilamentSettlement'
import { archives, spools } from '../../api'
import type { Archive, SettlementPreview } from '../../types'
const permissions = vi.hoisted(() => ({ allowed: true }))
vi.mock('../../permissions', () => ({ canDo: () => permissions.allowed }))
vi.mock('../../api', () => ({ archives: { settlementPreview: vi.fn(), settleFilament: vi.fn(), get: vi.fn() }, spools: { list: vi.fn() } }))
const cause = { code: 'no_bound_spool', title: 'No spool was bound to the slot this print used', next_step: 'Bind spools on the Printers page; settle this print' }
const item = { id: 1, accounting_state: 'unresolved', accounting_cause: cause } as Archive
let data: SettlementPreview
function mount() { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><FilamentAccounting archive={item} /></QueryClientProvider>) }
async function open() { mount(); fireEvent.click(screen.getByRole('button', { name: 'Settle filament' })); await screen.findByRole('button', { name: 'Confirm settlement' }); if (!data.manual_usage.length) await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeEnabled()) }
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
test('cause is readable and settlement button respects permission', () => {
  permissions.allowed = false; mount(); expect(screen.getByText(cause.title)).toBeInTheDocument(); expect(screen.queryByRole('button', { name: 'Settle filament' })).not.toBeInTheDocument()
})
test('prefills and settles a spool, then shows who settled it', async () => {
  await open(); expect(screen.getByLabelText('Grams 1')).toHaveValue(40)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await screen.findByText(/Filament settled by Operator/)
  expect(archives.settleFilament).toHaveBeenCalledWith(1, expect.objectContaining({ entries: [{ spool_id: 17, grams: 40 }], no_deduction: null }))
})
test('every manual row must be reviewed and counted use is submitted', async () => {
  data.manual_usage = [{ usage_id: 9, spool_id: 17, grams: 10, used_at: '2026-11-16T11:00:00Z', notes: 'Manual use' }]
  await open(); expect(screen.getByRole('button', { name: 'Confirm settlement' })).toBeDisabled()
  fireEvent.click(screen.getByLabelText('Yes')); fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await waitFor(() => expect(archives.settleFilament).toHaveBeenCalledWith(1, expect.objectContaining({ counted_usage_ids: [9] })))
})
test('no further deduction plus counted use explains how to enter a zero-net settlement', async () => {
  data.manual_usage = [{ usage_id: 9, spool_id: 17, grams: 40, used_at: '2026-11-16T11:00:00Z', notes: '' }]
  await open(); fireEvent.change(screen.getByLabelText('Deduction'), { target: { value: 'recorded_by_hand' } }); fireEvent.click(screen.getByLabelText('Yes'))
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
  fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await waitFor(() => expect(archives.settleFilament).toHaveBeenCalledWith(1, expect.objectContaining({ deduct_despite_later_balance: [17] })))
})
test('server refusal is shown and refreshed prior usage can be accepted', async () => {
  vi.mocked(archives.settleFilament).mockImplementationOnce(async () => {
    data = { ...data, prior_usage: 'marker' }; throw new Error('Filament was already deducted. Keep the existing deduction.')
  })
  await open(); fireEvent.click(screen.getByRole('button', { name: 'Confirm settlement' }))
  await screen.findByText('Filament was already deducted. Keep the existing deduction.')
  fireEvent.click(await screen.findByRole('button', { name: 'Keep the existing deduction' }))
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
  mount(); fireEvent.click(screen.getByRole('button', { name: 'Settle filament' })); await screen.findByText(data.not_settleable)
  expect(screen.queryByRole('button', { name: 'Confirm settlement' })).not.toBeInTheDocument()
})

test('prior usage discovered while a blank spool is selected remains confirmable', async () => {
  data.suggested = []
  data.grams = { measured: null, sliced_estimate: null }
  vi.mocked(archives.settlementPreview).mockImplementation(async (_id, ids) => ids?.length ? { ...data, prior_usage: 'marker' } : data)
  mount(); fireEvent.click(screen.getByRole('button', { name: 'Settle filament' }))
  const picker = await screen.findByLabelText('Spool 1')
  fireEvent.change(picker, { target: { value: '17' } })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Keep the existing deduction' })).toBeEnabled())
})
