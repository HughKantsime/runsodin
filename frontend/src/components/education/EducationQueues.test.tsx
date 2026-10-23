import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import EducationQueues from './EducationQueues'
import { education } from '../../api'
vi.mock('../../api', () => ({ education: { queue: vi.fn(), reorderQueue: vi.fn(), archiveSubmission: vi.fn() } }))
afterEach(cleanup)
const item = (id: number, section = 'waiting') => ({ id, job_id: id, item_name: `Job ${id}`, submitter_username: 'student', center_name: 'Class A', cost_center_id: 7, job_status: 'scheduled', lifecycle_revision: 1, section, can_reorder: section === 'waiting', can_archive: false })
const group = { printer_id: 9, printer_name: 'P1S', revision: 'r'.repeat(64), items: [item(1), item(2), item(3, 'committed'), item(4, 'printing')] }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(education.queue).mockResolvedValue({ printers: [group] } as any); vi.mocked(education.reorderQueue).mockResolvedValue({ revision: 'new', job_ids: [2, 1] }) })
function mount(kind: 'jobs' | 'archive' = 'jobs', readOnly = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><EducationQueues kind={kind} readOnly={readOnly} audience="teacher" orgId={7} /></QueryClientProvider>)
}
it('keyboard buttons send the full waiting order and revision', async () => {
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Move Job 2 up' }))
  await waitFor(() => expect(education.reorderQueue).toHaveBeenCalledWith(9, { revision: group.revision, job_ids: [2, 1] }))
  expect(screen.queryByRole('button', { name: 'Move Job 3 up' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Move Job 4 up' })).not.toBeInTheDocument()
})
it('drag sends the same reorder contract', async () => {
  mount()
  const source = (await screen.findByText('Job 2')).closest('[draggable]')!
  const target = screen.getByText('Job 1').closest('[draggable]')!
  fireEvent.dragStart(source)
  fireEvent.dragOver(target)
  fireEvent.drop(target)
  await waitFor(() => expect(education.reorderQueue).toHaveBeenCalledWith(9, { revision: group.revision, job_ids: [2, 1] }))
})
it('409 refreshes rather than silently overwriting a concurrent order', async () => {
  vi.mocked(education.reorderQueue).mockRejectedValue(Object.assign(new Error('conflict'), { status: 409 }))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Move Job 2 up' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Queue changed')
  await waitFor(() => expect(education.queue).toHaveBeenCalledTimes(2))
})
it('platform views are read only', async () => {
  mount('jobs', true)
  await screen.findByText('Job 1')
  expect(screen.queryByRole('button', { name: /Move/ })).not.toBeInTheDocument()
  expect(education.queue).toHaveBeenCalledWith('jobs', { orgId: 7, archived: 'all' })
})
it('archive groups and filters work and already archived rows are read only', async () => {
  vi.mocked(education.queue).mockResolvedValue({ items: [{ ...item(1), can_archive: true }, { ...item(2), archived: true }] } as any)
  mount('archive')
  await screen.findByText('Job 1')
  expect(screen.getByRole('heading', { name: 'Class A' })).toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: 'Archive' })).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Archive' }))
  await waitFor(() => expect(education.archiveSubmission).toHaveBeenCalledWith(1, expect.objectContaining({ revision: 1 })))
  fireEvent.change(screen.getByLabelText('Archived work'), { target: { value: 'archived' } })
  await waitFor(() => expect(education.queue).toHaveBeenCalledWith('archive', { orgId: 7, archived: 'archived' }))
})
