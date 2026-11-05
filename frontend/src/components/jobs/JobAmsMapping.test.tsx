import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import TimelineJobDetail from './TimelineJobDetail'
import JobAmsMapping, { JobAmsMappingPanel } from './JobAmsMapping'
import { EditJobModal } from './JobModals'
import fixture from '../../test-fixtures/schedule-ams-wire.json'
import type { ArchiveAmsPreview } from '../../types/archive'
const wire = { ...fixture, preview: fixture.preview as ArchiveAmsPreview, job: { ...fixture.job, ams_preview: fixture.job.ams_preview as ArchiveAmsPreview } }
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
  await waitFor(() => expect(api.update).toHaveBeenCalledWith(wire.job.id, { ams_mapping: [{ source_slot: 1, target_slot: 3 }] }))
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


test('missing mapping explains verified defaults and explicit confirmation', () => {
  mount(<JobAmsMapping job={{ ...wire.job, ams_mapping: null }} />)
  expect(screen.getByText(/Without a saved mapping/)).toHaveTextContent('exact matches, need your confirmation')
  expect(api.update).not.toHaveBeenCalled()
})

test('held plan explains missing load and release becomes available with live matching trays', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const held = { ...wire.job, hold: true, ams_preview: { ...wire.preview, planned_loads: ['load PETG in AMS 1 slot 2'] } }
  const view = render(<QueryClientProvider client={client}><JobAmsMapping job={held} /></QueryClientProvider>)
  expect(screen.getByText('Planned: load PETG in AMS 1 slot 2')).toBeInTheDocument()
  const blocked = screen.getByRole('button', { name: 'Release hold — load PETG in AMS 1 slot 2' })
  expect(blocked).toBeDisabled()
  fireEvent.click(blocked)
  expect(api.update).not.toHaveBeenCalled()
  view.rerender(<QueryClientProvider client={client}><JobAmsMapping job={{ ...held, ams_preview: { ...wire.preview, planned_loads: [] } }} /></QueryClientProvider>)
  expect(screen.queryByText(/^Planned:/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Release hold' }))
  await waitFor(() => expect(api.update).toHaveBeenCalledWith(wire.job.id, { hold: false }))
})

test('release refusal is visible and viewer has no release control', async () => {
  const held = { ...wire.job, hold: true, ams_preview: { ...wire.preview, planned_loads: [] } }
  api.update.mockRejectedValue(new Error('Selected tray material does not match the source filament'))
  const view = mount(<JobAmsMapping job={held} />)
  fireEvent.click(screen.getByRole('button', { name: 'Release hold' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Selected tray material does not match')
  view.unmount(); api.allowed = false; mount(<JobAmsMapping job={held} />)
  expect(screen.queryByRole('button', { name: 'Release hold' })).not.toBeInTheDocument()
})


test.each(['panel', 'timeline'])('%s refreshes planned loads while the held detail stays open', async mode => {
  const held = { ...wire.job, hold: true, ams_preview: { ...wire.preview, planned_loads: ['load PETG in AMS 1 slot 2'] } }
  api.get.mockResolvedValueOnce(held).mockResolvedValue({ ...held, ams_preview: { ...wire.preview, planned_loads: [] } })
  mount(mode === 'panel' ? <JobAmsMappingPanel jobId={held.id} /> : <TimelineJobDetail block={{ job_id: held.id, start: '2026-10-27T10:00:00Z', end: '2026-10-27T11:00:00Z' }} />)
  expect(await screen.findByText('Planned: load PETG in AMS 1 slot 2')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Release hold' })).toBeEnabled(), { timeout: 6500 })
  expect(api.get).toHaveBeenCalledTimes(2)
}, 8000)

test('unheld jobs never show a planned label or release control', () => {
  mount(<JobAmsMapping job={{ ...wire.job, hold: false, ams_preview: { ...wire.preview, planned_loads: ['load PETG in AMS 1 slot 2'] } }} />)
  expect(screen.queryByText(/^Planned:/)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Release hold/ })).not.toBeInTheDocument()
})
