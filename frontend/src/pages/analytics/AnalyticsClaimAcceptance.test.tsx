import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Calculator from '../orders/Calculator'
import Analytics from './Analytics'
import Utilization from './Utilization'

const api = vi.hoisted(() => ({ config: vi.fn(), save: vi.fn(), models: vi.fn(), analytics: vi.fn(), stats: vi.fn(), jobs: vi.fn() }))
vi.mock('../../api', () => ({
  pricingConfig: { get: api.config, update: api.save }, models: { list: api.models },
  analytics: { get: api.analytics, timeAccuracy: vi.fn().mockResolvedValue(null), failures: vi.fn().mockResolvedValue(null) },
  stats: { get: api.stats }, printJobs: { list: api.jobs },
  getEnergyRate: vi.fn().mockResolvedValue({ energy_cost_per_kwh: 0.1 }),
}))
// Only the browser layout boundary is replaced; charts, axes, bars, and tables are real.
vi.mock('recharts', async importOriginal => {
  const actual = await importOriginal<typeof import('recharts')>()
  const React = await import('react')
  return { ...actual, ResponsiveContainer: ({ children, height }: any) => React.cloneElement(children, { width: 800, height: typeof height === 'number' ? height : 300 }) }
})
const config = { spool_cost: 200, spool_weight: 1000, hourly_rate: 12, post_processing_min: 10, packing_min: 10, support_min: 10, electricity_rate: 1, printer_wattage: 500, printer_cost: 1000, printer_lifespan: 1000, packaging_cost: 3, failure_rate: 10, monthly_rent: 500, parts_per_month: 100, other_costs: 4, default_margin: 50 }
const model = { id: 9, name: 'Acceptance model', total_filament_grams: 100, build_time_hours: 2, units_per_bed: 2 }
const printer = { id: 1, name: 'Acceptance printer', completed_jobs: 8, failed_jobs: 2, total_hours: 20, utilization_pct: 40, success_rate: 80, avg_job_hours: 2.5 }
const analytics = {
  summary: { total_jobs: 10, completed_jobs: 8, pending_jobs: 2, total_models: 2, total_revenue: 60, projected_revenue: 15, avg_value_per_hour: 10, total_print_hours: 20, total_cost: 25, total_margin: 35 },
  jobs_by_date: { '2026-10-02': { created: 1, completed: 1, revenue: 40, cost: 15 }, '2026-10-01': { created: 2, completed: 1, revenue: 20, cost: 10 } },
  top_by_hour: [{ id: 2, name: 'High hourly low count', value_per_hour: 50, value_per_bed: 100, build_time_hours: 2, job_count: 1 }, { id: 1, name: 'Low hourly high count', value_per_hour: 10, value_per_bed: 20, build_time_hours: 2, job_count: 10 }],
  top_by_jobs: [{id:1,name:'Low hourly high count',job_count:10},{id:2,name:'High hourly low count',job_count:1}],
  worst_performers: [], printer_stats: [printer],
}
const clients: QueryClient[] = []
function show(node: React.ReactNode) { const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c); return render(<QueryClientProvider client={c}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>) }
function input(label: string) { return screen.getByText(label, { selector: 'label', exact: true }).parentElement!.querySelector('input')! }
function change(label: string, value: number) { fireEvent.change(input(label), { target: { value: String(value) } }) }
function moneyRow(label: string) { return screen.getByText(label, { exact: true }).parentElement! }
async function loadCalculator() { show(<Calculator />); await screen.findByRole('heading', { name: 'Pricing Calculator' }); await screen.findByRole('option', { name: model.name }); fireEvent.change(screen.getByRole('combobox'), { target: { value: '9' } }); await waitFor(() => expect(input('Filament Used')).toHaveValue(100)) }
function dailyChart() { return within(screen.getByRole('heading', { name:'Completed and Failed Prints per Day' }).parentElement!) }
function panel(title: string) { return screen.getByRole('heading', { name: title }).parentElement!.parentElement! }
beforeEach(() => {
  vi.useFakeTimers({ toFake:['Date'] }); vi.setSystemTime(new Date('2026-10-13T12:00:00Z'))
  vi.clearAllMocks(); api.config.mockResolvedValue(config); api.save.mockImplementation(async value => value)
  api.models.mockResolvedValue([model]); api.analytics.mockResolvedValue(analytics); api.stats.mockResolvedValue({ printer_stats: [printer] }); api.jobs.mockResolvedValue([])
})
afterEach(() => { vi.useRealTimers(); cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('F10-10.2-04 loads the dedicated Calculator page and model-derived inputs', async () => {
  await loadCalculator(); expect(input('Print Time')).toHaveValue(2); expect(input('Quantity')).toHaveValue(2)
  expect(screen.getByRole('heading', { name: 'Final Pricing' })).toBeInTheDocument()
})
it('F10-10.2-01 computes each displayed per-model cost component from saved inputs', async () => {
  await loadCalculator()
  for (const [label, value] of [['Material Cost:', '$20.00'], ['Labor Cost:', '$6.00'], ['Electricity:', '$1.00'], ['Printer Depreciation:', '$2.00'], ['Packaging:', '$3.00'], ['Failure Buffer (10%):', '$3.60'], ['Overhead:', '$5.00'], ['Other Costs:', '$4.00']]) expect(moneyRow(label)).toHaveTextContent(value)
})
it('F10-10.2-01 CLAIM GAP: subtotal includes monetary costs only, without adding the failure percentage as dollars', async () => {
  await loadCalculator(); expect(moneyRow('Subtotal:')).toHaveTextContent('$44.60')
})
it('F10-10.2-02 edits and saves manufacturing margin and recomputes price with zero failure rate', async () => {
  // The acceptance model is a 2-up bed: per-unit price is half the bed price ($51.25 / 2).
  api.config.mockResolvedValue({ ...config, failure_rate: 0 }); await loadCalculator(); change('Margin', 25)
  expect(moneyRow('Recommended Price (per unit)')).toHaveTextContent('$25.63')
  fireEvent.click(screen.getByRole('button', { name: 'Save Config' }))
  await waitFor(() => expect(api.save).toHaveBeenCalledWith(expect.objectContaining({ default_margin: 25, failure_rate: 0 }), expect.anything()))
})
it('F10-10.2-03 computes per-unit and bulk prices with zero failure rate', async () => {
  api.config.mockResolvedValue({ ...config, failure_rate: 0 }); await loadCalculator(); change('Quantity', 3)
  expect(moneyRow('Recommended Price (per unit)')).toHaveTextContent('$30.75')
  expect(moneyRow('Total for 3 units')).toHaveTextContent('$92.25')
})
it('F10-10.2-03 CLAIM GAP: nonzero failure buffer produces correct per-unit and bulk prices', async () => {
  await loadCalculator(); change('Quantity', 3)
  expect([moneyRow('Recommended Price (per unit)').textContent, moneyRow('Total for 3 units').textContent]).toEqual(['Recommended Price (per unit)$33.45', 'Total for 3 units$100.35'])
})
it('F10-10.1-02 renders actual cost/revenue bars and chronological date ticks', async () => {
  show(<Analytics />); await screen.findByRole('heading', { name: 'Revenue vs Cost' })
  const chart = panel('Revenue vs Cost')
  await waitFor(() => expect(chart.querySelectorAll('.recharts-bar-rectangle').length).toBe(4))
  expect(within(chart as HTMLElement).getByText('Revenue')).toBeInTheDocument(); expect(within(chart as HTMLElement).getByText('Cost')).toBeInTheDocument()
  const ticks = [...chart.querySelectorAll('.recharts-cartesian-axis-tick-value')].map(el => el.textContent).filter(text => text?.startsWith('Oct '))
  expect(ticks).toEqual(['Oct 1','Oct 2'])
})
it('F10-10.1-03 displays model rankings by dollars per hour, preserving service order', async () => {
  show(<Analytics />); await screen.findByRole('heading',{name:'Top Performers'})
  const ranking = panel('Top Performers'); expect(ranking).toHaveTextContent('$/hour')
  expect(ranking.textContent!.indexOf('High hourly low count')).toBeLessThan(ranking.textContent!.indexOf('Low hourly high count'))
})
it('F10-10.1-03 CLAIM GAP: model rankings are ordered and labeled by job count', async () => {
  show(<Analytics />); await screen.findByRole('heading',{name:'Top Printed Models'}); const ranking = panel('Top Printed Models')
  expect(ranking.textContent!.indexOf('Low hourly high count')).toBeLessThan(ranking.textContent!.indexOf('High hourly low count'))
  expect(ranking).toHaveTextContent(/10 completed jobs/)
})
it('F10-10.1-04 renders fleet totals and computed completion rate', async () => {
  show(<Analytics />); await screen.findByRole('heading', { name: 'Fleet Overview' }); const fleet = panel('Fleet Overview') as HTMLElement
  for (const [label, value] of [['Total Jobs','10'],['Completed','8'],['Pending','2'],['Models','2']]) expect(within(fleet).getByText(label).parentElement).toHaveTextContent(value)
  expect(fleet).toHaveTextContent('80% completion rate')
})
it('F10-10.3-03 renders utilization SVG bars and exact per-printer table values, and changes time range', async () => {
  show(<Utilization />); await screen.findByRole('heading', { name:'Completed and Failed Prints per Day' }); await screen.findByRole('cell', { name: printer.name })
  const row = screen.getByRole('cell', { name: printer.name }).closest('tr')!
  expect(within(row).getAllByRole('cell').map(c => c.textContent)).toEqual(['Acceptance printer','8','20h','40%','80%','2.5h'])
  await waitFor(() => expect(document.querySelectorAll('.recharts-bar-rectangle').length).toBe(1))
  fireEvent.change(screen.getByRole('combobox'), { target: { value: '7' } })
  await waitFor(() => expect(api.stats).toHaveBeenCalledWith(7)); expect(api.jobs).toHaveBeenCalledWith({ limit: 500, days: 7 })
})
async function exportedCsv(name = printer.name) {
  api.stats.mockResolvedValue({ printer_stats: [{ ...printer, name }] })
  const blobs: Blob[] = []
  vi.spyOn(URL, 'createObjectURL').mockImplementation(blob => { blobs.push(blob as Blob); return 'blob:acceptance' })
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  show(<Utilization />); await screen.findByRole('heading', { name:'Completed and Failed Prints per Day' }); await screen.findByRole('cell', { name }); fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
  expect(click.mock.instances[0]).toHaveProperty('download', expect.stringMatching(/^utilization-report-\d{4}-\d{2}-\d{2}\.csv$/))
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:acceptance'); expect(blobs[0].type).toBe('text/csv')
  return await new Promise<string>((resolve, reject) => { const r = new FileReader(); r.onload = () => resolve(String(r.result)); r.onerror = reject; r.readAsText(blobs[0]) })
}
it('F10-10.3-04 exports CSV headers, data, filename, and revokes the temporary URL', async () => {
  expect(await exportedCsv()).toBe('Printer,Jobs,Hours,Utilization %,Success Rate %,Failed,Avg Job (hrs)\nAcceptance printer,8,20,40,80,2,2.5')
})
it('F10-10.3-04 CLAIM GAP: quotes comma and quote characters in printer names for valid CSV columns', async () => {
  expect(await exportedCsv('Lab, "A"')).toContain('\n"Lab, ""A""",8,20,40,80,2,2.5')
})

it('Calculator model request does not pass React Query context as an organization ID', async () => {
  await loadCalculator()
  expect(api.models).toHaveBeenCalledExactlyOnceWith()
})

it('Analytics preserves a genuine zero printer success rate', async () => {
  api.analytics.mockResolvedValue({ ...analytics, printer_stats: [{ ...printer, completed_jobs:0, failed_jobs:10, success_rate:0 }] })
  show(<Analytics />)
  await screen.findByText('Acceptance printer')
  const section = screen.getByRole('heading', { name:'Printer Utilization' }).parentElement!
  expect(within(section).getByText('0%')).toBeInTheDocument()
  expect(within(section).queryByText('100%')).not.toBeInTheDocument()
})

it('Utilization charts completed telemetry on its actual ending date', async () => {
  api.jobs.mockResolvedValue([{ id:101, printer_id:1, status:'completed', started_at:'2026-10-11T10:00:00', ended_at:'2026-10-11T12:00:00', created_at:'2026-10-10T10:00:00' }])
  show(<Utilization />); await screen.findByRole('heading', { name:'Completed and Failed Prints per Day' })
  expect((await dailyChart().findAllByText('10-11')).length).toBeGreaterThan(0)
  expect(dailyChart().queryByText('10-10')).not.toBeInTheDocument()
})

it('Utilization groups offset completion timestamps by UTC day', async () => {
  api.jobs.mockResolvedValue([{ id:101, printer_id:1, status:'failed', ended_at:'2026-10-11T23:30:00-04:00' }])
  show(<Utilization />); await screen.findByRole('heading', { name:'Completed and Failed Prints per Day' })
  expect((await dailyChart().findAllByText('10-12')).length).toBeGreaterThan(0)
  expect(dailyChart().queryByText('10-11')).not.toBeInTheDocument()
})

it('Utilization does not invent a completion date from creation time', async () => {
  api.jobs.mockResolvedValue([{ id:101, printer_id:1, status:'failed', ended_at:null, created_at:'2026-10-10T10:00:00' }])
  show(<Utilization />); await screen.findByRole('heading', { name:'Completed and Failed Prints per Day' })
  await screen.findByText('1 finished print has no valid completion date.')
  expect(dailyChart().queryByText('10-10')).not.toBeInTheDocument()
  expect(screen.getByText('No dated print data available')).toBeInTheDocument()
})

it('Utilization daily chart excludes ending dates outside the selected window', async () => {
  api.jobs.mockResolvedValue([{ id:101, printer_id:1, status:'completed', ended_at:'2026-10-12T12:00:00Z' }, { id:102, printer_id:1, status:'completed', ended_at:'2026-10-02T12:00:00Z' }])
  show(<Utilization />); await screen.findByRole('heading', { name:'Completed and Failed Prints per Day' })
  await dailyChart().findAllByText('10-02')
  fireEvent.change(screen.getByRole('combobox'), { target:{ value:'7' } }); await screen.findByRole('heading', { name:'Completed and Failed Prints per Day' })
  await dailyChart().findAllByText('10-12')
  await waitFor(() => expect(dailyChart().queryByText('10-02')).not.toBeInTheDocument())
})

it('Analytics distinguishes absent daily monetary fields from zero revenue', async () => {
  api.analytics.mockResolvedValue({ ...analytics, jobs_by_date: { '2026-10-01':{created:2,completed:1} } })
  show(<Analytics />)
  expect(await screen.findByText('Daily revenue and cost are unavailable.')).toBeInTheDocument()
})

it('Analytics retains explicitly known zero daily money instead of claiming absent data', async () => {
 api.analytics.mockResolvedValue({ ...analytics, jobs_by_date:{'2026-10-01':{created:1,completed:1,revenue:0,cost:0}} })
 show(<Analytics />)
 await screen.findByRole('heading',{name:'Revenue vs Cost'})
 const chart=within(screen.getByRole('heading',{name:'Revenue vs Cost'}).parentElement!.parentElement!)
 expect((await chart.findAllByText('Oct 1')).length).toBeGreaterThan(0)
 expect(chart.queryByText('Daily revenue and cost are unavailable.')).not.toBeInTheDocument()
 expect(chart.queryByText('No cost/revenue data yet')).not.toBeInTheDocument()
})

it('Analytics marks partial daily monetary coverage explicitly', async () => {
 api.analytics.mockResolvedValue({ ...analytics, jobs_by_date:{'2026-10-01':{created:1,completed:1,revenue:10,cost:5},'2026-10-02':{created:1,completed:1}} })
 show(<Analytics />)
 expect(await screen.findByText('Some daily revenue and cost values are unavailable.')).toBeInTheDocument()
})

it('Analytics marks nonfinite subtraction as unavailable without losing valid dates', async () => {
 api.analytics.mockResolvedValue({ ...analytics, jobs_by_date:{'2026-10-01':{created:1,completed:1,revenue:10,cost:5},'2026-10-02':{created:1,completed:1,revenue:1e308,cost:-1e308}} })
 show(<Analytics />)
 expect(await screen.findByText('Some daily revenue and cost values are unavailable.')).toBeInTheDocument()
 const chart=within(screen.getByRole('heading',{name:'Revenue vs Cost'}).parentElement!.parentElement!)
 expect((await chart.findAllByText('Oct 1')).length).toBeGreaterThan(0)
 expect(chart.queryByText('Oct 2')).not.toBeInTheDocument()
})
