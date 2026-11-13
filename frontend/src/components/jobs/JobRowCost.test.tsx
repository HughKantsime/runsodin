import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import JobRow, { JobCardList } from './JobRow'
import { PricingConfigured } from './JobCost'
import { jobCost } from '../../api/jobCost'

vi.mock('../../api/jobCost', () => ({ jobCost: { get: vi.fn() } }))
vi.mock('./OutcomeCorrection', () => ({ default: () => null }))

const job = (over = {}) => ({ id: 801, status: 'completed', printer_id: 9, printer: { name: 'P1S-A' }, priority: 3, quantity: 1, hold: false, is_locked: false, ...over })

function mount(j: any, { configured = true, phone = false } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const row = phone
    ? <JobCardList jobs={[j]} isLoading={false} emptyText="" selectedIds={new Set()} onToggleSelect={vi.fn()} onSelectAll={vi.fn()} allSelected={false} onAction={vi.fn()} printers={[]} />
    : <table><tbody><JobRow job={j} onAction={vi.fn()} isSelected={false} onToggleSelect={vi.fn()} /></tbody></table>
  return render(<QueryClientProvider client={client}><PricingConfigured.Provider value={configured}>{row}</PricingConfigured.Provider></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(jobCost.get).mockResolvedValue({ job_id: 801, configured: true, finished: true, state: 'partial', estimate: 7.5, difference: null, pieces: 1, total: 5.75,
    unknown: [{ part: 'material', reason: 'usage_not_recorded', attempt_id: 1, spool_id: null }],
    material: { amount: 0, complete: false, spools: [] }, attempts: [] })
})
afterEach(() => cleanup())

it.each([false, true])('a completed row offers its cost and loads it only when opened (phone layout: %s)', async phone => {
  mount(job(), { phone })
  expect(jobCost.get).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Cost' }))
  const panel = await screen.findByRole('region', { name: 'Print cost' })
  expect(jobCost.get).toHaveBeenCalledWith(801)
  expect(panel).toHaveTextContent('Estimate $7.50, actual at least $5.75')
  expect(panel).toHaveTextContent('Filament use was not recorded for a print attempt')
})

it.each(['pending', 'scheduled', 'printing', 'submitted', 'rejected'])('a %s row has no cost control', status => {
  mount(job({ status }))
  expect(screen.queryByRole('button', { name: 'Cost' })).toBeNull()
})

it('a completed row has no cost control until pricing is configured', () => {
  mount(job(), { configured: false })
  expect(screen.queryByRole('button', { name: 'Cost' })).toBeNull()
})
