// Cert-E F10-10.6-02: Settings -> System -> Data Export downloads the real backend CSV bytes.
// The CSV body is the producer wire fixture captured by tests/test_contracts/test_cert_E_ana_backend.py
// (GET /export/jobs?status=completed); only the network (fetch) and blob URL browser APIs are stubbed.
import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Settings from './Settings'
import exportWire from '../../test-fixtures/cert-E-ana-export-jobs.json'

vi.mock('../../api', async () => {
  const client = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    downloadBlob: client.downloadBlob,
    pricingConfig: { get: vi.fn().mockResolvedValue({ ui_mode: 'advanced' }) },
    fetchAPI: vi.fn().mockResolvedValue({}), config: { getNetwork: vi.fn().mockResolvedValue({}) },
    backups: { list: vi.fn().mockResolvedValue([]) }, gdpr: {}, adminBundle: {},
  }
})
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true }) }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ role: 'admin' }), clearSensitiveBrowserState: vi.fn() }))
vi.mock('./Admin', () => ({ default: () => null }))
vi.mock('./Permissions', () => ({ default: () => null }))
vi.mock('./Branding', () => ({ default: () => null }))
vi.mock('../../components/admin/OIDCSettings', () => ({ default: () => null }))
vi.mock('../../components/admin/WebhookSettings', () => ({ default: () => null }))
vi.mock('../../components/admin/GroupManager', () => ({ default: () => null }))
vi.mock('../../components/auth/MFASetup', () => ({ default: () => null }))
vi.mock('../../components/auth/APITokenManager', () => ({ default: () => null }))
vi.mock('../../components/auth/SessionManager', () => ({ default: () => null }))
vi.mock('../../components/admin/QuotaManager', () => ({ default: () => null }))
vi.mock('../../components/admin/IPAllowlistSettings', () => ({ default: () => null }))
vi.mock('../../components/admin/OrgManager', () => ({ default: () => null }))
vi.mock('../../components/admin/LogViewer', () => ({ default: () => null }))
vi.mock('../../components/admin/NotificationsTab', () => ({ default: () => null }))
vi.mock('../../components/admin/GeneralTab', () => ({ default: () => null }))
vi.mock('../../components/admin/VisionSettingsTab', () => ({ default: () => null }))
vi.mock('../../components/admin/LicenseTab', () => ({ default: () => null }))
vi.mock('../../components/admin/BackupRestore', () => ({ default: () => null }))
vi.mock('../../components/admin/TroubleshootingPanel', () => ({ default: () => null }))
vi.mock('../../components/admin/DataRetentionSettings', () => ({ default: () => null }))
vi.mock('../../components/admin/ReportScheduleManager', () => ({ default: () => null }))
vi.mock('../../components/admin/ChargebackReport', () => ({ default: () => null }))
vi.mock('../../components/shared/ProBadge', () => ({ default: () => null }))

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

test('F10-10.6-02 Settings > System > Data Export downloads the backend jobs CSV bytes', async () => {
  const fetchSpy = vi.fn().mockResolvedValue(new Response(exportWire.body, { status: 200, headers: { 'Content-Type': exportWire.content_type } }))
  vi.stubGlobal('fetch', fetchSpy)
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  const blobs: Blob[] = []
  vi.spyOn(URL, 'createObjectURL').mockImplementation(b => { blobs.push(b as Blob); return 'blob:cert' })
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter><Settings /></MemoryRouter></QueryClientProvider>)
  fireEvent.click(screen.getByRole('button', { name: /System/ }))
  await screen.findByRole('heading', { name: 'Data Export' })
  for (const name of ['Jobs', 'Models', 'Spools', 'Filament Usage']) expect(screen.getByRole('button', { name })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Jobs' }))
  await waitFor(() => expect(click).toHaveBeenCalledTimes(1))
  expect(fetchSpy.mock.calls[0][0]).toBe('/api/export/jobs')
  expect(click.mock.instances[0]).toHaveProperty('download', 'jobs_export.csv')
  const textOf = await new Promise<string>((ok, bad) => { const r = new FileReader(); r.onload = () => ok(String(r.result)); r.onerror = bad; r.readAsText(blobs[0]) })
  expect(textOf).toBe(exportWire.body)
  client.clear()
})
