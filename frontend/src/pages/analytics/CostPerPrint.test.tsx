import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import CostPerPrint, { rangeStart, varianceText } from './CostPerPrint'
import { analytics } from '../../api'
import type { CostPerPrintData } from '../../api/reporting'

const state = vi.hoisted(() => ({ features: ['analytics', 'csv_export'], role: 'operator' }))
vi.mock('../../api', () => ({ analytics: { costPerPrint: vi.fn(), costPerPrintCsv: vi.fn() } }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ hasFeature: (feature: string) => state.features.includes(feature) }) }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 3, role: state.role }) }))

// The rows of tests/test_contracts/test_cost_per_print.py.
const table: CostPerPrintData = {
  configured: true, group: 'model', from: '2026-01-01', to: null, minimum_jobs: 3,
  rows: [
    { id: 101, name: 'Bracket', jobs: 5, jobs_with_full_cost: 3, jobs_partial: 1, jobs_without_usage: 1, pieces: 4,
      average_actual_per_piece: 5.66, average_estimate_per_piece: 5.25, variance_percent: 7.9, failure_share_percent: 2 },
    { id: null, name: null, jobs: 1, jobs_with_full_cost: 1, jobs_partial: 0, jobs_without_usage: 0, pieces: 1,
      average_actual_per_piece: null, average_estimate_per_piece: null, variance_percent: null, failure_share_percent: null },
  ],
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><CostPerPrint days={30} /></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  state.features = ['analytics', 'csv_export']; state.role = 'operator'
  vi.mocked(analytics.costPerPrint).mockResolvedValue(table)
  vi.mocked(analytics.costPerPrintCsv).mockResolvedValue(undefined)
})
afterEach(() => cleanup())

it('shows actual and estimated cost per piece, the difference and the failed share for a model', async () => {
  mount()
  const section = await screen.findByRole('region', { name: 'Cost per print' })
  expect(analytics.costPerPrint).toHaveBeenCalledWith('model', rangeStart(30))
  const row = within(section).getByText('Bracket').closest('tr')!
  expect(row).toHaveTextContent('5 completed (left out: 1 with missing records, 1 with no usage recorded)')
  expect(within(row).getAllByRole('cell').map(cell => cell.textContent).slice(1)).toEqual(['4', '5.66', '5.25', '7.9% over estimate', '2%'])
  expect(within(section).getAllByRole('columnheader').map(cell => cell.textContent)).toEqual(
    ['Model', 'Pieces made', 'Actual cost per piece', 'Estimate per piece', 'Actual vs estimate', 'Spent on failed attempts'])
})

it('shows no average for a group with fewer than three prints with full cost records', async () => {
  mount()
  const row = (await screen.findByText('Prints without a model')).closest('tr')!
  expect(row).toHaveTextContent('Needs 3 completed prints with full cost records (has 1)')
  expect(row).not.toHaveTextContent('$')
})

it('words the difference by its sign', () => {
  expect(varianceText(7.9)).toBe('7.9% over estimate')
  expect(varianceText(-24.5)).toBe('24.5% under estimate')
  expect(varianceText(0)).toBe('Same as estimate')
  expect(varianceText(null)).toBe('—')
})

it('starts the range on the local calendar day, N days back', () => {
  expect(rangeStart(30, new Date(2026, 0, 31, 23, 30))).toBe('2026-01-01')
  expect(rangeStart(7, new Date(2026, 2, 3, 0, 5))).toBe('2026-02-24')
})

it('groups by product on request and exports what is on screen', async () => {
  mount()
  fireEvent.change(await screen.findByLabelText('Group cost per print by'), { target: { value: 'product' } })
  await vi.waitFor(() => expect(analytics.costPerPrint).toHaveBeenLastCalledWith('product', rangeStart(30)))
  expect(await screen.findByRole('columnheader', { name: 'Product' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
  expect(analytics.costPerPrintCsv).toHaveBeenCalledWith('product', rangeStart(30))
})

it.each([{ features: ['analytics'], role: 'operator' }, { features: ['analytics', 'csv_export'], role: 'viewer' }])(
  'offers no export without the CSV entitlement or for a viewer (%o)', async over => {
    Object.assign(state, over)
    mount()
    await screen.findByRole('region', { name: 'Cost per print' })
    expect(screen.queryByRole('button', { name: 'Export CSV' })).toBeNull()
  })

it('shows nothing until pricing is configured', async () => {
  vi.mocked(analytics.costPerPrint).mockResolvedValue({ ...table, configured: false, rows: [] })
  const { container } = mount()
  await vi.waitFor(() => expect(analytics.costPerPrint).toHaveBeenCalled())
  expect(container).toBeEmptyDOMElement()
})

it('shows nothing when the table cannot be loaded (for example below Pro)', async () => {
  vi.mocked(analytics.costPerPrint).mockRejectedValue(new Error('403'))
  const { container } = mount()
  await vi.waitFor(() => expect(analytics.costPerPrint).toHaveBeenCalled())
  expect(container).toBeEmptyDOMElement()
})

it('says so when the period has no completed prints, and never speaks of profit or margin', async () => {
  vi.mocked(analytics.costPerPrint).mockResolvedValue({ ...table, rows: [] })
  mount()
  const section = await screen.findByRole('region', { name: 'Cost per print' })
  expect(section).toHaveTextContent('No completed prints in this period.')
  expect(section.textContent).not.toMatch(/profit|margin|cogs/i)
})

it('scrolls sideways inside its own region on a phone instead of widening the page', async () => {
  mount()
  const scroller = await screen.findByRole('region', { name: 'Cost per print table' })
  expect(scroller.className).toContain('overflow-x-auto')
  expect(scroller).toHaveAttribute('tabindex', '0')
})
