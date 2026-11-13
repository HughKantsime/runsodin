import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import StartNextPanel, { IdleS1StartNext } from './StartNextPanel'
import wire from '../../test-fixtures/s1-start-next-suggestion-wire.json'

const api = vi.hoisted(() => ({ suggestion: vi.fn(), start: vi.fn(), canDo: vi.fn(), role: { current: 'admin' } }))
vi.mock('../../api/jobCompletion', () => ({ jobCompletion: { suggestion: api.suggestion, start: api.start } }))
vi.mock('../../permissions', () => ({ canDo: api.canDo, getCurrentUser: () => ({ role: api.role.current }) }))
const S1 = { id: 4, api_type: 'moonraker', model: 'Kobra S1', gcode_state: 'STANDBY' }
const SUGGESTION = { job_id: 12, reason: 'Scheduler queue order', filament: 'verify_needed', fingerprint: 'f'.repeat(64), load: [] }

const BUTTON = { name: 'Bed clear, start next' }
const OWNER = 'I checked the printer and cleared the bed.'
const RELAYED = 'The owner confirmed to me that the printer is checked and the bed is clear.'

beforeEach(() => {
  vi.clearAllMocks()
  api.role.current = 'admin'
  api.canDo.mockReturnValue(true)
  api.suggestion.mockResolvedValue(SUGGESTION)
  api.start.mockResolvedValue({ success: true, state: 'dispatched' })
})
afterEach(cleanup)
const client = () => new QueryClient({ defaultOptions: { queries: { retry: false } } })
const mount = (printer: typeof S1) => render(<QueryClientProvider client={client()}><IdleS1StartNext printer={printer} /></QueryClientProvider>)

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

test('someone other than the owner relays the owner\'s word, and says so', async () => {
  api.role.current = 'operator'
  mount(S1)
  fireEvent.click(await screen.findByLabelText(RELAYED))
  expect(screen.queryByLabelText(OWNER)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', BUTTON))
  await waitFor(() => expect(api.start).toHaveBeenCalledTimes(1))
  expect(api.start.mock.calls[0][1].attestation).toBe('owner_confirmed_to_operator')
})

test('the owner attests in the app', async () => {
  mount(S1)
  fireEvent.click(await screen.findByLabelText(OWNER))
  fireEvent.click(screen.getByRole('button', BUTTON))
  await waitFor(() => expect(api.start).toHaveBeenCalledTimes(1))
  expect(api.start.mock.calls[0][1].attestation).toBe('owner_in_app')
})

test('without jobs.start the suggestion shows but nothing can be started', async () => {
  api.canDo.mockImplementation((action: string) => action !== 'jobs.start')
  mount(S1)
  expect(await screen.findByText(/Next: job 12/)).toBeInTheDocument()
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', BUTTON)).not.toBeInTheDocument()
})

test('a changed suggestion clears the tick, so an old confirmation can\'t start the new job', async () => {
  const queries = client()
  render(<QueryClientProvider client={queries}><IdleS1StartNext printer={S1} /></QueryClientProvider>)
  fireEvent.click(await screen.findByLabelText(OWNER))
  const button = screen.getByRole('button', BUTTON)
  expect(button).toBeEnabled()
  api.suggestion.mockResolvedValue({ ...SUGGESTION, job_id: 13, fingerprint: 'e'.repeat(64) })
  await queries.invalidateQueries({ queryKey: ['next-suggestion', 4] })
  expect(await screen.findByText(/Next: job 13/)).toBeInTheDocument()
  expect(screen.getByRole('checkbox')).not.toBeChecked()
  expect(button).toBeDisabled()
  fireEvent.click(button)
  expect(api.start).not.toHaveBeenCalled()
})

test('a job the S1 can\'t start says why, and the start itself refuses it', async () => {
  api.suggestion.mockResolvedValue(wire.unstartable)
  api.start.mockResolvedValue({ success: false, state: 'refused', message: 'Choose an ACE slot for every file filament' })
  mount(S1)
  expect(await screen.findByText(/Job 12 can't start: Choose an ACE slot/)).toBeInTheDocument()
  const button = screen.getByRole('button', BUTTON)
  expect(button).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(button)
  await waitFor(() => expect(api.start).toHaveBeenCalledTimes(1))
  expect(api.start.mock.calls[0][1].suggestion_fingerprint).toBe(wire.unstartable.fingerprint)
  expect(await screen.findByRole('alert')).toHaveTextContent('Choose an ACE slot for every file filament')
  expect(screen.queryByText('Next job dispatched.')).not.toBeInTheDocument()
})

test('a suggestion with no fingerprint can\'t be sent', async () => {
  api.suggestion.mockResolvedValue(wire.no_fingerprint)
  render(<QueryClientProvider client={client()}><StartNextPanel printerId={1} /></QueryClientProvider>)
  const button = await screen.findByRole('button', BUTTON)
  fireEvent.click(screen.getByRole('checkbox'))
  expect(button).toBeDisabled()
  fireEvent.click(button)
  expect(api.start).not.toHaveBeenCalled()
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
