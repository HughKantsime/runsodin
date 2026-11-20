import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import LivePrintValues from './LivePrintValues'
import PrintProgress from './PrintProgress'
afterEach(cleanup)
it.each([0, 37, 100])('retains measured progress %s including zero', percent => {
 render(<PrintProgress percent={percent} remainingMinutes={0} />)
 expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', String(percent))
 expect(screen.getByText('0m left')).toBeInTheDocument()
})
it.each([null, undefined, -1, 101, NaN, Infinity, '37'])('shows unknown for invalid progress %s', percent => {
 render(<PrintProgress percent={percent} remainingMinutes={null} />)
 expect(screen.getByText('Progress unknown')).toBeInTheDocument()
 expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
 expect(screen.getByText('Time remaining unknown')).toBeInTheDocument()
})
it.each([[59.6,'1h 0m left'], [125,'2h 5m left'], [0.4,'0m left']])('formats remaining minutes without 60-minute remainder', (minutes, label) => {
 render(<PrintProgress percent={null} remainingMinutes={minutes} />)
 expect(screen.getByText(label)).toBeInTheDocument()
})
it.each([-1, NaN, Infinity, '12', undefined])('does not manufacture remaining time from %s', minutes => {
 render(<PrintProgress percent={0} remainingMinutes={minutes} />)
 expect(screen.getByText('Time remaining unknown')).toBeInTheDocument()
})

it('ages known values after 60 seconds while the view stays open and preserves unknowns', () => {
 vi.useFakeTimers()
 try {
   const printer = { api_type: 'bambu', mc_percent: 42, mc_remaining_time: 0, speed_level: null,
     speed_percent: 100, telemetry_at: new Date().toISOString() }
   render(<LivePrintValues printer={printer} />)
   act(() => vi.advanceTimersByTime(60000))
   expect(screen.getByText('42%')).toBeVisible()
   act(() => vi.advanceTimersByTime(1))
   expect(screen.getByText('42% as of 1 min ago')).toBeVisible()
   act(() => vi.advanceTimersByTime(120000))
   expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '42% as of 3 min ago')
   expect(screen.getByText('0m left as of 3 min ago')).toHaveClass('text-[var(--brand-text-muted)]')
   expect(screen.getByText('Last reported speed: 100% as of 3 min ago')).toBeVisible()
   expect(screen.getByText('Printer speed mode unknown')).toBeVisible()
 } finally { vi.useRealTimers() }
})
