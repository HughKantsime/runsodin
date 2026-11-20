import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { PrinterCard } from './Dashboard'
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-11T12:00:00Z')) })
afterEach(() => { delete document.documentElement.dataset.themeFamily; cleanup(); vi.useRealTimers() })
const printer = { id: 199, name: 'Synthetic dashboard printer', last_seen: '2026-10-11T11:59:59Z', gcode_state: 'RUNNING', filament_slots: [] }
const props = { hasCamera: false, onCameraClick: vi.fn(), onClick: vi.fn() }
it('labels unknown active-job progress instead of inventing measured zero', () => {
 render(<PrinterCard printer={printer} activeJob={{ job_name: 'Synthetic active', progress_percent: null, remaining_minutes: null }} {...props} />)
 expect(screen.getByText('Progress unknown')).toBeInTheDocument()
 expect(screen.getByText('Time remaining unknown')).toBeInTheDocument()
 expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})
it.each([{}, { telemetry_at: null }])('preserves zero and carries rounded remaining minutes without a snapshot (%j)', snapshot => {
 render(<PrinterCard printer={{ ...printer, ...snapshot }} activeJob={{ job_name: 'Synthetic active', progress_percent: 0, remaining_minutes: 59.6 }} {...props} />)
 expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '0')
 expect(screen.getByText('1h 0m left')).toBeInTheDocument()
})
it('does not show stale active progress while printer is offline', () => {
 render(<PrinterCard printer={{ ...printer, gcode_state: 'OFFLINE' }} activeJob={{ job_name: 'Synthetic active', progress_percent: 37, remaining_minutes: 125 }} {...props} />)
 expect(screen.queryByText('37%')).not.toBeInTheDocument()
 expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})
it('shows the human print name instead of the stored transport filename', () => {
 render(<PrinterCard printer={printer} activeJob={{ display_name: 'Squirtle', job_name: 'odin-dispatch-deadbeef', progress_percent: 10, remaining_minutes: 5 }} {...props} />)
 expect(screen.getByText('Squirtle')).toBeInTheDocument()
 expect(screen.queryByText(/odin-dispatch/)).not.toBeInTheDocument()
})

it.each([['RUNNING', 'Printing'], ['PAUSE', 'Needs you'], ['IDLE', 'Ready'], ['OFFLINE', 'Offline']])('uses the shared Foundry badge for %s once on the dashboard card', (state, label) => {
 document.documentElement.dataset.themeFamily = 'foundry'
 render(<PrinterCard printer={{ ...printer, gcode_state: state }} {...props} />)
 expect(screen.getAllByText(label)).toHaveLength(1)
 for (const badge of screen.getAllByText(label)) expect(badge).toHaveClass('foundry-status')
})

it.each([['RUNNING', 'Printing'], ['IDLE', 'Idle'], ['OFFLINE', 'Offline']])('restores Classic status %s in both header and footer with original styling', (state, label) => {
 render(<PrinterCard printer={{ ...printer, gcode_state: state }} {...props} />)
 const labels = screen.getAllByText(label)
 expect(labels).toHaveLength(2)
 expect(labels[0].parentElement).toHaveClass('font-medium')
 expect(labels[1].parentElement).toHaveClass('flex', 'items-center', 'gap-1.5')
 expect(labels[1].previousElementSibling).toHaveClass('w-1.5', 'h-1.5', 'rounded-full')
 expect(document.querySelector('.foundry-status')).toBeNull()
})

it('shows confirmation instead of frozen values across telemetry and refetch', () => {
 const job = { job_name: 'Bracket', awaiting_confirmation: true, progress_percent: 13, remaining_minutes: 615, current_layer: 6, total_layers: 93 }
 const view = render(<PrinterCard printer={printer} activeJob={job} {...props} />)
 for (const progress_percent of [80, 13]) {
  view.rerender(<PrinterCard printer={printer} activeJob={{ ...job, progress_percent }} {...props} />)
  expect(screen.getByText(/Waiting for print confirmation/)).toBeVisible()
  expect(screen.queryByRole('progressbar')).toBeNull()
  expect(screen.queryByText(/Layer 6\/93/)).toBeNull()
 }
})

it('Classic floor shows the catalogue name with two-line wrapping', () => {
 const name = 'ASA-CF Invented long colour name'
 render(<PrinterCard printer={{ ...printer, filament_slots: [{ slot_number: 1, filament_type: 'ASA-CF', catalogue_name: name, color_hex: '123456' }] }} {...props} />)
 expect(screen.getByText(name)).toHaveClass('line-clamp-2')
 expect(screen.getByText(name)).not.toHaveClass('truncate')
})

it('shows the last printer reading and its age even when the jobs list is behind', () => {
 render(<PrinterCard printer={{ ...printer, telemetry_at: '2026-10-11T11:57:00Z', mc_percent: 42,
   mc_remaining_time: 12, speed_percent: 80, layer_num: 18, total_layer_num: 60 }}
   activeJob={{ job_name: 'Bracket', progress_percent: 10 }} {...props} />)
 expect(screen.getByRole('progressbar', { name: 'Print progress, 42% as of 3 min ago' })).toBeVisible()
 expect(screen.getByText('Layer 18/60 as of 3 min ago')).toBeVisible()
 expect(screen.getByText('12m left as of 3 min ago')).toBeVisible()
 expect(screen.getByText('Last reported speed: 80% as of 3 min ago')).toBeVisible()
})
