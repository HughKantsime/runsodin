import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import StatusBadge, { printerBadgeStatus } from './StatusBadge'

afterEach(() => { cleanup(); document.documentElement.className = ''; delete document.documentElement.dataset.themeFamily })
it.each([
  ['RUNNING', 'Printing'], ['PRINTING', 'Printing'], ['PREPARING', 'Printing'], ['IDLE', 'Ready'], ['FINISH', 'Ready'],
  ['completed', 'Ready'], ['done', 'Ready'], ['approved', 'Ready'], ['online', 'Ready'], ['ready', 'Ready'],
  ['PAUSE', 'Needs you'], ['paused', 'Needs you'], ['failed', 'Needs you'], ['error', 'Needs you'], ['rejected', 'Needs you'],
  ['OFFLINE', 'Offline'], ['cancelled', 'Cancelled'], ['unknown', 'Unknown'],
  ['pending', 'Queued'], ['scheduled', 'Queued'], ['queued', 'Queued'],
  ['waiting_on_you', 'Waiting on you'], ['awaiting_approval', 'Waiting on you'], ['awaiting_clearance', 'Waiting on you'],
  ['needs-you', 'Needs you'], ['', 'Unknown'], ['new firmware state', 'new firmware state'],
])('Foundry always renders words for %s', (status, label) => {
  document.documentElement.dataset.themeFamily = 'foundry'
  render(<StatusBadge status={status} />)
  expect(screen.getByText(label)).toBeVisible()
  expect(screen.getByText(label)).toHaveClass('foundry-status')
})
it('retains the existing generic Classic badge classes and wording', () => {
  const { container } = render(<StatusBadge status="running" />)
  expect(screen.getByText('running')).toHaveClass('capitalize', 'font-medium')
  expect(container.firstChild).toHaveClass('text-[var(--status-printing)]')
  expect(container.querySelector('.foundry-status')).toBeNull()
})
it.each(['light', 'high-contrast'])('keeps Classic markup verbatim in the %s fallback', mode => {
  document.documentElement.dataset.themeFamily = 'foundry'; document.documentElement.classList.add(mode)
  const classic = <div className="legacy"><span>Online</span></div>
  const { container } = render(<StatusBadge status="idle" classic={classic} />)
  expect(container.innerHTML).toBe('<div class="legacy"><span>Online</span></div>')
})
it('prioritizes offline and errors over stale ready/printing telemetry', () => {
  expect(printerBadgeStatus({ gcode_state: 'RUNNING' }, false)).toBe('offline')
  expect(printerBadgeStatus({ gcode_state: 'IDLE', hms_errors: ['fault'] }, true)).toBe('error')
  expect(printerBadgeStatus({}, true)).toBe('unknown')
})

it.each(['offline', 'unknown', 'cancelled', 'new firmware state'])('%s uses a neutral Foundry badge, without asking for intervention', status => {
  document.documentElement.dataset.themeFamily = 'foundry'
  const { container } = render(<StatusBadge status={status} />)
  expect(container.firstChild).toHaveAttribute('data-status', 'neutral')
  expect(screen.queryByText(/Needs you/)).not.toBeInTheDocument()
})
it.each(['error', 'paused', 'attention'])('%s requires a person in Foundry', status => {
  document.documentElement.dataset.themeFamily = 'foundry'
  render(<StatusBadge status={status} />)
  expect(screen.getByText('Needs you')).toHaveAttribute('data-status', 'needsYou')
})
