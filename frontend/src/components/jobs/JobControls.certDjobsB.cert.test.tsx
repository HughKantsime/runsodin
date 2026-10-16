// cert-D jobsB: job queue controls rendered from the producer wire captured by
// tests/test_contracts/test_cert_D_jobsB_api.py. Claims F04-4.2-02 (target types),
// F04-4.2-08 (quick-schedule presets), F04-4.2-09 (due dates and priority badges).
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import JobRow from './JobRow'
import { CreateJobModal } from './JobModals'
import { priorityOptions } from './jobUtils'
import Jobs from '../../pages/jobs/Jobs'
import wire from '../../test-fixtures/certD-jobsB-wire.json'

const api = vi.hoisted(() => ({ schedule: vi.fn(), list: vi.fn() }))
vi.mock('../../api', () => ({
  jobs: { list: api.list, reorder: vi.fn(), create: vi.fn(), update: vi.fn(), start: vi.fn(), complete: vi.fn(), cancel: vi.fn(), delete: vi.fn(), repeat: vi.fn(), dispatch: vi.fn() },
  models: { list: vi.fn().mockResolvedValue([]) }, modelRevisions: { list: vi.fn().mockResolvedValue([]) },
  printers: { list: vi.fn().mockResolvedValue([]) },
  getApprovalSetting: vi.fn().mockResolvedValue({ require_job_approval: false }),
  presets: { list: vi.fn(async () => wire.presets_list), create: vi.fn(), schedule: api.schedule, delete: vi.fn() },
  scheduler: { run: vi.fn() }, bulkOps: { jobs: vi.fn() },
  approveJob: vi.fn(), rejectJob: vi.fn(), resubmitJob: vi.fn(), updateJobFailure: vi.fn(), timeline: { get: vi.fn() }, jobActions: { move: vi.fn() }, printJobs: { get: vi.fn() },
}))
vi.mock('../../permissions', () => ({ canDo: () => true, getCurrentUser: () => ({ id: 3, role: 'operator' }) }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))

const rows = wire.jobs_list_priority_due.map((job, index) => ({ ...job, id: 500 + index }))
beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-10-14T12:00:00Z')); api.list.mockResolvedValue(rows); api.schedule.mockResolvedValue({ id: 900, item_name: 'Synthetic bracket', status: 'pending' }) })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks() })
function row(job: Record<string, unknown>) {
  return render(<table><tbody><JobRow job={job} onAction={vi.fn()} isSelected={false} onToggleSelect={vi.fn()} /></tbody></table>)
}

describe('F04-4.2-09 due dates and priority badges from producer rows', () => {
  it('renders P-rank badges in red / amber / neutral tiers and due-date badges', () => {
    const [urgent, normal, low] = rows
    let view = row(urgent)
    expect(within(view.container).getByText('P1')).toHaveClass('bg-red-900/50', 'text-red-400')
    expect(within(view.container).getByText('1d overdue')).toHaveClass('text-red-400')
    view.unmount(); view = row(normal)
    expect(within(view.container).getByText('P3')).toHaveClass('bg-amber-900/50', 'text-amber-400')
    expect(view.container.querySelector('svg.lucide-calendar')).toBeNull()
    view.unmount(); view = row(low)
    expect(within(view.container).getByText('P4')).toHaveClass('text-[var(--brand-text-secondary)]')
    expect(within(view.container).getByText('Due in 16d')).toBeInTheDocument()
  })

  it('CLAIM MISMATCH: offered levels are five numeric ranks, not low/normal/high/urgent', () => {
    expect(priorityOptions.map(o => o.label)).toEqual(['1 - Highest', '2 - High', '3 - Normal', '4 - Low', '5 - Lowest'])
    const modal = render(<QueryClientProvider client={new QueryClient()}><CreateJobModal isOpen onClose={vi.fn()} onSubmit={vi.fn()} printersData={[]} modelsData={[]} /></QueryClientProvider>)
    const select = screen.getAllByRole('combobox').find(e => Array.from(e.querySelectorAll('option')).some(o => o.textContent === '3 - Normal'))!
    expect(Array.from(select.querySelectorAll('option')).map(o => o.textContent)).not.toContain('Urgent')
    modal.unmount()
    for (const priority of [1, 2, 5]) {
      const view = row({ ...rows[1], priority })
      expect(within(view.container).getByText(`P${priority}`)).toBeInTheDocument()
      view.unmount()
    }
  })
})

describe('F04-4.2-02 target type selector', () => {
  const printers = [
    { id: 9, name: 'P1S-09', machine_type: 'P1S', api_type: 'bambu' },
    { id: 11, name: 'X1C-11', machine_type: 'X1C', api_type: 'bambu' },
    { id: 12, name: 'Voron-12', machine_type: 'Voron', api_type: 'moonraker' },
  ]
  function create() {
    const submit = vi.fn()
    const client = new QueryClient()
    render(<QueryClientProvider client={client}><CreateJobModal isOpen onClose={vi.fn()} onSubmit={submit} printersData={printers} modelsData={[]} /></QueryClientProvider>)
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'Synthetic targeted' } })
    return submit
  }
  it.each([
    ['Any Model Type', 'Select machine type...', 'X1C', ['P1S', 'X1C', 'Voron']],
    ['Any Protocol', 'Select protocol...', 'moonraker', ['bambu', 'moonraker']],
  ])('%s submits target_type and filter chosen from fleet values', (button, placeholder, value, offered) => {
    const submit = create()
    fireEvent.click(screen.getByRole('button', { name: button }))
    const select = screen.getAllByRole('combobox').find(e => e.querySelector('option')?.textContent === placeholder)!
    expect(Array.from(select.querySelectorAll('option')).slice(1).map(o => o.textContent)).toEqual(offered)
    fireEvent.change(select, { target: { value } })
    fireEvent.click(screen.getByRole('button', { name: /Create Job/ }))
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      target_type: button === 'Any Model Type' ? 'model' : 'protocol', target_filter: value, printer_id: null }))
  })
  it('Specific Printer submits the chosen printer id', () => {
    const submit = create()
    const select = screen.getAllByRole('combobox').find(e => e.querySelector('option')?.textContent === 'Auto-assign')!
    fireEvent.change(select, { target: { value: '12' } })
    fireEvent.click(screen.getByRole('button', { name: /Create Job/ }))
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({ target_type: 'specific', target_filter: null, printer_id: 12 }))
  })
})

describe('F04-4.2-08 quick-schedule presets from producer list', () => {
  it('Quick Schedule strip lists the persisted preset and schedules it by id', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    client.setQueryData(['auth', 'me'], { id: 3, role: 'operator', group_id: 1 })
    render(<QueryClientProvider client={client}><MemoryRouter><Jobs /></MemoryRouter></QueryClientProvider>)
    const preset = wire.presets_list[0]
    const button = await screen.findByRole('button', { name: preset.name })
    expect(button).toHaveAttribute('title', `Schedule: ${preset.item_name}`)
    expect(screen.getByText('Quick Schedule')).toBeInTheDocument()
    fireEvent.click(button)
    await waitFor(() => expect(api.schedule).toHaveBeenCalledTimes(1))
    expect(api.schedule.mock.calls[0][0]).toBe(preset.id)
    expect(wire.preset_schedule_response_keys).toEqual(['id', 'item_name', 'status'])
  })
})
