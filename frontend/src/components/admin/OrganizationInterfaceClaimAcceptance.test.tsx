import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import OrgManager from './OrgManager'
import Settings from '../../pages/admin/Settings'
const boundary = vi.hoisted(() => ({ role: 'admin', failMember: false, fetch: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: () => boundary.role === 'admin', getCurrentUser: () => ({ id: 99, role: boundary.role, group_id: null }), clearSensitiveBrowserState: vi.fn() }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, hasFeature: (name: string) => ['user_groups', 'sso', 'permissions'].includes(name), atUserLimit: () => false, maxUsers: 0 }) }))
vi.mock('../../BrandingContext', () => ({ useBrandingUpdate: () => vi.fn(), applyBrandingFavicon: vi.fn() }))
const org = { id: 17, name: 'Fixture organization', member_count: 1, is_org: true }
const users = [{ id: 3, username: 'Existing member', email: 'existing@example.test', role: 'viewer', group_id: 17, is_active: true }, { id: 4, username: 'Unassigned user', email: 'unassigned@example.test', role: 'operator', group_id: null, is_active: true }]
const printers = [{ id: 7, name: 'Fixture printer', org_id: null }]
const clients: QueryClient[] = []
function show(node: React.ReactNode) { const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c); return render(<QueryClientProvider client={c}><MemoryRouter initialEntries={['/settings']}>{node}</MemoryRouter></QueryClientProvider>) }
function calls(path: string, method: string) { return boundary.fetch.mock.calls.filter(([url, options]) => String(url) === path && (options?.method || 'GET') === method) }
async function manager() { show(<OrgManager />); await screen.findByText(org.name); await screen.findByRole('option', { name: 'Unassigned user' }); await screen.findByRole('option', { name: 'Fixture printer' }) }
function selectFor(option: string) { return screen.getByRole('option', { name: option, exact: true }).closest('select')! }
async function settingsAccess() { show(<Settings />); fireEvent.click(screen.getByRole('button', { name: 'Access', exact: true })); await screen.findByText('Existing member', { selector: 'p' }) }
function response(data: any, ok = true) { return { ok, status: ok ? 200 : 403, json: async () => data, text: async () => JSON.stringify(data) } }
beforeEach(() => {
  vi.clearAllMocks(); boundary.role = 'admin'; boundary.failMember = false
  boundary.fetch.mockImplementation(async (url: unknown, options: any = {}) => {
    const path = String(url); const method = options.method || 'GET'
    if (path === '/api/orgs/17/members' && method === 'POST' && boundary.failMember) return response({ detail: 'Fixture membership denied' },false)
    if (method !== 'GET') return response({ status: 'ok' })
    if (path === '/api/users') return response(users)
    if (path === '/api/printers') return response(printers)
    if (path === '/api/orgs' || path === '/api/groups') return response([org])
    if (path === '/api/orgs/17/settings') return response({ default_filament_type: 'PLA', quiet_hours_enabled: false })
    if (path.includes('pricing')) return response({ ui_mode: 'advanced' })
    if (path.includes('webhooks') || path.includes('backups') || path.includes('report-schedules') || path.includes('chargeback')) return response([])
    if (path.includes('oidc')) return response({ configured: true, provider_type: 'generic', is_enabled: false, has_client_secret: false })
    if (path === '/api/license') return response({ tier: 'pro', valid: true, features: [] })
    if (path === '/api/branding') return response({ app_name: 'Fixture ODIN', app_subtitle: 'Fixture', theme_colors: {}, logo_url: null, favicon_url: null })
    if (path === '/api/stats') return response({ jobs: {}, printers: {} })
    if (path === '/api/config') return response({ spoolman_url: 'http://fixture.invalid:7912', blackout_start: '22:30', blackout_end: '05:30' })
    return response({})
  })
  vi.stubGlobal('fetch',boundary.fetch)
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.unstubAllGlobals(); vi.restoreAllMocks() })
it('F11-11.5-02 add member sends exact org/user identifiers through actual API adapter and refreshes organizations', async () => {
  await manager(); const reads = calls('/api/orgs','GET').length; fireEvent.change(selectFor('Add member...'), { target: { value: '4' } }); fireEvent.click(screen.getByRole('button', { name: 'Add', exact: true }))
  await waitFor(() => expect(calls('/api/orgs/17/members','POST')).toHaveLength(1)); const options = calls('/api/orgs/17/members','POST')[0][1]
  expect(JSON.parse(options.body)).toEqual({ user_id: 4 }); expect(options.credentials).toBe('include'); await waitFor(() => expect(calls('/api/orgs','GET').length).toBeGreaterThan(reads))
})
it('F11-11.5-02 assign printer sends exact org/printer identifiers through actual API adapter', async () => {
  await manager(); fireEvent.change(selectFor('Assign printer...'), { target: { value: '7' } }); fireEvent.click(screen.getByRole('button', { name: 'Assign', exact: true }))
  await waitFor(() => expect(calls('/api/orgs/17/printers','POST')).toHaveLength(1)); expect(JSON.parse(calls('/api/orgs/17/printers','POST')[0][1].body)).toEqual({ printer_id: 7 })
})
it('F11-11.5-02 failed member mutation reports boundary refusal without pretending membership changed', async () => {
  const toast = (await import('react-hot-toast')).default; const error = vi.spyOn(toast,'error'); boundary.failMember = true; await manager(); const reads = calls('/api/orgs','GET').length
  fireEvent.change(selectFor('Add member...'), { target: { value: '4' } }); fireEvent.click(screen.getByRole('button', { name: 'Add', exact: true }))
  await waitFor(() => expect(error).toHaveBeenCalledWith('Add member failed: Fixture membership denied')); expect(calls('/api/orgs','GET')).toHaveLength(reads)
})
it.each(['operator','viewer'])('F11-11.5-03 %s without settings.edit grant has no organization management controls', async role => {
  boundary.role = role; const view = show(<OrgManager />); expect(view.container).toBeEmptyDOMElement(); expect(screen.queryByRole('button', { name: 'New Org' })).not.toBeInTheDocument()
})
it('F11-11.5-03 actual Settings Access accordion opens the organization manager', async () => {
  await settingsAccess(); expect(screen.queryByRole('heading', { name: 'Organizations' })).not.toBeInTheDocument(); fireEvent.click(screen.getByRole('button', { name: 'Organizations', exact: true }))
  expect(await screen.findByRole('heading', { name: 'Organizations' })).toBeInTheDocument(); expect(await screen.findByText(org.name, { selector: 'span' })).toBeInTheDocument(); expect(screen.getByRole('button', { name: 'New Org' })).toBeInTheDocument()
})
it('F11-11.5-02/F11-11.3-05 admin removes organization membership and changes supported role through Users & Groups', async () => {
  await settingsAccess(); const row = screen.getByText('Existing member', { selector: 'p' }).closest('tr')!; fireEvent.click(within(row).getByTitle('Edit')); await screen.findByRole('heading', { name: 'Edit User' })
  const group = screen.getByText('Group', { selector: 'label', exact: true }).parentElement!.querySelector('select')!; expect(group).toHaveValue('17'); fireEvent.change(group, { target: { value: '' } })
  const role = screen.getByText('Role', { selector: 'label', exact: true }).parentElement!.querySelector('select')!; expect([...role.options].map(o => o.value)).toEqual(['viewer','operator','admin']); fireEvent.change(role, { target: { value: 'operator' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes', exact: true })); await waitFor(() => expect(calls('/api/users/3','PATCH')).toHaveLength(1))
  expect(JSON.parse(calls('/api/users/3','PATCH')[0][1].body)).toEqual(expect.objectContaining({ group_id: null, role: 'operator' }))
})
it('F11-11.3-05 admin general settings saves exact scheduler configuration through actual API adapter', async () => {
  show(<Settings />); const input = await screen.findByRole('textbox', { name: 'Spoolman URL' }); await waitFor(() => expect(input).toHaveValue('http://fixture.invalid:7912')); fireEvent.change(input, { target: { value: 'http://localhost:7912' } }); fireEvent.click(screen.getByRole('button', { name: 'Save Scheduling Settings' }))
  await waitFor(() => expect(calls('/api/config','PUT')).toHaveLength(1)); expect(JSON.parse(calls('/api/config','PUT')[0][1].body)).toEqual({ spoolman_url: 'http://localhost:7912', blackout_start: '22:30', blackout_end: '05:30' })
})
it('F11-11.3-05 admin opens SSO configuration controls in actual Access surface', async () => {
  await settingsAccess(); fireEvent.click(screen.getByRole('button', { name: 'Authentication (OIDC & MFA)', exact: true })); expect(await screen.findByRole('heading', { name: 'Single sign-on', exact: true })).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: /Enable single sign-on/ })).toBeInTheDocument(); expect(screen.getByRole('button', { name: 'Save configuration' })).toBeInTheDocument()
})
it('F11-11.3-05 admin navigates real webhook, branding, license and backup control panels', async () => {
  show(<Settings />); fireEvent.click(screen.getByRole('button', { name: 'Integrations', exact: true })); expect(await screen.findByRole('heading', { name: 'Webhooks' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Branding', exact: true })); expect(await screen.findByRole('heading', { name: 'White-Label Branding' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'License', exact: true })); expect(await screen.findByRole('heading', { name: 'Current License' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'System', exact: true })); expect(await screen.findByRole('heading', { name: 'Database Backups' })).toBeInTheDocument(); expect(screen.getByRole('button', { name: 'Create Backup' })).toBeInTheDocument()
})
