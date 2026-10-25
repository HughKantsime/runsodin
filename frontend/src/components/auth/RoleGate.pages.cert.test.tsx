/**
 * Every page-access key of the permission matrix guards its frontend route with RoleGate.
 *
 * Real App route table, ProtectedRoute, permissions module and RoleGate. The only boundary is window.fetch, which
 * returns the producer wire captured from a real ODIN server (tests/test_contracts/test_cert_F_C_ui_wire.py checks
 * the embedded JSON against that capture). A narrowed page key is served as a saved matrix would be (GET
 * /api/permissions returns the stored override). Page bodies are replaced by headings so the test observes which
 * route mounted.
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
vi.mock('../../pages/admin/AuditLogs', () => ({ default: () => <h1>AuditLogs page</h1> }))
vi.mock('../../pages/admin/Settings', () => ({ default: () => <h1>Settings page</h1> }))
vi.mock('../../pages/analytics/Analytics', () => ({ default: () => <h1>Analytics page</h1> }))
vi.mock('../../pages/analytics/EducationReports', () => ({ default: () => <h1>EducationReports page</h1> }))
vi.mock('../../pages/dashboard/Dashboard', () => ({ default: () => <h1>Dashboard page</h1> }))
vi.mock('../../pages/education/EducationWorkbench', () => ({ default: () => <h1>EducationWorkbench page</h1> }))
vi.mock('../../pages/inventory/Spools', () => ({ default: () => <h1>Spools page</h1> }))
vi.mock('../../pages/jobs/Jobs', () => ({ default: () => <h1>Jobs page</h1> }))
vi.mock('../../pages/jobs/Timeline', () => ({ default: () => <h1>Timeline page</h1> }))
vi.mock('../../pages/jobs/Upload', () => ({ default: () => <h1>Upload page</h1> }))
vi.mock('../../pages/models/Models', () => ({ default: () => <h1>Models page</h1> }))
vi.mock('../../pages/notifications/Alerts', () => ({ default: () => <h1>Alerts page</h1> }))
vi.mock('../../pages/orders/Calculator', () => ({ default: () => <h1>Calculator page</h1> }))
vi.mock('../../pages/orders/Orders', () => ({ default: () => <h1>Orders page</h1> }))
vi.mock('../../pages/orders/Products', () => ({ default: () => <h1>Products page</h1> }))
vi.mock('../../pages/printers/Cameras', () => ({ default: () => <h1>Cameras page</h1> }))
vi.mock('../../pages/printers/Maintenance', () => ({ default: () => <h1>Maintenance page</h1> }))
vi.mock('../../pages/printers/Printers', () => ({ default: () => <h1>Printers page</h1> }))

type Role = 'admin' | 'operator' | 'viewer'
// page key -> [route, mounted heading]
const PAGES: Record<string, [string, string]> = {
 "dashboard": [
  "/",
  "Dashboard page"
 ],
 "timeline": [
  "/timeline",
  "Timeline page"
 ],
 "jobs": [
  "/jobs",
  "Jobs page"
 ],
 "printers": [
  "/printers",
  "Printers page"
 ],
 "models": [
  "/models",
  "Models page"
 ],
 "spools": [
  "/spools",
  "Spools page"
 ],
 "cameras": [
  "/cameras",
  "Cameras page"
 ],
 "analytics": [
  "/analytics",
  "Analytics page"
 ],
 "calculator": [
  "/calculator",
  "Calculator page"
 ],
 "upload": [
  "/upload",
  "Upload page"
 ],
 "maintenance": [
  "/maintenance",
  "Maintenance page"
 ],
 "settings": [
  "/settings",
  "Settings page"
 ],
 "admin": [
  "/admin",
  "Settings page"
 ],
 "audit": [
  "/audit",
  "AuditLogs page"
 ],
 "branding": [
  "/branding",
  "Settings page"
 ],
 "education_reports": [
  "/education-reports",
  "EducationReports page"
 ],
 "education": [
  "/education",
  "EducationWorkbench page"
 ],
 "orders": [
  "/orders",
  "Orders page"
 ],
 "products": [
  "/products",
  "Products page"
 ],
 "alerts": [
  "/alerts",
  "Alerts page"
 ]
}

function serve(role: Role, pageOverride?: Record<string, string[]>) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
    if (url === '/api/auth/me') return json(200, (WIRE.roles as any)[role].me)
    if (url === '/api/permissions') {
      const p = (WIRE.roles as any)[role].permissions
      return json(200, pageOverride ? { ...p, page_access: { ...p.page_access, ...pageOverride } } : p)
    }
    return json(404, { detail: 'Not Found' })
  }))
}

function visit(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>)
}

async function expectRefused(page: string) {
  if (page === 'dashboard') {
    expect(await screen.findByRole('heading', { name: 'No dashboard access' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: PAGES.dashboard[1] })).not.toBeInTheDocument()
  } else {
    expect(await screen.findByRole('heading', { name: PAGES.dashboard[1] })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: PAGES[page][1] })).not.toBeInTheDocument()
  }
}

beforeEach(async () => {
  await clearSensitiveBrowserState()
  vi.stubGlobal('__APP_VERSION__', 'synthetic')
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals() })

const ROLES: Role[] = ['admin', 'operator', 'viewer']
const REDIRECTS_TO_SETTINGS = ['admin', 'branding']

it('covers every page key the server matrix defines', () => {
  expect(Object.keys(PAGES).sort()).toEqual(Object.keys((WIRE.roles as any).admin.permissions.page_access).sort())
})

for (const page of Object.keys(PAGES)) {
  for (const role of ROLES) {
    it(`${page}: default matrix, ${role}`, async () => {
      serve(role)
      const allowed = (WIRE.roles as any)[role].permissions.page_access[page].includes(role)
      visit(PAGES[page][0])
      if (allowed) expect(await screen.findByRole('heading', { name: PAGES[page][1] })).toBeInTheDocument()
      else await expectRefused(page)
    })
  }
  it(`${page}: a role removed from the key is refused`, async () => {
    const roles: string[] = (WIRE.roles as any).admin.permissions.page_access[page]
    const victim = (['viewer', 'operator'] as Role[]).find(r => roles.includes(r))
    if (!victim) return  // admin-only page; admin cannot be removed from admin/settings/audit and is the only holder
    serve(victim, { [page]: roles.filter(r => r !== victim) })
    visit(PAGES[page][0])
    await expectRefused(page)
  })
  it(`${page}: a role added to the key is admitted`, async () => {
    const roles: string[] = (WIRE.roles as any).admin.permissions.page_access[page]
    const guest = (['viewer', 'operator'] as Role[]).find(r => !roles.includes(r))
    if (!guest) return
    // /admin and /branding redirect to /settings, which its own key also guards
    const settings: string[] = (WIRE.roles as any).admin.permissions.page_access.settings
    serve(guest, { [page]: [...roles, guest], ...(REDIRECTS_TO_SETTINGS.includes(page) ? { settings: [...settings, guest] } : {}) })
    visit(PAGES[page][0])
    expect(await screen.findByRole('heading', { name: PAGES[page][1] })).toBeInTheDocument()
  })
}
