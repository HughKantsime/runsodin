import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import JobRow, { JobCardList } from './JobRow'
import BlockDetail from './TimelineJobDetail'
import { jobs } from '../../api'

vi.mock('../../api', () => ({ jobs: { get: vi.fn() }, printJobs: { get: vi.fn() } }))
vi.mock('./OutcomeCorrection', () => ({ default: () => null }))
vi.mock('./JobTimeline', () => ({ default: () => null }))
vi.mock('./JobAmsMapping', () => ({ default: () => null }))
vi.mock('../shared/ControlledPart', () => ({ default: () => null }))

// The schedule dialog stores "due Nov 20" as midnight UTC; the API returns it without a zone.
const DUE = '2026-11-20T00:00:00'
const ZONES = ['America/Los_Angeles', 'UTC', 'Pacific/Auckland']
const job = (over = {}) => ({ id: 4, item_name: 'Bracket', status: 'pending', priority: 3, quantity: 1, hold: false, is_locked: false, colors_list: [], due_date: DUE, ...over })
const originalZone = process.env.TZ

function wrap(children: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>)
}
const row = (j: any) => wrap(<table><tbody><JobRow job={j} onAction={vi.fn()} isSelected={false} onToggleSelect={vi.fn()} /></tbody></table>)
/** Pretend the viewer's own clock reads this local date and time, in the zone under test. */
function viewerClock(zone: string, ...local: [number, number, number, number, number]) {
  process.env.TZ = zone
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(...local))
}

afterEach(() => {
  cleanup(); vi.useRealTimers(); vi.clearAllMocks()
  if (originalZone === undefined) delete process.env.TZ; else process.env.TZ = originalZone
})

describe.each(ZONES)('a job due Nov 20, seen from %s', zone => {
  it('is "Due tomorrow" all through Nov 19 and names Nov 20', () => {
    viewerClock(zone, 2026, 10, 19, 21, 0)
    row(job())
    const badge = screen.getByText('Due tomorrow')
    expect(badge).toHaveClass('text-orange-400')
    expect(badge).toHaveAttribute('title', `Due ${new Date(2026, 10, 20).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })}`)
    expect(screen.getByRole('row')).not.toHaveClass('bg-red-950/30')
  })

  it.each([[0, 5], [12, 0], [23, 55]])('is "Due today" and not overdue at %i:%i on Nov 20', (hour, minute) => {
    viewerClock(zone, 2026, 10, 20, hour, minute)
    row(job())
    expect(screen.getByText('Due today')).toBeInTheDocument()
    expect(screen.getByRole('row')).not.toHaveClass('bg-red-950/30')
  })

  it('is overdue from the first minute of Nov 21, on the row and on the phone card', () => {
    viewerClock(zone, 2026, 10, 21, 0, 5)
    const view = row(job())
    expect(screen.getByText('1d overdue')).toHaveClass('text-red-400')
    expect(screen.getByRole('row')).toHaveClass('bg-red-950/30', 'border-l-red-500')
    view.unmount()
    wrap(<JobCardList jobs={[job()]} isLoading={false} emptyText="" selectedIds={new Set()} onToggleSelect={vi.fn()} onSelectAll={vi.fn()} allSelected={false} onAction={vi.fn()} />)
    expect(screen.getByTestId('job-card')).toHaveClass('bg-red-950/30')
  })

  it('does not tint a finished job that is past its day', () => {
    viewerClock(zone, 2026, 10, 25, 9, 0)
    row(job({ status: 'completed' }))
    expect(screen.getByRole('row')).not.toHaveClass('bg-red-950/30')
  })

  it('shows "Nov 20, 2026" in the Timeline job detail', async () => {
    process.env.TZ = zone
    vi.mocked(jobs.get).mockResolvedValue(job({ status: 'scheduled' }) as any)
    wrap(<BlockDetail block={{ job_id: 4, start: '2026-11-18T10:00:00', end: '2026-11-18T12:00:00', printer_name: 'P1S-A' }} />)
    expect(await screen.findByText('Nov 20, 2026')).toBeInTheDocument()
    expect(screen.queryByText('Nov 19, 2026')).toBeNull()
  })
})
