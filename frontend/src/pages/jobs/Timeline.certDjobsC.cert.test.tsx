// cert-D jobsC: real Timeline page rendered against /timeline, /printers and /jobs/{id} bodies captured by
// tests/test_contracts/test_cert_D_jobsC_wire.py. Only the HTTP client boundary is replaced.
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Timeline from './Timeline'
import wire from '../../test-fixtures/certD-jobsC-wire.json'

const api = vi.hoisted(() => ({ timeline: vi.fn(), get: vi.fn() }))
vi.mock('../../api', () => ({
  timeline: { get: api.timeline }, printers: { list: vi.fn(async () => wire.printers) },
  jobActions: { move: vi.fn() }, jobs: { get: api.get }, printJobs: { get: vi.fn() },
}))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))

const jobsById: Record<number, any> = { 203: wire.job_203, 204: wire.job_204, 205: wire.job_205 }
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter><Timeline /></MemoryRouter></QueryClientProvider>)
}
beforeEach(() => {
  // Local midnight == UTC midnight so the page's startOfDay(now) matches the captured window start.
  process.env.TZ = 'UTC'
  vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-10-12T12:00:00Z'))
  api.timeline.mockResolvedValue(wire.timeline)
  api.get.mockImplementation(async (id: number) => jobsById[id])
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks() })

it('F04-4.8-01 one Gantt row per captured printer; each captured slot sits only on its printer row', async () => {
  const view = mount()
  await screen.findAllByText('CertC printing')
  const rows = Array.from(view.container.querySelectorAll('.timeline-grid')).map(g => (g as HTMLElement).dataset.printerId)
  expect(rows).toEqual(wire.printers.map((p: any) => String(p.id)))
  const p9 = view.container.querySelector('.timeline-grid[data-printer-id="9"]') as HTMLElement
  const p10 = view.container.querySelector('.timeline-grid[data-printer-id="10"]') as HTMLElement
  expect(p9).toHaveTextContent('CertC printing'); expect(p9).not.toHaveTextContent('CertC order scheduled')
  expect(p10).toHaveTextContent('CertC order scheduled'); expect(p10).not.toHaveTextContent('CertC printing')
  expect(api.timeline.mock.calls[0][1]).toBe(7)
})

it('F04-4.8-02 horizontal axis: captured 02:00-05:00 and 06:00-09:00 intervals map to left/width in 15-minute slots, and zoom rescales', async () => {
  const view = mount()
  await screen.findAllByText('CertC printing')
  const block = (printer: number, name: string) => within(view.container.querySelector(`.timeline-grid[data-printer-id="${printer}"]`) as HTMLElement).getByText(name).parentElement!.parentElement!
  // default slotWidth 20px, SLOT_MINUTES 15: 02:00 -> slot 8 -> 160px, 3h -> 12 slots -> 238px; 06:00 -> 480px
  expect(block(10, 'CertC order scheduled')).toHaveStyle({ left: '160px', width: '238px' })
  expect(block(9, 'CertC printing')).toHaveStyle({ left: '480px', width: '238px' })
  expect(screen.getAllByText('02:00').length).toBeGreaterThan(0)
  fireEvent.click(view.container.querySelector('.lucide-zoom-in')!.closest('button')!)
  expect(block(10, 'CertC order scheduled')).toHaveStyle({ left: '200px', width: '298px' })
})

// The "Job Details" drawer shows the slicer estimate alongside the job's actual start/end and actual duration.
it('F04-4.7-02 job detail drawer for a completed job shows its actual print duration', async () => {
  api.timeline.mockResolvedValue(wire.timeline_two_day)
  const view = mount()
  // Move the window back one day so the captured 10-11 window is the one rendered.
  await screen.findAllByText('CertC printing')
  fireEvent.click(within(view.container.querySelector('.timeline-grid[data-printer-id="9"]') as HTMLElement).getByText('CertC completed overdue'))
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(205))
  expect(await screen.findByText('Priority')).toBeInTheDocument()
  expect(screen.getByText(/2h 30m/)).toBeInTheDocument()
  expect(screen.getByText('Actual duration').closest('div')).toHaveTextContent('Actual duration2h 30m')
  expect(screen.getByText('Estimated').closest('div')).toHaveTextContent('Estimated2h') // wire duration_hours 2.0 kept
})
