// cert-D ops: F18 Backup & Restore in Settings -> System, against the producer
// wire captured by tests/test_contracts/test_cert_D_ops_backup.py. The real
// backups client and BackupRestore panel run; only network transport is stubbed.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SystemTab from './SystemTab'
import wire from '../../test-fixtures/certDops-backup-wire.json'

vi.mock('../../api', async () => {
  const real = await vi.importActual<typeof import('../../api/system')>('../../api/system')
  return {
    backups: real.backups,
    chargebacks: { report: vi.fn().mockResolvedValue([]) },
    reportSchedules: { list: vi.fn().mockResolvedValue([]), create: vi.fn(), update: vi.fn(), delete: vi.fn(), runNow: vi.fn() },
    fetchAPI: vi.fn().mockResolvedValue({}), config: { getNetwork: vi.fn().mockResolvedValue({}) },
    downloadBlob: vi.fn(), gdpr: {}, adminBundle: { download: vi.fn() },
  }
})
vi.mock('../../permissions', () => ({ clearSensitiveBrowserState: vi.fn() }))
vi.mock('./TroubleshootingPanel', () => ({ default: () => null }))
vi.mock('./DataRetentionSettings', () => ({ default: () => null }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))

const transport = vi.fn()
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
const sqliteBytes = () => new Uint8Array(wire.backup.download_prefix_hex.match(/../g)!.map(b => parseInt(b, 16)))
let listed: unknown[] = []
beforeEach(() => {
  vi.clearAllMocks(); listed = []
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false }))
  vi.stubGlobal('fetch', transport)
  transport.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === '/api/backups' && init?.method === 'POST') { listed = wire.backup.list; return json(wire.backup.create) }
    if (url === '/api/backups') return json(listed)
    if (url === `/api/backups/${wire.backup.create.filename}`) return new Response(sqliteBytes(), { status: 200, headers: { 'Content-Type': 'application/octet-stream' } })
    if (url === '/api/backups/restore') return json(wire.restore)
    throw new Error('unexpected ' + url)
  })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
function system() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><SystemTab /></QueryClientProvider>)
}

describe('F18 Settings System tab backups', () => {
  it('Create Backup posts to the API, shows the created snapshot and lists it', async () => {
    system()
    expect(await screen.findByText('No backups yet. Create your first backup to protect your data.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create Backup' }))
    expect(await screen.findByText(`Backup created: ${wire.backup.create.filename} (1.4 MB)`)).toBeInTheDocument()
    expect(transport.mock.calls.some(([url, init]) => url === '/api/backups' && init?.method === 'POST')).toBe(true)
    const names = await screen.findAllByText(wire.backup.create.filename)
    expect(names.length).toBeGreaterThanOrEqual(1)
  })

  it('F19-19.1-02 System tab links to the audit log viewer at /audit', async () => {
    system()
    const link = (await screen.findByText('View full audit log →')).closest('a')!
    expect(link).toHaveAttribute('href', '/audit')
    expect(link).toHaveTextContent('Audit Log')
  })

  it('Download fetches the exact backup file and saves it under its name', async () => {
    listed = wire.backup.list
    class OwnedURL extends URL { static createObjectURL = vi.fn(() => 'blob:certdops'); static revokeObjectURL = vi.fn() }
    vi.stubGlobal('URL', OwnedURL)
    const downloads: string[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { downloads.push(this.download) })
    system()
    await screen.findByText(wire.backup.create.filename)
    fireEvent.click(screen.getByTitle('Download backup'))
    await waitFor(() => expect(downloads).toEqual([wire.backup.create.filename]))
    const call = transport.mock.calls.find(([url]) => url === `/api/backups/${wire.backup.create.filename}`)!
    expect(call[1].credentials).toBe('include')
    const blob = (OwnedURL.createObjectURL as any).mock.calls[0][0] as Blob
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(sqliteBytes())
  })

  it('restore panel uploads the chosen .db, confirms, and shows the staged result with safety backup', async () => {
    system()
    expect(await screen.findByText('Restore from Backup')).toBeInTheDocument()
    const input = document.querySelector('input[type=file]') as HTMLInputElement
    expect(input.accept).toBe('.db,.dump')
    const file = new File([sqliteBytes()], 'candidate.db', { type: 'application/octet-stream' })
    fireEvent.change(input, { target: { files: [file] } })
    expect(screen.getByText('candidate.db')).toBeInTheDocument()
    expect(transport.mock.calls.some(([url]) => url === '/api/backups/restore')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Stage Restore' }))
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent(`Pre-restore database backup: ${wire.restore.pre_restore_backup}`)
    expect(status).toHaveTextContent('Restart ODIN during your maintenance window to apply it.')
    const [, init] = transport.mock.calls.find(([url]) => url === '/api/backups/restore')!
    expect(init.method).toBe('POST')
    expect((init.body as FormData).get('file')).toBe(file)
  })
})
