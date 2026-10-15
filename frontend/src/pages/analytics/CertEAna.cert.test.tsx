// Cert-E analytics UI (F10.1-F10.6). Every API response below is a producer wire fixture captured
// from the real backend by tests/test_contracts/test_cert_E_ana_backend.py (fixed clock 2026-10-13T12:00Z).
import React from 'react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Analytics from './Analytics'
import Utilization from './Utilization'
import Calculator from '../orders/Calculator'
import analyticsWire from '../../test-fixtures/cert-E-ana-analytics.json'
import failuresWire from '../../test-fixtures/cert-E-ana-failures.json'
import energyWire from '../../test-fixtures/cert-E-ana-print-jobs-energy.json'
import statsWire from '../../test-fixtures/cert-E-ana-stats-7.json'
import pricingWire from '../../test-fixtures/cert-E-ana-pricing-config.json'
import modelsWire from '../../test-fixtures/cert-E-ana-models.json'

const api = vi.hoisted(() => ({ analytics: vi.fn(), failures: vi.fn(), jobs: vi.fn(), stats: vi.fn(), rate: vi.fn(), config: vi.fn(), save: vi.fn(), models: vi.fn() }))
vi.mock('../../api', () => ({
  analytics: { get: api.analytics, timeAccuracy: vi.fn().mockResolvedValue(null), failures: api.failures },
  stats: { get: api.stats }, printJobs: { list: api.jobs },
  getEnergyRate: api.rate, setEnergyRate: vi.fn(),
  pricingConfig: { get: api.config, update: api.save }, models: { list: api.models },
}))
// Only the browser layout boundary (ResizeObserver sizing) is replaced; charts render real SVG.
vi.mock('recharts', async importOriginal => {
  const actual = await importOriginal<typeof import('recharts')>()
  const R = await import('react')
  return { ...actual, ResponsiveContainer: ({ children, height }: any) => R.cloneElement(children, { width: 800, height: typeof height === 'number' ? height : 300 }) }
})

const clients: QueryClient[] = []
function show(node: React.ReactNode) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c)
  return render(<QueryClientProvider client={c}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>)
}
const card = (label: string) => screen.getByText(label, { selector: 'span.uppercase' }).parentElement!.parentElement!
const panel = (title: string) => screen.getByRole('heading', { name: title }).parentElement!.parentElement! as HTMLElement
const ticks = (el: Element) => [...el.querySelectorAll('.recharts-cartesian-axis-tick-value')].map(t => t.textContent)

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-10-13T12:00:00Z'))
  api.analytics.mockResolvedValue(analyticsWire); api.failures.mockResolvedValue(failuresWire)
  api.jobs.mockResolvedValue(energyWire.rows); api.rate.mockResolvedValue(energyWire.rate)
  api.stats.mockResolvedValue(statsWire); api.config.mockResolvedValue(pricingWire)
  api.save.mockImplementation(async (v: unknown) => v); api.models.mockResolvedValue(modelsWire)
})
afterEach(() => { vi.useRealTimers(); cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.clearAllMocks(); vi.restoreAllMocks() })

