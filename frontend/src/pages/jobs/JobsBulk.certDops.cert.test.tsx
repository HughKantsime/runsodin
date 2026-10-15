// cert-D ops: F04-4.3-05 / F19-19.2-01 bulk job toolbar against the captured
// producer wire (tests/test_contracts/test_cert_D_ops_bulk_audit.py). The real
// bulkOps client and fetchAPI run; only the network transport is stubbed.
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Jobs from './Jobs'
import wire from '../../test-fixtures/certDops-bulk-jobs-wire.json'

const h = vi.hoisted(() => ({ list: vi.fn(), success: vi.fn(), error: vi.fn() }))
vi.mock('react-hot-toast', () => ({ default: { success: h.success, error: h.error } }))
vi.mock('../../api', async () => {
  const real = await vi.importActual<typeof import('../../api/printers')>('../../api/printers')
  return {
    jobs: { list: h.list, reorder: vi.fn(), create: vi.fn(), update: vi.fn(), start: vi.fn(), complete: vi.fn(), cancel: vi.fn(), delete: vi.fn(), repeat: vi.fn(), dispatch: vi.fn(), recentTerminal: vi.fn().mockResolvedValue([]) },
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

type Action = 'cancel' | 'set_priority' | 'reschedule' | 'delete'
const transport = vi.fn()
const clients: QueryClient[] = []
function show() {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c)
  c.setQueryData(['auth', 'me'], { id: 3, role: 'admin', group_id: null })
  return render(<QueryClientProvider client={c}><MemoryRouter><Jobs /></MemoryRouter></QueryClientProvider>)
}
function bulkCalls() { return transport.mock.calls.filter(([url]) => String(url).endsWith('/jobs/bulk-update')) }
async function selectBoth() {
  await screen.findAllByText('Owned ordinary batch')
  const boxes = screen.getAllByRole('checkbox', { name: 'Select job Owned ordinary batch' })
  expect(boxes).toHaveLength(2)
  boxes.forEach(b => fireEvent.click(b))
  expect(await screen.findByText('2 selected')).toBeInTheDocument()
}
beforeEach(() => {
  vi.clearAllMocks()
  h.list.mockResolvedValue(wire.jobs)
  vi.stubGlobal('fetch', transport)
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.unstubAllGlobals() })

describe('F04-4.3-05 / F19-19.2-01 multi-select bulk job toolbar', () => {
  it('per-row checkboxes and select-all drive the selected count and toolbar', async () => {
    show(); await screen.findAllByText('Owned ordinary batch')
    expect(screen.queryByText(/selected$/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all jobs' }))
    expect(await screen.findByText('2 selected')).toBeInTheDocument()
    for (const name of ['Cancel', 'Priority 1', 'Reschedule', 'Delete']) expect(screen.getByRole('button', { name })).toBeEnabled()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all jobs' }))
    await waitFor(() => expect(screen.queryByText('2 selected')).not.toBeInTheDocument())
  })

  it.each([
    ['cancel', 'Cancel', 'Bulk cancel completed'],
    ['set_priority', 'Priority 1', 'Bulk set_priority completed'],
    ['reschedule', 'Reschedule', '2 job(s) returned to queue; 0 unchanged'],
    ['delete', 'Delete', 'Bulk delete completed'],
  ] as [Action, string, string][])('%s sends the exact producer-accepted payload and reports the real response', async (action, label, toastText) => {
    transport.mockImplementation(async () => new Response(JSON.stringify(wire[action].response), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    show(); await selectBoth()
    fireEvent.click(screen.getByRole('button', { name: label }))
    if (action === 'delete') {
      const dialog = await screen.findByRole('alertdialog')
      expect(dialog).toHaveTextContent('Permanently delete 2 selected job(s)?')
      expect(bulkCalls()).toHaveLength(0)
      fireEvent.click(within(dialog).getByRole('button', { name: 'Delete All' }))
    }
    await waitFor(() => expect(bulkCalls()).toHaveLength(1))
    const [url, init] = bulkCalls()[0]
    expect(url).toBe('/api/jobs/bulk-update')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual(wire[action].request)
    await waitFor(() => expect(h.success).toHaveBeenCalledWith(toastText))
    await waitFor(() => expect(screen.queryByText('2 selected')).not.toBeInTheDocument())
  })
})
