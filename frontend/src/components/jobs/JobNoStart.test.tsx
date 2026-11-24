import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { jobs } from '../../api'
import { canDo } from '../../permissions'
import type { Job } from '../../types'
import JobNoStart from './JobNoStart'

vi.mock('../../permissions', () => ({ canDo: vi.fn(() => true) }))
vi.mock('../../api', () => ({ jobs: { recordNoStart: vi.fn() } }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
function mount(over: Partial<Job> = {}) {
  const client = new QueryClient()
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  const job = { id: 15, hold: true, status: 'pending', no_start_state: 'needs_check', ...over } as Job
  render(<QueryClientProvider client={client}><JobNoStart job={job} inTimeline /></QueryClientProvider>)
  return invalidate
}

it('requires an explicit printer check and refreshes both job views on success', async () => {
  vi.mocked(jobs.recordNoStart).mockResolvedValue({ recorded: 2 })
  const invalidate = mount()
  expect(screen.getByText('A start was requested and did not begin')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'It did not start' }))
  const dialog = screen.getByRole('dialog', { name: 'Check the start of job 15' })
  const record = within(dialog).getByRole('button', { name: 'Record check' })
  expect(record).toBeDisabled()
  fireEvent.click(within(dialog).getByRole('checkbox', { name: /I checked the printer/ }))
  fireEvent.click(record)
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(jobs.recordNoStart).toHaveBeenCalledWith(15)
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['job', 15] })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['jobs'] })
})

it('keeps the refusal visible in the dialog', async () => {
  vi.mocked(jobs.recordNoStart).mockRejectedValue(new Error('The printer is reporting a print that matches this job.'))
  mount()
  fireEvent.click(screen.getByText('It did not start'))
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getByText('Record check'))
  expect(await within(screen.getByRole('dialog')).findByRole('alert')).toHaveTextContent('matches this job')
})

it('points back to cancellation after the start check has been recorded', () => {
  mount({ no_start_state: 'recorded' })
  expect(screen.getByRole('status')).toHaveTextContent('You can now cancel')
  expect(screen.getByRole('link', { name: 'Open job' })).toHaveAttribute('href', '/jobs?job=15')
  expect(screen.queryByRole('button')).toBeNull()
})

it('hides the action without cancel permission', () => {
  vi.mocked(canDo).mockReturnValueOnce(false)
  mount()
  expect(canDo).toHaveBeenCalledWith('jobs.cancel')
  expect(screen.queryByRole('button')).toBeNull()
})

it.each([{ hold: false }, { no_start_state: undefined }, { status: 'completed' as const }])('hides the notice for %j', over => {
  mount(over)
  expect(screen.queryByText('A start was requested and did not begin')).toBeNull()
})
