import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import TimelineJobDetail from './TimelineJobDetail'
import JobAmsMapping from './JobAmsMapping'
import { EditJobModal } from './JobModals'
import wire from '../../test-fixtures/schedule-ams-wire.json'
const api = vi.hoisted(() => ({ update: vi.fn(), get: vi.fn(), allowed: true }))
vi.mock('../../api', () => ({ jobs: { update: api.update, get: api.get }, printJobs: {} }))
vi.mock('../../api/jobCompletion', () => ({ jobCompletion: { timeline: async () => ({ events: [] }) } }))
vi.mock('../../permissions', () => ({ canDo: () => api.allowed }))
beforeEach(() => { vi.clearAllMocks(); api.allowed = true; api.update.mockResolvedValue(wire.job); api.get.mockResolvedValue(wire.job) })
afterEach(cleanup)
function mount(element = <JobAmsMapping job={wire.job} />) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{element}</QueryClientProvider>)
}

test('actual job detail shows saved mapping and saves an explicit change', async () => {
  mount(<TimelineJobDetail block={{ job_id: wire.job.id, start: '2026-10-27T10:00:00Z', end: '2026-10-27T11:00:00Z' }} />)
  expect(await screen.findByText(/Saved AMS mapping:/)).toHaveTextContent('Filament 1 → Slot 4')
  fireEvent.change(screen.getByLabelText('Filament 1 AMS slot'), { target: { value: '3' } })
  expect(api.update).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Save AMS mapping' }))
  await waitFor(() => expect(api.update).toHaveBeenCalledWith(wire.job.id, { ams_mapping: [{ source_slot: 0, target_slot: 3 }] }))
})

test('clear sends explicit null, failures remain visible', async () => {
  api.update.mockRejectedValue(new Error('Job has an active dispatch claim'))
  mount(); fireEvent.click(screen.getByRole('button', { name: 'Clear AMS mapping' }))
  await waitFor(() => expect(api.update).toHaveBeenCalledWith(wire.job.id, { ams_mapping: null }))
  expect(await screen.findByRole('alert')).toHaveTextContent('active dispatch claim')
})

test.each(['printing', 'completed'])('a %s job mapping is read only', status => {
  mount(<JobAmsMapping job={{ ...wire.job, status }} />)
  expect(screen.getByText(/Saved AMS mapping:/)).toHaveTextContent('Slot 4')
  expect(screen.queryByRole('button', { name: 'Save AMS mapping' })).not.toBeInTheDocument()
})

test('Education/claimed job response and viewer permission hide edit controls', () => {
  const view = mount(<JobAmsMapping job={{ ...wire.job, ams_mapping_editable: false }} />)
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
  view.unmount(); api.allowed = false; mount()
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})


test('pending queue edit dialog loads and edits its mapping before scheduler placement', async () => {
  mount(<EditJobModal isOpen onClose={vi.fn()} onSubmit={vi.fn()} job={wire.job} modelsData={[]} printersData={[]} />)
  expect(await screen.findByLabelText('Filament 1 AMS slot')).toHaveValue('4')
  fireEvent.click(screen.getByRole('button', { name: 'Clear AMS mapping' }))
  await waitFor(() => expect(api.update).toHaveBeenCalledWith(wire.job.id, { ams_mapping: null }))
})
