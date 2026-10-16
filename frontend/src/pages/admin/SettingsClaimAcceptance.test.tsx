import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
function mount() {
 const client = new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})
 return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/settings']}><Settings /></MemoryRouter></QueryClientProvider>)
}
test('F16-16.7-02 Simple and Advanced persist mode, preserve other config and update Vigil visibility', async () => {
 mount()
 await screen.findByRole('button',{name:'Vigil AI',exact:true})
 fireEvent.click(screen.getByRole('button',{name:/^Simple /}))
 await waitFor(() => expect(api.update).toHaveBeenCalledWith({ui_mode:'simple',syntheticPreserved:17}))
 await waitFor(() => expect(screen.queryByRole('button',{name:'Vigil AI',exact:true})).not.toBeInTheDocument())
 fireEvent.click(screen.getByRole('button',{name:/^Advanced /}))
 await waitFor(() => expect(api.update).toHaveBeenCalledWith({ui_mode:'advanced',syntheticPreserved:17}))
 await screen.findByRole('button',{name:'Vigil AI',exact:true})
})
test.each([['Notifications','Notifications boundary'],['Integrations','Integrations boundary'],['Branding','Branding boundary'],['Vigil AI','Vigil boundary'],['System','System boundary']])('F16-16.7-03 %s tab mounts its assigned section', async (label, content) => {
 mount()
 await screen.findByRole('button',{name:'Vigil AI',exact:true})
 fireEvent.click(screen.getByRole('button',{name:label,exact:true}))
 expect(screen.getByText(content)).toBeInTheDocument()
 expect(screen.queryByRole('heading',{name:'Interface Mode'})).not.toBeInTheDocument()
})
test('F16-16.7-03 Access accordion mounts default Users then toggles Permissions', async () => {
 mount()
 fireEvent.click(screen.getByRole('button',{name:'Access',exact:true}))
 expect(screen.getByText('Users boundary')).toBeInTheDocument()
 expect(screen.queryByText('Permissions boundary')).not.toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'Permissions',exact:true}))
 expect(screen.getByText('Permissions boundary')).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'Permissions',exact:true}))
 expect(screen.queryByText('Permissions boundary')).not.toBeInTheDocument()
})
test('F16-16.7-03 Community prevents opening Pro Integrations and Branding', async () => {
 api.pro = false; mount()
 for (const label of ['Integrations','Branding']) {
  fireEvent.click(screen.getByRole('button',{name:label,exact:true}))
  expect(screen.getByRole('heading',{name:'Interface Mode'})).toBeInTheDocument()
 }
})
test('F20-20.1-04 Community opens Access: free security sections mount, Pro sections are locked', async () => {
 api.pro = false; mount()
 fireEvent.click(screen.getByRole('button',{name:'Access'}))
 expect(screen.getByText('Users boundary')).toBeInTheDocument()
 for (const [section, boundary] of [['Authentication (OIDC & MFA)','MFA boundary'],['Tokens & Sessions','Tokens boundary'],['Quotas & Restrictions','Allowlist boundary'],['Organizations','Organizations boundary']] as const) {
  fireEvent.click(screen.getByRole('button',{name:section}))
  expect(screen.getByText(boundary)).toBeInTheDocument()
 }
 expect(screen.getByText('Sessions boundary')).toBeInTheDocument()
 expect(screen.queryByText('OIDC boundary')).not.toBeInTheDocument()
 expect(screen.getByText(/Single sign-on \(OIDC\) requires an O\.D\.I\.N\./)).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'Permissions'}))
 expect(screen.queryByText('Permissions boundary')).not.toBeInTheDocument()
 expect(screen.getByText(/Custom role permissions requires an O\.D\.I\.N\./)).toBeInTheDocument()
})
test('F20-20.1-04 Pro license mounts the Pro Access sections', async () => {
 mount()
 fireEvent.click(screen.getByRole('button',{name:'Access'}))
 fireEvent.click(screen.getByRole('button',{name:'Authentication (OIDC & MFA)'}))
 expect(screen.getByText('OIDC boundary')).toBeInTheDocument()
 expect(screen.getByText('MFA boundary')).toBeInTheDocument()
})
test('F16-16.7-01 Advanced exposes the nine documented tabs', async () => {
 mount()
 await screen.findByRole('button',{name:'Vigil AI',exact:true})
 // TabBar is the first direct flex group; isolate navigation from form controls.
 const general = screen.getByRole('button',{name:'General',exact:true})
 expect(general.parentElement?.querySelectorAll('button')).toHaveLength(9)
})
