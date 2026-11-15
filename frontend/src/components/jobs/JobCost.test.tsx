import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, expect, it, vi } from 'vitest'
import JobCost, { PricingConfigured } from './JobCost'
import { pricingConfig } from '../../api'
import { jobCost, type JobCost as Cost } from '../../api/jobCost'

vi.mock('../../api', () => ({ pricingConfig: { get: vi.fn() } }))
vi.mock('../../api/jobCost', () => ({ jobCost: { get: vi.fn() } }))

// The figures of tests/test_contracts/test_cost_actual.py job 801: failed 0.45 + success 2.70 + settings 8.50.
const complete: Cost = {
  job_id: 801, configured: true, finished: true, state: 'complete', estimate: 7.5, difference: 4.15, pieces: 2, total: 11.65, unknown: [],
  material: { amount: 2.4, complete: true, spools: [
    { spool_id: 71, name: 'Acme Black PLA', grams: 60, amount: 1.2, provenance: 'measured' },
    { spool_id: 72, name: null, grams: 30, amount: 1.2, provenance: 'measured' }] },
  machine: { amount: 0.75, complete: true, hours: 3, rate_per_hour: 0.25, basis: 'current_rates' },
  from_settings: { amount: 8.5, complete: true, basis: 'settings', labor: 6, packaging: 1, other: 0.5, overhead: 1 },
  failed_attempts: { amount: 0.45, complete: true, count: 1 },
  attempts: [
    { id: 9001, outcome: 'failed', hours: 1, material: 0.2, machine: 0.25, amount: 0.45, complete: true },
    { id: 9002, outcome: 'completed', hours: 2, material: 2.2, machine: 0.5, amount: 2.7, complete: true }],
}
const partial: Cost = {
  ...complete, state: 'partial', total: 5.75, difference: null,
  unknown: [{ part: 'material', reason: 'spool_price_missing', attempt_id: 9005, spool_id: 71 }],
  material: { amount: 0, complete: false, spools: [{ spool_id: 71, name: 'Acme Black PLA', grams: 50, amount: null, provenance: 'measured' }] },
  failed_attempts: { amount: 0, complete: true, count: 0 }, attempts: [],
}

function mount(props: { status?: string; compact?: boolean; rowConfigured?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><PricingConfigured.Provider value={props.rowConfigured ?? true}>
    <JobCost jobId={801} status={props.status ?? 'completed'} compact={props.compact} />
  </PricingConfigured.Provider></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(pricingConfig.get).mockResolvedValue({ configured: true } as never)
  vi.mocked(jobCost.get).mockResolvedValue(complete)
})

it('shows estimate, actual and the difference, with the breakdown on click', async () => {
  mount()
  const panel = await screen.findByRole('region', { name: 'Print cost' })
  expect(panel).toHaveTextContent('Estimate $7.50, actual $11.65')
  expect(panel).toHaveTextContent('$4.15 more than the estimate')
  expect(panel).not.toHaveTextContent('Filament used')
  fireEvent.click(screen.getByRole('button', { name: 'Show breakdown' }))
  expect(panel).toHaveTextContent('Acme Black PLA (spool 71): 60 g, printer reading$1.20')
  expect(panel).toHaveTextContent('Spool (spool 72): 30 g, printer reading$1.20')
  expect(panel).toHaveTextContent('Machine time (recorded hours, at current rates)')
  expect(panel).toHaveTextContent('3 h at $0.25 per hour$0.75')
  expect(panel).toHaveTextContent('From settings (not measured, at current rates)')
  expect(panel).toHaveTextContent('Labour$6.00')
  expect(panel).toHaveTextContent('Failed attempt, 1 h$0.45')
  expect(panel).toHaveTextContent(/failure allowance is not added/)
})

it('says less than the estimate when the print came in under it', async () => {
  vi.mocked(jobCost.get).mockResolvedValue({ ...complete, total: 7, difference: -0.5 })
  mount()
  expect(await screen.findByRole('region', { name: 'Print cost' })).toHaveTextContent('$0.50 less than the estimate')
})

