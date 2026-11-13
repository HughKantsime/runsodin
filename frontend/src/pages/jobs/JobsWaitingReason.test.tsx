// The waiting line's buttons on the Jobs page: "Choose slots" opens the job editor with the
// filament mapping, "Open printer" goes to the printer. Same harness as Jobs.mobileCards.test.tsx.
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Jobs from './Jobs'
import wire from '../../test-fixtures/certDops-bulk-jobs-wire.json'

const h = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn() }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))
vi.mock('../../api', async () => {
  const real = await vi.importActual<typeof import('../../api/printers')>('../../api/printers')
  return {
    jobs: { list: h.list, get: h.get, reorder: vi.fn(), create: vi.fn(), update: vi.fn(), start: vi.fn(), complete: vi.fn(), cancel: vi.fn(), delete: vi.fn(), repeat: vi.fn(), dispatch: vi.fn(), recentTerminal: vi.fn().mockResolvedValue([]) },
    models: { list: vi.fn().mockResolvedValue([]) }, modelRevisions: { list: vi.fn().mockResolvedValue([]) },
    printers: { list: vi.fn().mockResolvedValue([]) },
    getApprovalSetting: vi.fn().mockResolvedValue({ require_approval: false }),
    presets: { list: vi.fn().mockResolvedValue([]), create: vi.fn(), schedule: vi.fn(), delete: vi.fn() },
    scheduler: { run: vi.fn() }, bulkOps: real.bulkOps,
    approveJob: vi.fn(), rejectJob: vi.fn(), resubmitJob: vi.fn(), updateJobFailure: vi.fn(), timeline: { get: vi.fn() }, jobActions: { move: vi.fn() }, printJobs: { get: vi.fn() },
  }
})
vi.mock('../../permissions', () => ({ canDo: () => true, getCurrentUser: () => ({ id: 3, role: 'admin' }), clearSensitiveBrowserState: vi.fn() }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: null }) }))

const clients: QueryClient[] = []
function show() {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c)
  c.setQueryData(['auth', 'me'], { id: 3, role: 'admin', group_id: null })
  return render(<QueryClientProvider client={c}><MemoryRouter initialEntries={['/jobs']}><Routes>
    <Route path="/jobs" element={<Jobs />} />
    <Route path="/printers/:id" element={<p>Printer page</p>} />
  </Routes></MemoryRouter></QueryClientProvider>)
}
const waiting = (reason: any) => [{ ...wire.jobs[0], status: 'scheduled', printer_id: 9, printer: { id: 9, name: 'P1S-09' }, ams_mapping: null, waiting_reason: reason }, wire.jobs[1]]

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0 })

it('"Choose slots" opens the job editor with the filament mapping panel', async () => {
  const jobs = waiting({ code: 'needs_filament_mapping' })
  h.list.mockResolvedValue(jobs)
  h.get.mockResolvedValue({ ...jobs[0], ams_preview: null, ams_mapping_editable: false })
  show()
  const line = await screen.findByTestId('job-waiting-reason')
  expect(line).toHaveTextContent('Needs a filament slot chosen for each colour')
  expect(screen.queryByRole('dialog')).toBeNull()
  fireEvent.click(within(line).getByRole('button', { name: 'Choose slots' }))
  const editor = await screen.findByRole('dialog')
  expect(editor).toHaveTextContent(`Edit Job (Job ${jobs[0].id})`)
  expect(await within(editor).findByRole('region', { name: 'Job AMS mapping' })).toBeInTheDocument()
  expect(h.get).toHaveBeenCalledWith(jobs[0].id)
})

it('"Open printer" goes to the printer the job is waiting on', async () => {
  h.list.mockResolvedValue(waiting({ code: 'printer_offline', printer_id: 9 }))
  show()
  const line = await screen.findByTestId('job-waiting-reason')
  expect(line).toHaveTextContent('Printer is offline')
  fireEvent.click(within(line).getByRole('button', { name: 'Open printer' }))
  expect(await screen.findByText('Printer page')).toBeInTheDocument()
})

it('only the waiting job has a line', async () => {
  h.list.mockResolvedValue(waiting({ code: 'quiet_hours' }))
  show()
  expect(await screen.findAllByTestId('job-waiting-reason')).toHaveLength(1)
})
