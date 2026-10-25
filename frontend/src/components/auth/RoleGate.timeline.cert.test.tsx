/**
 * /timeline is guarded by RoleGate page="timeline" like /settings, /audit and /education.
 *
 * Real App route table, real ProtectedRoute, real permissions module and real RoleGate. The only boundary
 * is window.fetch, which returns the producer wire captured from a real ODIN server
 * (tests/test_contracts/test_cert_F_C_ui_wire.py; the embedded JSON below is checked against that capture).
 * Heavy page bodies are replaced by headings so the test observes which route actually mounted.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from '../../App'
import { clearSensitiveBrowserState } from '../../permissions'

const WIRE = /*WIRE-BEGIN*/{"roles":{"admin":{"me":{"email":"certadmin@example.test","group_id":null,"id":1,"role":"admin","username":"certadmin"},"permissions":{"action_access":{"alerts.read":["admin","operator","viewer"],"education.reorder":["admin","operator","viewer"],"jobs.approve":["admin","operator"],"jobs.cancel":["admin","operator"],"jobs.complete":["admin","operator"],"jobs.create":["admin","operator"],"jobs.delete":["admin","operator"],"jobs.edit":["admin","operator"],"jobs.reject":["admin","operator"],"jobs.resubmit":["admin","operator","viewer"],"jobs.start":["admin","operator"],"jobs.submit":["admin","operator","viewer"],"maintenance.log":["admin","operator"],"maintenance.tasks":["admin","operator"],"models.create":["admin","operator"],"models.delete":["admin"],"models.edit":["admin","operator"],"models.plates":["admin","operator"],"orders.create":["admin","operator"],"orders.delete":["admin","operator"],"orders.edit":["admin"],"orders.ship":["admin","operator"],"printers.add":["admin"],"printers.delete":["admin"],"printers.dispatch_recovery":["admin"],"printers.edit":["admin","operator"],"printers.plug":["admin","operator"],"printers.reorder":["admin","operator"],"printers.slots":["admin","operator"],"products.assortment":["admin"],"products.assortment_preview":["admin","operator"],"products.create":["admin","operator"],"products.delete":["admin"],"products.edit":["admin","operator"],"products.finishing":["admin","operator"],"products.inspections":["admin","operator"],"products.materials_manage":["admin"],"products.materials_preview":["admin","operator"],"products.materials_receive":["admin","operator"],"products.operations":["admin","operator"],"products.recipes":["admin"],"products.square_manage":["admin"],"products.square_send":["admin","operator"],"settings.edit":["admin"],"spools.delete":["admin","operator"],"spools.edit":["admin","operator"],"timeline.move":["admin","operator"],"upload.delete":["admin","operator"],"upload.schedule":["admin","operator"],"upload.upload":["admin","operator"],"users.restore_superadmin":["admin"]},"page_access":{"admin":["admin"],"alerts":["admin","operator","viewer"],"analytics":["admin","operator","viewer"],"audit":["admin"],"branding":["admin"],"calculator":["admin","operator","viewer"],"cameras":["admin","operator","viewer"],"dashboard":["admin","operator","viewer"],"education":["admin","operator","viewer"],"education_reports":["admin","operator"],"jobs":["admin","operator","viewer"],"maintenance":["admin","operator"],"models":["admin","operator","viewer"],"orders":["admin","operator","viewer"],"printers":["admin","operator","viewer"],"products":["admin","operator","viewer"],"settings":["admin"],"spools":["admin","operator","viewer"],"timeline":["admin","operator","viewer"],"upload":["admin","operator"]}}},"operator":{"me":{"email":"ui_operator@example.test","group_id":null,"id":2,"role":"operator","username":"ui_operator"},"permissions":{"action_access":{"alerts.read":["admin","operator","viewer"],"education.reorder":["admin","operator","viewer"],"jobs.approve":["admin","operator"],"jobs.cancel":["admin","operator"],"jobs.complete":["admin","operator"],"jobs.create":["admin","operator"],"jobs.delete":["admin","operator"],"jobs.edit":["admin","operator"],"jobs.reject":["admin","operator"],"jobs.resubmit":["admin","operator","viewer"],"jobs.start":["admin","operator"],"jobs.submit":["admin","operator","viewer"],"maintenance.log":["admin","operator"],"maintenance.tasks":["admin","operator"],"models.create":["admin","operator"],"models.delete":["admin"],"models.edit":["admin","operator"],"models.plates":["admin","operator"],"orders.create":["admin","operator"],"orders.delete":["admin","operator"],"orders.edit":["admin"],"orders.ship":["admin","operator"],"printers.add":["admin"],"printers.delete":["admin"],"printers.dispatch_recovery":["admin"],"printers.edit":["admin","operator"],"printers.plug":["admin","operator"],"printers.reorder":["admin","operator"],"printers.slots":["admin","operator"],"products.assortment":["admin"],"products.assortment_preview":["admin","operator"],"products.create":["admin","operator"],"products.delete":["admin"],"products.edit":["admin","operator"],"products.finishing":["admin","operator"],"products.inspections":["admin","operator"],"products.materials_manage":["admin"],"products.materials_preview":["admin","operator"],"products.materials_receive":["admin","operator"],"products.operations":["admin","operator"],"products.recipes":["admin"],"products.square_manage":["admin"],"products.square_send":["admin","operator"],"settings.edit":["admin"],"spools.delete":["admin","operator"],"spools.edit":["admin","operator"],"timeline.move":["admin","operator"],"upload.delete":["admin","operator"],"upload.schedule":["admin","operator"],"upload.upload":["admin","operator"],"users.restore_superadmin":["admin"]},"page_access":{"admin":["admin"],"alerts":["admin","operator","viewer"],"analytics":["admin","operator","viewer"],"audit":["admin"],"branding":["admin"],"calculator":["admin","operator","viewer"],"cameras":["admin","operator","viewer"],"dashboard":["admin","operator","viewer"],"education":["admin","operator","viewer"],"education_reports":["admin","operator"],"jobs":["admin","operator","viewer"],"maintenance":["admin","operator"],"models":["admin","operator","viewer"],"orders":["admin","operator","viewer"],"printers":["admin","operator","viewer"],"products":["admin","operator","viewer"],"settings":["admin"],"spools":["admin","operator","viewer"],"timeline":["admin","operator","viewer"],"upload":["admin","operator"]}}},"viewer":{"me":{"email":"ui_viewer@example.test","group_id":null,"id":3,"role":"viewer","username":"ui_viewer"},"permissions":{"action_access":{"alerts.read":["admin","operator","viewer"],"education.reorder":["admin","operator","viewer"],"jobs.approve":["admin","operator"],"jobs.cancel":["admin","operator"],"jobs.complete":["admin","operator"],"jobs.create":["admin","operator"],"jobs.delete":["admin","operator"],"jobs.edit":["admin","operator"],"jobs.reject":["admin","operator"],"jobs.resubmit":["admin","operator","viewer"],"jobs.start":["admin","operator"],"jobs.submit":["admin","operator","viewer"],"maintenance.log":["admin","operator"],"maintenance.tasks":["admin","operator"],"models.create":["admin","operator"],"models.delete":["admin"],"models.edit":["admin","operator"],"models.plates":["admin","operator"],"orders.create":["admin","operator"],"orders.delete":["admin","operator"],"orders.edit":["admin"],"orders.ship":["admin","operator"],"printers.add":["admin"],"printers.delete":["admin"],"printers.dispatch_recovery":["admin"],"printers.edit":["admin","operator"],"printers.plug":["admin","operator"],"printers.reorder":["admin","operator"],"printers.slots":["admin","operator"],"products.assortment":["admin"],"products.assortment_preview":["admin","operator"],"products.create":["admin","operator"],"products.delete":["admin"],"products.edit":["admin","operator"],"products.finishing":["admin","operator"],"products.inspections":["admin","operator"],"products.materials_manage":["admin"],"products.materials_preview":["admin","operator"],"products.materials_receive":["admin","operator"],"products.operations":["admin","operator"],"products.recipes":["admin"],"products.square_manage":["admin"],"products.square_send":["admin","operator"],"settings.edit":["admin"],"spools.delete":["admin","operator"],"spools.edit":["admin","operator"],"timeline.move":["admin","operator"],"upload.delete":["admin","operator"],"upload.schedule":["admin","operator"],"upload.upload":["admin","operator"],"users.restore_superadmin":["admin"]},"page_access":{"admin":["admin"],"alerts":["admin","operator","viewer"],"analytics":["admin","operator","viewer"],"audit":["admin"],"branding":["admin"],"calculator":["admin","operator","viewer"],"cameras":["admin","operator","viewer"],"dashboard":["admin","operator","viewer"],"education":["admin","operator","viewer"],"education_reports":["admin","operator"],"jobs":["admin","operator","viewer"],"maintenance":["admin","operator"],"models":["admin","operator","viewer"],"orders":["admin","operator","viewer"],"printers":["admin","operator","viewer"],"products":["admin","operator","viewer"],"settings":["admin"],"spools":["admin","operator","viewer"],"timeline":["admin","operator","viewer"],"upload":["admin","operator"]}}}}}/*WIRE-END*/