it('shows a partial total as "at least" with the reason and no difference', async () => {
  vi.mocked(jobCost.get).mockResolvedValue(partial)
  mount()
  const panel = await screen.findByRole('region', { name: 'Print cost' })
  expect(panel).toHaveTextContent('Estimate $7.50, actual at least $5.75')
  expect(panel).toHaveTextContent('Not the full cost.')
  expect(panel).toHaveTextContent('A spool had no purchase price when the print started')
  expect(panel).not.toHaveTextContent('than the estimate')
  fireEvent.click(screen.getByRole('button', { name: 'Show breakdown' }))
  expect(panel).toHaveTextContent('Acme Black PLA (spool 71): 50 g, printer readingunknown')
})

it('says no usage was recorded for a job without print attempts', async () => {
  vi.mocked(jobCost.get).mockResolvedValue({ job_id: 801, configured: true, finished: true, state: 'none', estimate: 7.5, difference: null, pieces: 1 })
  mount()
  const panel = await screen.findByRole('region', { name: 'Print cost' })
  expect(panel).toHaveTextContent('Estimate $7.50. No usage recorded, so there is no actual cost.')
  expect(screen.queryByRole('button', { name: 'Show breakdown' })).toBeNull()
})

it('never uses the words profit, margin or COGS', async () => {
  mount()
  const panel = await screen.findByRole('region', { name: 'Print cost' })
  fireEvent.click(screen.getByRole('button', { name: 'Show breakdown' }))
  expect(panel.textContent).not.toMatch(/profit|margin|cogs/i)
})

it('stays hidden until pricing is configured and does not ask for the cost', async () => {
  vi.mocked(pricingConfig.get).mockResolvedValue({ configured: false } as never)
  const { container } = mount()
  await vi.waitFor(() => expect(pricingConfig.get).toHaveBeenCalled())
  expect(container).toBeEmptyDOMElement()
  expect(jobCost.get).not.toHaveBeenCalled()
})

it('renders nothing and asks for nothing while a job is not finished', () => {
  const { container } = mount({ status: 'printing' })
  expect(container).toBeEmptyDOMElement()
  expect(pricingConfig.get).not.toHaveBeenCalled()
})

it('in a row, loads the cost only when the Cost toggle is opened and shows the breakdown with it', async () => {
  mount({ compact: true })
  const toggle = await screen.findByRole('button', { name: 'Cost' })
  expect(jobCost.get).not.toHaveBeenCalled()
  fireEvent.click(toggle)
  const panel = await screen.findByRole('region', { name: 'Print cost' })
  expect(jobCost.get).toHaveBeenCalledWith(801)
  expect(panel).toHaveTextContent('Estimate $7.50, actual $11.65')
  expect(panel).toHaveTextContent('Filament used (recorded)')
  fireEvent.click(screen.getByRole('button', { name: 'Hide cost' }))
  expect(screen.queryByRole('region', { name: 'Print cost' })).toBeNull()
})

it('shows failed and cancelled jobs too', async () => {
  mount({ status: 'failed' })
  expect(await screen.findByRole('region', { name: 'Print cost' })).toBeInTheDocument()
})

it('in a row, stays hidden until the page says pricing is configured', () => {
  const { container } = mount({ compact: true, rowConfigured: false })
  expect(container).toBeEmptyDOMElement()
  expect(pricingConfig.get).not.toHaveBeenCalled()
})


it('explains that sliced quantities do not enter the actual lower bound', async () => {
  vi.mocked(jobCost.get).mockResolvedValue({
    ...partial,
    unknown: [{ part: 'material', reason: 'usage_unmeasured', attempt_id: 9005, spool_id: 71 }],
    material: { amount: 0, complete: false, spools: [
      { spool_id: 71, grams: 50, amount: null, provenance: 'sliced_estimate' },
    ] },
  })
  mount()
  const panel = await screen.findByRole('region', { name: 'Print cost' })
  expect(panel).toHaveTextContent('actual at least $5.75')
  expect(panel).toHaveTextContent('Filament use was estimated from the sliced file, not measured')
  fireEvent.click(screen.getByRole('button', { name: 'Show breakdown' }))
  expect(panel).toHaveTextContent('Spool (spool 71): 50 g, from the sliced fileunknown')
  expect(panel).not.toHaveTextContent('than the estimate')
})
