import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Settings from './Settings'
import { MemoryRouter } from 'react-router-dom'
const api = vi.hoisted(() => ({ get: vi.fn(), update: vi.fn(), mode: 'advanced', pro: true }))
vi.mock('../../api', () => ({
 pricingConfig: { get: api.get, update: api.update },
 config: { get: vi.fn().mockResolvedValue({}), getNetwork: vi.fn().mockResolvedValue({}), update: vi.fn(), testSpoolman: vi.fn() },
 users: { list: vi.fn().mockResolvedValue([]) }, fetchAPI: vi.fn().mockResolvedValue({}),
 getEducationMode: vi.fn().mockResolvedValue({enabled:false}), setEducationMode: vi.fn(),
 getApprovalSetting: vi.fn().mockResolvedValue({}), setApprovalSetting: vi.fn(),
}))
// License fixture: a Pro license carries the Pro tier features; Community carries none.
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: api.pro, hasFeature: (f: string) => api.pro && ['permissions', 'sso', 'branding', 'webhooks'].includes(f) }) }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 1 }) }))
vi.mock('./Admin', () => ({ default: () => <div>Users boundary</div> }))
vi.mock('./Permissions', () => ({ default: () => <div>Permissions boundary</div> }))
vi.mock('./Branding', () => ({ default: () => <div>Branding boundary</div> }))
vi.mock('../../components/admin/OIDCSettings', () => ({ default: () => <div>OIDC boundary</div> }))
vi.mock('../../components/admin/WebhookSettings', () => ({ default: () => <div>Integrations boundary</div> }))
vi.mock('../../components/admin/GroupManager', () => ({ default: () => <div>Groups boundary</div> }))
vi.mock('../../components/auth/MFASetup', () => ({ default: () => <div>MFA boundary</div> }))
vi.mock('../../components/auth/APITokenManager', () => ({ default: () => <div>Tokens boundary</div> }))
vi.mock('../../components/auth/SessionManager', () => ({ default: () => <div>Sessions boundary</div> }))
vi.mock('../../components/admin/QuotaManager', () => ({ default: () => <div>Quotas boundary</div> }))
vi.mock('../../components/admin/IPAllowlistSettings', () => ({ default: () => <div>Allowlist boundary</div> }))
vi.mock('../../components/admin/OrgManager', () => ({ default: () => <div>Organizations boundary</div> }))
vi.mock('../../components/admin/LogViewer', () => ({ default: () => <div>Logs boundary</div> }))
vi.mock('../../components/admin/NotificationsTab', () => ({ default: () => <div>Notifications boundary</div> }))
vi.mock('../../components/admin/VisionSettingsTab', () => ({ default: () => <div>Vigil boundary</div> }))
vi.mock('../../components/admin/SystemTab', () => ({ default: () => <div>System boundary</div> }))
vi.mock('../../components/admin/LicenseTab', () => ({ default: () => <div>License boundary</div> }))
beforeEach(() => {
 api.mode = 'advanced'; api.pro = true
 api.get.mockImplementation(async () => ({ui_mode: api.mode, syntheticPreserved: 17}))
 api.update.mockImplementation(async data => { api.mode = data.ui_mode; return data })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })
function mount(path: string) {
 const client = new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})
 return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><Settings /></MemoryRouter></QueryClientProvider>)
}

// The ?tab= deep link (used by the system-notice banner) must respect the same gating as the tab bar.
test.each([['integrations', 'Integrations boundary'], ['branding', 'Branding boundary']])(
 'Community deep link ?tab=%s opens General, not the Pro tab', async (tab, content) => {
 api.pro = false; mount(`/settings?tab=${tab}`)
 expect(await screen.findByRole('heading', { name: 'Interface Mode' })).toBeInTheDocument()
 expect(screen.queryByText(content)).not.toBeInTheDocument()
})
test.each([['integrations', 'Integrations boundary'], ['branding', 'Branding boundary'], ['system', 'System boundary']])(
 'Pro deep link ?tab=%s opens that tab', async (tab, content) => {
 mount(`/settings?tab=${tab}`)
 expect(await screen.findByText(content)).toBeInTheDocument()
})
test('Community deep link to a free tab still opens it', async () => {
 api.pro = false; mount('/settings?tab=system')
 expect(await screen.findByText('System boundary')).toBeInTheDocument()
})
test('Simple view deep link ?tab=vision opens General, since the Vigil AI tab is hidden', async () => {
 api.mode = 'simple'; mount('/settings?tab=vision')
 await screen.findByRole('heading', { name: 'Interface Mode' })
 expect(screen.queryByRole('button', { name: 'Vigil AI', exact: true })).not.toBeInTheDocument()
 expect(screen.queryByText('Vigil boundary')).not.toBeInTheDocument()
})