vi.mock('../../api', () => ({ setup: { status: vi.fn().mockResolvedValue({ needs_setup: false }) }, stats: { get: vi.fn().mockResolvedValue({}) }, printers: { list: vi.fn().mockResolvedValue([]) }, pricingConfig: { get: vi.fn().mockResolvedValue({ ui_mode: 'advanced' }) }, getEducationMode: vi.fn().mockResolvedValue({ enabled: false }), fetchAPI: vi.fn().mockResolvedValue({}), config: { getNetwork: vi.fn().mockResolvedValue({}) }, backups: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('../../BrandingContext', () => ({ useBranding: () => ({ app_name: 'Synthetic farm' }) }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ tier: 'enterprise', isPro: true, hasFeature: () => true, isProPage: () => false }) }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: null }) }))
vi.mock('../../hooks/useWebSocket', () => ({ default: () => null }))
vi.mock('../../hooks/useKeyboardShortcuts', () => ({ default: () => ({ showHelp: false, setShowHelp: vi.fn() }) }))
vi.mock('../shared/OnboardingTour', () => ({ default: () => null }))
vi.mock('../notifications/AlertBell', () => ({ default: () => null }))
vi.mock('../printers/EmergencyStop', () => ({ default: () => null }))
vi.mock('../shared/UpgradeBanner', () => ({ default: () => null }))
vi.mock('../shared/GlobalSearch', () => ({ default: () => null }))
vi.mock('../layout/MobileHeader', () => ({ default: () => null }))
vi.mock('../ui/MobileBottomNav', () => ({ default: () => null }))
vi.mock('../layout/ThemeToggle', () => ({ default: () => null }))
vi.mock('../printers/CameraPipProvider', () => ({ default: ({ children }: any) => <>{children}</> }))
vi.mock('../shared/KeyboardShortcutsModal', () => ({ default: () => null }))
vi.mock('../../pages/dashboard/Dashboard', () => ({ default: () => <h1>Dashboard page</h1> }))
vi.mock('../../pages/admin/Settings', () => ({ default: () => <h1>Settings page</h1> }))
vi.mock('../../pages/admin/AuditLogs', () => ({ default: () => <h1>Audit page</h1> }))
vi.mock('../../pages/auth/Login', () => ({ default: () => <h1>Login page</h1> }))
vi.mock('../../pages/jobs/Timeline', () => ({ default: () => <h1>Timeline page</h1> }))

