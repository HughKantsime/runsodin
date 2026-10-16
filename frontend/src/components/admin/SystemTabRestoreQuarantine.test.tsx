import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import SystemTab from './SystemTab'

// Wire shape of GET /backups for a PostgreSQL install with one quarantined restore
// (backend/modules/system/routes_backup.py list_backups).
const QUARANTINED = {
  filename: 'failed_restore_20261015_101500_000001.dump', size_bytes: 4096, size_mb: 0, created_at: '2026-10-15T10:15:00+00:00',
  quarantined: true, reason: 'Unsupported PostgreSQL archive schema version or partial table inventory',
  action: 'The current database was not changed. Fix the reason above, then re-stage the archive and restart with ODIN_POSTGRES_RESTORE_ACKNOWLEDGEMENT set to its SHA-256.',
}
const api = vi.hoisted(() => ({ list: vi.fn(), stage: vi.fn(), remove: vi.fn(), download: vi.fn() }))
vi.mock('../../api', () => ({
  fetchAPI: vi.fn().mockResolvedValue({}), config: { getNetwork: vi.fn().mockResolvedValue({}) },
  backups: { list: api.list, stage: api.stage, remove: api.remove, download: api.download, create: vi.fn() },
  chargebacks: { report: vi.fn().mockResolvedValue([]) }, reportSchedules: { list: vi.fn().mockResolvedValue([]) },
  downloadBlob: vi.fn(), getEnergyRate: vi.fn().mockResolvedValue({ energy_cost_per_kwh: 0.2 }), setEnergyRate: vi.fn(),
}))
vi.mock('../../permissions', () => ({ clearSensitiveBrowserState: vi.fn() }))
vi.mock('./BackupRestore', () => ({ default: () => null }))
vi.mock('./TroubleshootingPanel', () => ({ default: () => null }))
vi.mock('./DataRetentionSettings', () => ({ default: () => null }))
vi.mock('recharts', () => ({ ResponsiveContainer: ({ children }: any) => <div>{children}</div>, BarChart: () => null, Bar: () => null, XAxis: () => null, YAxis: () => null, CartesianGrid: () => null, Tooltip: () => null }))
beforeEach(() => {
  api.list.mockResolvedValue([QUARANTINED, { filename: 'odin_backup_20261014_000000_000000.dump', size_bytes: 2048, created_at: '2026-10-14T00:00:00+00:00' }])
  api.stage.mockResolvedValue({ status: 'staged', message: 'staged', restart_required: true, candidate_sha256: 'a'.repeat(64) })
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false }))
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals() })

function system() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><SystemTab /></QueryClientProvider>)
}

test('a quarantined restore is listed with its reason and can be staged again', async () => {
  system()
  const name = await screen.findByText(QUARANTINED.filename)
  const row = name.closest('div.flex') as HTMLElement
  expect(within(row).getByRole('alert')).toHaveTextContent('Restore not applied (database unchanged).')
  expect(within(row).getByRole('alert')).toHaveTextContent(QUARANTINED.reason)
  fireEvent.click(within(row).getByRole('button', { name: 'Stage again' }))
  await waitFor(() => expect(api.stage).toHaveBeenCalledExactlyOnceWith(QUARANTINED.filename, expect.anything()))
  expect(screen.getAllByRole('button', { name: 'Stage again' })).toHaveLength(1)
})
