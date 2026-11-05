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
it('preserves zero and carries rounded remaining minutes', () => {
 render(<PrinterCard printer={printer} activeJob={{ job_name: 'Synthetic active', progress_percent: 0, remaining_minutes: 59.6 }} {...props} />)
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
