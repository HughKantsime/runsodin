import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import ChargebackReport from './ChargebackReport'
import fixture from '../../test-fixtures/chargeback-wire-owned.json'
const api = vi.hoisted(() => ({ report: vi.fn() }))
vi.mock('../../api', () => ({ chargebacks: { report: api.report } }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

test('actual owned HTTP fields display the job count without inventing grams or currency', async () => {
  api.report.mockResolvedValue(fixture.rows)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><ChargebackReport /></QueryClientProvider>)
  const row = (await screen.findByText('student')).closest('tr')!
  expect(within(row).getByText('5')).toBeInTheDocument()
  expect(within(row).getByText('2.5')).toBeInTheDocument()
  expect(within(row).getAllByText('Unknown')).toHaveLength(2)
  expect(within(row).queryByText('$7.25')).not.toBeInTheDocument()
})


test('query failure is visible and retry loads the real owned response', async () => {
  api.report.mockRejectedValueOnce(new Error('Synthetic unavailable')).mockResolvedValueOnce(fixture.rows)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><ChargebackReport /></QueryClientProvider>)
  expect(await screen.findByRole('alert')).toHaveTextContent('Unable to load chargeback data.')
  expect(screen.queryByText('No chargeback data for this period.')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  const row = (await screen.findByText('student')).closest('tr')!
  expect(within(row).getByText('5')).toBeInTheDocument()
  expect(api.report).toHaveBeenCalledTimes(2)
})

test('explicit currency and valid zeros remain distinct from unknown metrics', async () => {
  api.report.mockResolvedValue([{ ...fixture.rows[0], job_count:0, total_hours:0, total_grams:0, total_cost:0, currency:'JPY' }])
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><ChargebackReport /></QueryClientProvider>)
  const row = (await screen.findByText('student')).closest('tr')!
  expect(within(row).getByText('0')).toBeInTheDocument()
  expect(within(row).getByText('0.0')).toBeInTheDocument()
  expect(within(row).getByText('0g')).toBeInTheDocument()
  expect(within(row).getByText('JPY 0')).toBeInTheDocument()
})
