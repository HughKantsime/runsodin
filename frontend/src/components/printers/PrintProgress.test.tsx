import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
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
