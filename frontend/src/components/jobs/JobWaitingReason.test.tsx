// A queued job says what it is waiting for (server-computed waiting_reason): one plain line on the
// table row and on the phone card, with a button only where an existing action fixes the wait.
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import JobRow, { JobCardList } from './JobRow'

const perms = vi.hoisted(() => ({ allowed: true }))
vi.mock('./OutcomeCorrection', () => ({ default: () => null }))
vi.mock('../../permissions', () => ({ canDo: () => perms.allowed, getCurrentUser: () => ({ id: 3, role: 'operator' }) }))

const job = (over: any = {}) => ({ id: 4, item_name: 'Bracket', status: 'scheduled', printer_id: 9, printer: { name: 'P1S-A' }, priority: 3, quantity: 1, hold: false, is_locked: false, ...over })
const wrap = (node: React.ReactNode) => render(<QueryClientProvider client={new QueryClient()}>{node}</QueryClientProvider>)
function row(j: any, onAction = vi.fn()) {
  wrap(<table><tbody><JobRow job={j} onAction={onAction} isSelected={false} onToggleSelect={vi.fn()} /></tbody></table>)
  return onAction
}
function card(j: any, onAction = vi.fn()) {
  wrap(<JobCardList jobs={[j]} isLoading={false} emptyText="" selectedIds={new Set()} onToggleSelect={vi.fn()} onSelectAll={vi.fn()} allSelected={false} onAction={onAction} printers={[]} />)
  return onAction
}
const line = () => screen.getByTestId('job-waiting-reason')
// A fixed local time, so the expected clock text does not depend on the machine's zone.
const at = (hours: number, minutes: number) => { const d = new Date(); d.setHours(hours, minutes, 0, 0); return d.toISOString() }

beforeEach(() => { perms.allowed = true })
afterEach(() => cleanup())

const CASES: [string, any, any, string][] = [
  ['awaiting_approval', { status: 'submitted' }, {}, 'Waiting for approval'],
  ['on_hold', { status: 'pending', hold: true }, {}, 'On hold until it is released'],
  ['needs_filament_mapping', {}, {}, 'Needs a filament slot chosen for each colour'],
  ['printer_offline', {}, { printer_id: 9 }, 'Printer is offline'],
  ['printer_busy', {}, { printer_id: 9 }, 'Printer is busy'],
  ['printer_busy', {}, { printer_id: 9, until: at(14, 30) }, 'Printer is busy until 14:30'],
  ['quiet_hours', {}, { until: at(5, 30) }, 'Quiet hours until 05:30'],
  ['no_printer_can_run_it', { status: 'pending', printer_id: null, printer: null }, {}, 'No printer can run this job'],
  ['no_printer_can_run_it', { status: 'pending', printer_id: null, printer: null }, { detail: 'no printer has PETG loaded' }, 'No printer has PETG loaded'],
  ['no_printer_can_run_it', { status: 'pending', printer_id: null, printer: null }, { detail: 'no printer has the tag "enclosed"' }, 'No printer has the tag "enclosed"'],
  ['dispatch_refused', {}, { detail: 'Loaded filament does not match the job' }, 'Loaded filament does not match the job'],
  ['dispatch_refused', {}, {}, 'The last dispatch was refused'],
  ['some_new_code', {}, {}, 'Some new code'],
]

describe.each([['table row', row], ['phone card', card]] as const)('waiting reason on the %s', (_name, mount) => {
  it.each(CASES)('%s %j %j reads "%s"', (code, over, extra, text) => {
    mount(job({ ...over, waiting_reason: { code, ...extra } }))
    expect(within(line()).getByRole('status')).toHaveTextContent(new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`))
  })

  it.each(['printing', 'paused', 'completed', 'failed', 'cancelled'])('a %s job shows no waiting line, even if one is sent', status => {
    mount(job({ status, waiting_reason: { code: 'printer_offline', printer_id: 9 } }))
    expect(screen.queryByTestId('job-waiting-reason')).toBeNull()
  })

  it('a job with no reason shows no line', () => {
    mount(job({ waiting_reason: null }))
    expect(screen.queryByTestId('job-waiting-reason')).toBeNull()
  })

  it('the mapping button opens the job editor, which holds the filament mapping', () => {
    const onAction = mount(job({ waiting_reason: { code: 'needs_filament_mapping' } }))
    fireEvent.click(within(line()).getByRole('button', { name: 'Choose slots' }))
    expect(onAction).toHaveBeenCalledWith('edit', 4)
  })

  it('offers no mapping button to someone who cannot edit jobs', () => {
    perms.allowed = false
    mount(job({ waiting_reason: { code: 'needs_filament_mapping' } }))
    expect(line()).toHaveTextContent('Needs a filament slot chosen for each colour')
    expect(within(line()).queryByRole('button')).toBeNull()
  })

  it.each(['printer_offline', 'printer_busy'])('%s opens the printer only when the server names one', code => {
    const onAction = mount(job({ waiting_reason: { code, printer_id: 9 } }))
    fireEvent.click(within(line()).getByRole('button', { name: 'Open printer' }))
    expect(onAction).toHaveBeenCalledWith('openPrinter', 9)
    cleanup()
    mount(job({ waiting_reason: { code } }))  // a printer the caller may not see: the line, no button
    expect(within(line()).queryByRole('button')).toBeNull()
  })

  it.each(['awaiting_approval', 'on_hold', 'quiet_hours', 'no_printer_can_run_it', 'dispatch_refused'])('%s has no button', code => {
    mount(job({ status: code === 'awaiting_approval' ? 'submitted' : 'scheduled', waiting_reason: { code, printer_id: 9 } }))
    expect(within(line()).queryByRole('button')).toBeNull()
  })
})
