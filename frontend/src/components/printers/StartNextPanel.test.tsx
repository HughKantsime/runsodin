import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { IdleS1StartNext } from './StartNextPanel'

const api = vi.hoisted(() => ({ suggestion: vi.fn(), start: vi.fn() }))
vi.mock('../../api/jobCompletion', () => ({ jobCompletion: { suggestion: api.suggestion, start: api.start } }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
const S1 = { id: 4, api_type: 'moonraker', model: 'Kobra S1', gcode_state: 'STANDBY' }
const SUGGESTION = { job_id: 12, reason: 'Scheduler queue order', filament: 'verify_needed', fingerprint: 'f'.repeat(64), load: [] }

beforeEach(() => { vi.clearAllMocks(); api.suggestion.mockResolvedValue(SUGGESTION); api.start.mockResolvedValue({ success: true, state: 'dispatched' }) })
afterEach(cleanup)
const mount = (printer: typeof S1) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><IdleS1StartNext printer={printer} /></QueryClientProvider>)

test('an idle S1 starts its next job only after the bed-clear confirmation', async () => {
  mount(S1)
  const button = await screen.findByRole('button', { name: 'Bed clear, start next' })
  expect(screen.getByText(/Next: job 12/)).toBeInTheDocument()
  expect(button).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(button)
  await waitFor(() => expect(api.start).toHaveBeenCalledTimes(1))
  expect(api.start).toHaveBeenCalledWith(4, expect.objectContaining({ suggested_job_id: 12, suggestion_fingerprint: 'f'.repeat(64), bed_clear_confirmed: true, attestation: 'owner_in_app' }))
  expect(await screen.findByText('Next job dispatched.')).toBeInTheDocument()
})

test('why a job can\'t start is visible, and nothing can be sent without a fingerprint', async () => {
  api.suggestion.mockResolvedValue({ ...SUGGESTION, reason: "Job 12 can't start: Choose an ACE slot for every file filament. Fix it, or hold it to start later jobs.", fingerprint: null })
  mount(S1)
  expect(await screen.findByText(/Job 12 can't start: Choose an ACE slot/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Bed clear, start next' })).toBeDisabled()
})

test.each([
  [{ ...S1, gcode_state: 'PRINTING' }],
  [{ ...S1, gcode_state: 'PAUSED' }],
  [{ ...S1, gcode_state: 'HEATING' }],
  [{ ...S1, gcode_state: null as unknown as string }],
  [{ ...S1, model: 'Kobra S1 Max' }],
  [{ ...S1, api_type: 'bambu' }],
  [{ ...S1, model: 'Voron 2.4' }],
])('nothing to start on a busy S1 or another printer: %j', printer => {
  mount(printer)
  expect(screen.queryByRole('button', { name: 'Bed clear, start next' })).not.toBeInTheDocument()
  expect(api.suggestion).not.toHaveBeenCalled()
})
