// cert-D jobsC F04-4.5-02: real Analytics page rendered against /analytics, /analytics/failures and
// /analytics/time-accuracy bodies captured by tests/test_contracts/test_cert_D_jobsC_wire.py.
import React from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Analytics from './Analytics'
import wire from '../../test-fixtures/certD-jobsC-wire.json'

vi.mock('../../api', () => ({
  analytics: { get: vi.fn(async () => wire.analytics), timeAccuracy: vi.fn(async () => wire.time_accuracy), failures: vi.fn(async () => wire.failure_analytics) },
  printJobs: { list: vi.fn().mockResolvedValue([]) }, getEnergyRate: vi.fn().mockResolvedValue({ energy_cost_per_kwh: 0.1 }),
}))
// Only the browser layout boundary is replaced; charts, axes and bars are real recharts.
vi.mock('recharts', async importOriginal => {
  const actual = await importOriginal<typeof import('recharts')>()
  const R = await import('react')
  return { ...actual, ResponsiveContainer: ({ children, height }: any) => R.cloneElement(children, { width: 800, height: typeof height === 'number' ? height : 300 }) }
})
afterEach(() => { cleanup(); vi.clearAllMocks() })
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter><Analytics /></MemoryRouter></QueryClientProvider>)
}
const card = (heading: string) => screen.getByText(heading).closest('div.rounded-md') as HTMLElement

it('F04-4.5-02 failure analytics shows per-printer success/failure bars, fleet success rate and ranked reasons from the wire', async () => {
  const view = mount()
  await screen.findByText('Success / Failure by Printer')
  const printers = card('Success / Failure by Printer')
  expect(printers).toHaveTextContent(`${wire.failure_analytics.overall_success_rate}% fleet success`)
  for (const p of wire.failure_analytics.by_printer) expect(within(printers).getAllByText(p.name).length).toBeGreaterThan(0)
  // stacked completed + failed bars, one pair per printer
  expect(printers.querySelectorAll('.recharts-bar-rectangle').length).toBeGreaterThanOrEqual(wire.failure_analytics.by_printer.length)
  const reasons = card('Top Failure Reasons')
  const rows = Array.from(reasons.querySelectorAll('.space-y-2 > div')).map(r => r.textContent)
  expect(rows).toEqual(wire.failure_analytics.top_failure_reasons.map((r: any) => `${r.reason.replace(/_/g, ' ')}${r.count}`))
  expect(view.container).toBeTruthy()
})

// DEFECT: frontend/src/pages/analytics/Analytics.tsx:496-500 renders only by_printer and top_failure_reasons; the
// producer's by_model (which model fails), by_filament and mtbf_hours are never displayed.
it.fails('F04-4.5-02 DEFECT failure analytics shows which model fails most', async () => {
  mount()
  await screen.findByText('Success / Failure by Printer')
  const model = wire.failure_analytics.by_model[0]
  // A failure-by-model card naming the model with its failed count must exist.
  const heading = screen.getByText(/failure.*model|model.*failure/i)
  const section = heading.closest('div.rounded-md') as HTMLElement
  expect(section).toHaveTextContent(model.name)
  expect(section).toHaveTextContent(String(model.failed))
})