// ---------- F10.1 Analytics dashboard ----------
test('F10-10.1-01 hero/margin stat cards show producer revenue, cost, margin and completed jobs', async () => {
  show(<Analytics />); await screen.findByRole('heading', { name: 'Fleet Overview' })
  expect(card('Revenue')).toHaveTextContent('$31.00'); expect(card('Revenue')).toHaveTextContent('3 completed jobs')
  expect(card('Total Cost')).toHaveTextContent('$11.00')
  expect(card('Net Margin')).toHaveTextContent('$20.00'); expect(card('Net Margin')).toHaveTextContent('64.5% margin')
})
test('F10-10.1-01 wording: the first (hero) row is Revenue/Pipeline/Avg $/Hour/Print Hours; cost and margin are a second row', async () => {
  show(<Analytics />); await screen.findByRole('heading', { name: 'Fleet Overview' })
  const rows = [card('Revenue').parentElement!, card('Total Cost').parentElement!]
  expect([...rows[0].querySelectorAll('span.uppercase')].map(s => s.textContent)).toEqual(['Revenue', 'Pipeline', 'Avg $/Hour', 'Print Hours'])
  expect([...rows[1].querySelectorAll('span.uppercase')].map(s => s.textContent)).toEqual(['Total Cost', 'Net Margin', 'Projected Cost', 'Cost Tracked'])
})
// DEFECT: frontend/src/api/reporting.ts:72 analytics.get() drops the window Analytics.tsx:357 passes, and
// backend/modules/reporting/routes/analytics.py:179 accepts none, so the page range selector never changes hero totals.
test.fails('DEFECT F10-10.1-01 the real analytics client forwards the selected range to the backend', async () => {
  const real = await vi.importActual<typeof import('../../api/reporting')>('../../api/reporting')
  const fetchSpy = vi.fn().mockResolvedValue(new Response(JSON.stringify(analyticsWire), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetchSpy)
  try {
    await (real.analytics.get as (days?: number) => Promise<unknown>)(7)
    expect(String(fetchSpy.mock.calls[0][0])).toContain('days=7')
  } finally { vi.unstubAllGlobals() }
})
// DEFECT: backend/modules/reporting/routes/analytics.py:340-349 jobs_by_date has no revenue/cost, so with real producer
// data the Cost/revenue bar chart never renders bars.
test.fails('DEFECT F10-10.1-02 cost/revenue time-series renders daily bars from the real producer response', async () => {
  show(<Analytics />); await screen.findByRole('heading', { name: 'Revenue vs Cost' })
  const chart = panel('Revenue vs Cost')
  expect(chart).not.toHaveTextContent('Daily revenue and cost are unavailable.')
  await waitFor(() => expect(chart.querySelectorAll('.recharts-bar-rectangle').length).toBe(4), { timeout: 500 })
})
test('F10-10.1-03 Top Printed Models ranks models by completed job count from the producer', async () => {
  show(<Analytics />); await screen.findByRole('heading', { name: 'Top Printed Models' })
  const ranking = panel('Top Printed Models')
  expect(ranking.textContent).toContain('1Own model2 completed jobs')
  expect(ranking.textContent).toContain('2Shared model1 completed job')
  expect(ranking.textContent!.indexOf('Own model')).toBeLessThan(ranking.textContent!.indexOf('Shared model'))
})
test('F10-10.1-04 Fleet Overview shows producer totals and computed completion rate', async () => {
  show(<Analytics />); await screen.findByRole('heading', { name: 'Fleet Overview' })
  const fleet = panel('Fleet Overview')
  for (const [label, value] of [['Total Jobs', '6'], ['Completed', '3'], ['Pending', '1'], ['Models', '3']]) expect(within(fleet).getByText(label).parentElement).toHaveTextContent(value)
  expect(fleet).toHaveTextContent('50% completion rate')
})

// ---------- F10.5 Failure analytics ----------
test('F10-10.5-01 failure analytics charts success/failure per printer and top failure reasons', async () => {
  show(<Analytics />); await screen.findByRole('heading', { name: 'Success / Failure by Printer' })
  const chart = panel('Success / Failure by Printer')
  expect(chart).toHaveTextContent('60% fleet success')
  expect(ticks(chart)).toEqual(expect.arrayContaining(['P1S-10', 'P1S-09']))
  const reasons = panel('Top Failure Reasons')
  expect(reasons).toHaveTextContent('spaghetti'); expect(reasons).toHaveTextContent('adhesion')
})
test('F10-10.5-01 wording: per-model failure rates are API-only (not rendered on the Analytics page)', async () => {
  show(<Analytics />); await screen.findByRole('heading', { name: 'Top Failure Reasons' })
  expect(document.body).not.toHaveTextContent('33.3%')
})

// ---------- F10.4 Energy ----------
test('F10-10.4-02 EnergyWidget in Analytics totals producer kWh and cost', async () => {
  show(<Analytics />); const widget = (await screen.findByRole('heading', { name: 'Energy Consumption' })).parentElement!.parentElement!.parentElement!
  await within(widget).findByRole('button', { name: /\$0\.2\/kWh/ })
  expect(within(widget).getByText('Total kWh').previousSibling).toHaveTextContent('0.8')
  expect(within(widget).getByText('Total Cost').previousSibling).toHaveTextContent('$0.15')
  expect(within(widget).getByText('Jobs Tracked').previousSibling).toHaveTextContent('2')
})
// DEFECT: frontend/src/components/reporting/EnergyWidget.tsx:62 groups by j.completed_at, a field /print-jobs never returns
// (print_jobs has ended_at), so every job lands in one 'Unknown' bucket and no consumption trend over days is shown.
test.fails('DEFECT F10-10.4-02 EnergyWidget shows a per-day consumption trend from producer rows', async () => {
  show(<Analytics />); const widget = (await screen.findByRole('heading', { name: 'Energy Consumption' })).parentElement!.parentElement!.parentElement!
  await waitFor(() => expect(ticks(widget).filter(t => /^(Oct|Unknown)/.test(t ?? ''))).toEqual(['Oct 11', 'Oct 12']), { timeout: 500 })
})

// ---------- F10.3 Utilization ----------
test('F10-10.3-01 fleet summary: average utilization, total print hours, busiest printer', async () => {
  show(<Utilization />); await screen.findByRole('cell', { name: 'P1S-09' })
  expect(screen.getByText('Avg Utilization').previousSibling).toHaveTextContent('0.6%')
  expect(screen.getByText('Total Print Hours').previousSibling).toHaveTextContent('9h')
  expect(screen.getByText('Jobs Completed').previousSibling).toHaveTextContent('3')
  const busiest = screen.getByText('Busiest Printer').parentElement!
  expect(busiest).toHaveTextContent('P1S-09'); expect(busiest).toHaveTextContent('0.8% utilization in the selected window')
})
test('F10-10.3-02/03 per-printer table and utilization chart from the producer response', async () => {
  show(<Utilization />); await screen.findByRole('cell', { name: 'P1S-09' })
  const cells = (name: string) => within(screen.getByRole('cell', { name }).closest('tr')!).getAllByRole('cell').map(c => c.textContent)
  expect(cells('P1S-09')).toEqual(['P1S-09', '2', '6h', '0.8%', '100%', '3h'])
  expect(cells('P1S-10')).toEqual(['P1S-10', '1', '3h', '0.4%', '50%', '3h'])
  const util = screen.getByRole('heading', { name: 'Utilization by Printer' }).parentElement!
  await waitFor(() => expect(util.querySelectorAll('.recharts-bar-rectangle').length).toBe(2))
  const daily = screen.getByRole('heading', { name: 'Completed and Failed Prints per Day' }).parentElement!
  await waitFor(() => expect(ticks(daily)).toEqual(expect.arrayContaining(['10-11', '10-12'])))
  fireEvent.change(screen.getByRole('combobox'), { target: { value: '7' } })
  await waitFor(() => expect(api.stats).toHaveBeenLastCalledWith(7)); expect(api.jobs).toHaveBeenLastCalledWith({ limit: 500, days: 7 })
})
test('F10-10.3-04 CSV export downloads the per-printer rows', async () => {
  const blobs: Blob[] = []
  vi.spyOn(URL, 'createObjectURL').mockImplementation(b => { blobs.push(b as Blob); return 'blob:cert' })
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  show(<Utilization />); await screen.findByRole('cell', { name: 'P1S-09' })
  fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
  expect(click.mock.instances[0]).toHaveProperty('download', 'utilization-report-2026-10-13.csv')
  expect(blobs[0].type).toBe('text/csv')
  const textOf = await new Promise<string>((ok, bad) => { const r = new FileReader(); r.onload = () => ok(String(r.result)); r.onerror = bad; r.readAsText(blobs[0]) })
  expect(textOf).toBe('Printer,Jobs,Hours,Utilization %,Success Rate %,Failed,Avg Job (hrs)\nP1S-09,2,6,0.8,100,0,3\nP1S-10,1,3,0.4,50,1,3')
})

// ---------- F10.2 Cost calculator ----------
const label = (l: string) => screen.getByText(l, { selector: 'label', exact: true }).parentElement!.querySelector('input')!
const money = (l: string) => screen.getByText(l, { exact: true }).parentElement!
async function calculator() {
  show(<Calculator />); await screen.findByRole('heading', { name: 'Pricing Calculator' })
  await screen.findByRole('option', { name: 'Own model' })
  fireEvent.change(screen.getByRole('combobox'), { target: { value: '101' } })
  await waitFor(() => expect(label('Filament Used')).toHaveValue(100))
}
test('F10-10.2-04 dedicated Calculator page loads saved config and model inputs', async () => {
  await calculator()
  expect(label('Print Time')).toHaveValue(2); expect(label('Quantity')).toHaveValue(2)
  expect(screen.getByRole('heading', { name: 'Final Pricing' })).toBeInTheDocument()
})
test('F10-10.2-01 per-model breakdown matches backend /models/{id}/cost components', async () => {
  await calculator()
  for (const [l, v] of [['Material Cost:', '$20.00'], ['Labor Cost:', '$6.00'], ['Electricity:', '$1.00'], ['Printer Depreciation:', '$2.00'], ['Packaging:', '$3.00'], ['Failure Buffer (10%):', '$3.60'], ['Overhead:', '$5.00'], ['Other Costs:', '$4.00'], ['Subtotal:', '$44.60']]) expect(money(l)).toHaveTextContent(v)
})
test('F10-10.2-02 margin from saved config prices the unit and an edited margin is saved', async () => {
  await calculator()
  expect(money('Recommended Price (per unit)')).toHaveTextContent('$55.75')
  fireEvent.change(label('Margin'), { target: { value: '30' } })
  expect(money('Recommended Price (per unit)')).toHaveTextContent('$57.98')
  fireEvent.click(screen.getByRole('button', { name: 'Save Config' }))
  await waitFor(() => expect(api.save).toHaveBeenCalledWith(expect.objectContaining({ default_margin: 30 }), expect.anything()))
})
test('F10-10.2-03 bulk quantity multiplies the per-unit price', async () => {
  await calculator(); fireEvent.change(label('Quantity'), { target: { value: '3' } })
  expect(money('Total for 3 units')).toHaveTextContent('$167.25')
})
// DEFECT: frontend/src/pages/orders/Calculator.tsx:172-176 loads the model's per-bed grams/hours (backend
// filament_per_item = total/units_per_bed) and sets Quantity=units_per_bed, while :73-75 prices the whole bed as ONE
// unit, so a 2-up bed is priced at the full bed cost per unit and 'Total for 2 units' double-counts (111.50 vs 55.75).
test.fails('DEFECT F10-10.2-03 per-unit price of a multi-unit bed divides the bed cost', async () => {
  await calculator()
  expect(money('Total for 2 units')).toHaveTextContent('$55.75')
})
