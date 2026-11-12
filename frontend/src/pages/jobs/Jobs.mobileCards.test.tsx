// Mobile web pass, slice 1: Jobs renders stacked cards under 640 px and the unchanged
// table at 640 px and above. Same harness as JobsBulk.certDops.cert.test.tsx.
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Jobs from './Jobs'
import wire from '../../test-fixtures/certDops-bulk-jobs-wire.json'

const h = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))
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

const PHONE = '(max-width: 639px)'
const clients: QueryClient[] = []
function viewport(phone: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: phone && q === PHONE, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
}
function show() {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c)
  c.setQueryData(['auth', 'me'], { id: 3, role: 'admin', group_id: null })
  return render(<QueryClientProvider client={c}><MemoryRouter><Jobs /></MemoryRouter></QueryClientProvider>)
}
beforeEach(() => { vi.clearAllMocks(); h.list.mockResolvedValue(wire.jobs) })
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.unstubAllGlobals() })

describe('Jobs phone cards vs desktop table', () => {
  it('renders one card per job and no table under 640 px', async () => {
    viewport(true)
    const { container } = show()
    const cards = await screen.findAllByTestId('job-card')
    expect(cards).toHaveLength(wire.jobs.length)
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(container.querySelector('[aria-label="Jobs table"]')).toBeNull()
    expect(within(cards[0]).getByRole('checkbox', { name: 'Select job Owned ordinary batch' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Select all jobs' })).toBeInTheDocument()
  })

  it('card checkbox and select-all drive the same selection state', async () => {
    viewport(true)
    show()
    const cards = await screen.findAllByTestId('job-card')
    fireEvent.click(within(cards[0]).getByRole('checkbox'))
    expect(await screen.findByText('1 selected')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all jobs' }))
    expect(await screen.findByText(`${wire.jobs.length} selected`)).toBeInTheDocument()
  })

  it('keeps the table, its label and its select-all at 640 px and above', async () => {
    viewport(false)
    const { container } = show()
    await screen.findAllByText('Owned ordinary batch')
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(container.querySelector('[aria-label="Jobs table"]')).not.toBeNull()
    expect(screen.queryAllByTestId('job-card')).toHaveLength(0)
    expect(screen.getByRole('checkbox', { name: 'Select all jobs' })).toBeInTheDocument()
  })

  it('falls back to the table when matchMedia is unavailable', async () => {
    show()
    await screen.findAllByText('Owned ordinary batch')
    expect(screen.getByRole('table')).toBeInTheDocument()
  })
})