type Role = 'admin' | 'operator' | 'viewer'

function serve(role: Role | null, timelineRoles?: string[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
    if (role === null) return json(401, { detail: 'Not authenticated' })
    if (url === '/api/auth/me') return json(200, (WIRE.roles as any)[role].me)
    if (url === '/api/permissions') {
      const p = (WIRE.roles as any)[role].permissions
      return json(200, timelineRoles ? { ...p, page_access: { ...p.page_access, timeline: timelineRoles } } : p)
    }
    return json(404, { detail: 'Not Found' })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function visit(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>)
}

beforeEach(async () => {
  await clearSensitiveBrowserState()
  vi.stubGlobal('__APP_VERSION__', 'synthetic')
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals() })

for (const role of ['admin', 'operator', 'viewer'] as const) {
  it(`${role} with the default timeline page access mounts the Timeline`, async () => {
    serve(role)
    visit('/timeline')
    expect(await screen.findByRole('heading', { name: 'Timeline page' })).toBeInTheDocument()
  })
}

it('a role removed from the timeline page permission is redirected to the dashboard without mounting the page', async () => {
  serve('viewer', ['admin', 'operator'])
  visit('/timeline')
  expect(await screen.findByRole('heading', { name: 'Dashboard page' })).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Timeline page' })).not.toBeInTheDocument()
})

it('unauthenticated /timeline goes to login', async () => {
  serve(null)
  visit('/timeline')
  expect(await screen.findByRole('heading', { name: 'Login page' })).toBeInTheDocument()
})
