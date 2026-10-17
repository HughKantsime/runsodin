// Timeline drag-to-move follows the RBAC action timeline.move (Settings > Access > Permissions).
// Real Timeline page and real permissions module: /api/auth/me and /api/permissions return the producer
// wire captured by tests/test_contracts/test_cert_D_jobsC_wire.py. Only the HTTP boundary is replaced.
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Timeline from './Timeline'
import { clearSensitiveBrowserState, refreshPermissions } from '../../permissions'
import wire from '../../test-fixtures/certD-jobsC-wire.json'

const api = vi.hoisted(() => ({ timeline: vi.fn(), get: vi.fn() }))
vi.mock('../../api', () => ({
  timeline: { get: api.timeline }, printers: { list: vi.fn(async () => wire.printers) },
  jobActions: { move: vi.fn() }, jobs: { get: api.get }, printJobs: { get: vi.fn() },
}))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))

async function signIn(me: unknown, permissions: unknown) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const body = String(input) === '/api/auth/me' ? me : permissions
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
  expect(await refreshPermissions()).not.toBeNull()
}
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter><Timeline /></MemoryRouter></QueryClientProvider>)
}
async function scheduledBlock(view: ReturnType<typeof mount>) {
  await screen.findAllByText('CertC order scheduled')
  const row = view.container.querySelector('.timeline-grid[data-printer-id="10"]') as HTMLElement
  return within(row).getByText('CertC order scheduled').parentElement!.parentElement!
}
declare const process: { env: Record<string, string | undefined> }
beforeEach(async () => {
  await clearSensitiveBrowserState()
  process.env.TZ = 'UTC'
  vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-10-12T12:00:00Z'))
  api.timeline.mockResolvedValue(wire.timeline)
  api.get.mockImplementation(async (id: number) => (wire as any)[`job_${id}`])
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks(); vi.unstubAllGlobals() })

it('operator (granted timeline.move by default) can drag a scheduled job', async () => {
  await signIn(wire.me_operator, wire.permissions)
  expect(await scheduledBlock(mount())).toHaveClass('cursor-grab')
})

it('viewer (not granted timeline.move) cannot drag; clicking the block opens its details instead', async () => {
  await signIn(wire.me_viewer, wire.permissions)
  const block = await scheduledBlock(mount())
  expect(block).not.toHaveClass('cursor-grab')
  fireEvent.click(block)
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(203))
})

it('a saved permission that grants viewers timeline.move lets them drag', async () => {
  const permissions = { ...wire.permissions, action_access: { ...wire.permissions.action_access, 'timeline.move': ['admin', 'operator', 'viewer'] } }
  await signIn(wire.me_viewer, permissions)
  expect(await scheduledBlock(mount())).toHaveClass('cursor-grab')
})
