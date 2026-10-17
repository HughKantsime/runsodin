// Cert F / cluster B — F11-11.4-01/02 Group management UI. Real GroupManager + real LicenseProvider + real
// api/organizations.ts adapter + react-query; only global fetch is stubbed with GET /api/groups, /api/users and
// /api/license bodies captured from the real backend (tests/test_contracts/test_cert_F_B_groups_orgs.py, LiveOdin).
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import GroupManager from './GroupManager'
import { LicenseProvider } from '../../LicenseContext'

// WIRE-BEGIN
const WIRE: any = {
 "groups_list": [
  {
   "id": 1,
   "name": "Period 3",
   "description": "Engineering",
   "owner_id": 2,
   "created_at": "2026-10-15 00:58:18",
   "updated_at": "2026-10-15 00:58:18",
   "owner_username": "grp_owner",
   "member_count": 1
  }
 ],
 "users_list": [
  {
   "id": 1,
   "username": "certadmin",
   "email": "certadmin@example.test",
   "role": "admin",
   "is_active": 1,
   "last_login": "2026-10-15 00:58:16.120519+00:00",
   "created_at": "2026-10-15 00:58:15",
   "group_id": null,
   "mfa_enabled": false
  },
  {
   "id": 2,
   "username": "grp_owner",
   "email": "grp_owner@example.test",
   "role": "operator",
   "is_active": 1,
   "last_login": "2026-10-15 00:58:16.697038+00:00",
   "created_at": "2026-10-15 00:58:16",
   "group_id": null,
   "mfa_enabled": false
  },
  {
   "id": 3,
   "username": "grp_other_operator",
   "email": "grp_other_operator@example.test",
   "role": "operator",
   "is_active": 1,
   "last_login": "2026-10-15 00:58:17.412910+00:00",
   "created_at": "2026-10-15 00:58:17",
   "group_id": null,
   "mfa_enabled": false
  },
  {
   "id": 4,
   "username": "grp_student",
   "email": "grp_student@example.test",
   "role": "viewer",
   "is_active": 1,
   "last_login": "2026-10-15 00:58:18.167990+00:00",
   "created_at": "2026-10-15 00:58:17",
   "group_id": 1,
   "mfa_enabled": false
  },
  {
   "id": 5,
   "username": "grp_second_admin",
   "email": "grp_second_admin@example.test",
   "role": "admin",
   "is_active": 1,
   "last_login": null,
   "created_at": "2026-10-15 00:58:18",
   "group_id": null,
   "mfa_enabled": false
  }
 ],
 "license": {
  "valid": true,
  "tier": "enterprise",
  "tier_name": "Enterprise",
  "issued_at": "",
  "expires_at": "2099-01-01",
  "expired": false,
  "trial": false,
  "trial_days_left": null,
  "max_printers": 9999,
  "max_users": 9999,
  "features": [
   "analytics",
   "audit_export",
   "bom",
   "branding",
   "care_counters",
   "cost_calculator",
   "csv_export",
   "email_notifications",
   "job_approval",
   "maintenance",
   "mqtt_republish",
   "multi_user",
   "opcua",
   "orders",
   "permissions",
   "print_quotas",
   "products",
   "push_notifications",
   "rbac",
   "sqlcipher",
   "sso",
   "usage_reports",
   "user_groups",
   "webhooks",
   "white_label"
  ],
  "managed_externally": false,
  "binding_present": false,
  "binding_matches_current": false,
  "license_sha256": "b6afa8b2829680d87de0cd998e54611b61c9c1c33c490497c67f0e937fc86eb4"
 }
}
// WIRE-END

const state = vi.hoisted(() => ({ fetch: vi.fn(), license: null as any }))
function respond(data: any, status = 200) { return { ok: status < 400, status, json: async () => data, text: async () => JSON.stringify(data), headers: new Headers() } }
function calls(path: string, method: string) { return state.fetch.mock.calls.filter(([u, o]) => String(u) === path && (o?.method || 'GET') === method) }
const clients: QueryClient[] = []
function show() {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c)
  return render(<QueryClientProvider client={c}><LicenseProvider><GroupManager /></LicenseProvider></QueryClientProvider>)
}
beforeEach(() => {
  state.license = WIRE.license
  state.fetch.mockReset()
  state.fetch.mockImplementation(async (url: unknown, options: any = {}) => {
    const path = String(url); const method = options.method || 'GET'
    if (path === '/api/license') return respond(state.license)
    if (path === '/api/groups' && method === 'GET') return respond(WIRE.groups_list)
    if (path === '/api/users' && method === 'GET') return respond(WIRE.users_list)
    if (path === '/api/groups' && method === 'POST') return respond({ status: 'created', id: 9 })
    if (/^\/api\/groups\/\d+$/.test(path) && method === 'PATCH') return respond({ status: 'updated' })
    if (/^\/api\/groups\/\d+$/.test(path) && method === 'DELETE') return respond({ status: 'deleted' })
    return respond({ detail: 'unexpected' }, 500)
  })
  vi.stubGlobal('fetch', state.fetch)
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.unstubAllGlobals(); vi.restoreAllMocks() })
const group = () => WIRE.groups_list[0]

it('F11-11.4-01 renders the wire group with owner and member count', async () => {
  show()
  const row = (await screen.findByText(group().name)).closest('tr')!
  expect(within(row).getByText(group().description)).toBeInTheDocument()
  expect(within(row).getByText(group().owner_username)).toBeInTheDocument()
  expect(within(row).getByText(String(group().member_count))).toBeInTheDocument()
})

it('F11-11.4-02 owner (approver) choices are only operators and admins; create sends owner_id', async () => {
  show(); fireEvent.click(await screen.findByRole('button', { name: /New Group/ }))
  const owner = await screen.findByLabelText('Owner (approver)') as HTMLSelectElement
  await waitFor(() => expect(owner.options.length).toBeGreaterThan(1))
  const expected = WIRE.users_list.filter((u: any) => u.role === 'operator' || u.role === 'admin').map((u: any) => `${u.username} (${u.role})`)
  expect([...owner.options].slice(1).map(o => o.textContent)).toEqual(expected)
  expect([...owner.options].some(o => /\(viewer\)/.test(o.textContent || ''))).toBe(false)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Period 4' } })
  fireEvent.change(screen.getByLabelText('Description (optional)'), { target: { value: 'Robotics' } })
  const ownerUser = WIRE.users_list.find((u: any) => u.username === group().owner_username)
  fireEvent.change(owner, { target: { value: String(ownerUser.id) } })
  fireEvent.click(screen.getByRole('button', { name: 'Create Group' }))
  await waitFor(() => expect(calls('/api/groups', 'POST')).toHaveLength(1))
  expect(JSON.parse(calls('/api/groups', 'POST')[0][1].body)).toEqual({ name: 'Period 4', description: 'Robotics', owner_id: ownerUser.id })
})

it('F11-11.4-01 edit and delete go through PATCH/DELETE /api/groups/{id}', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  show()
  const row = (await screen.findByText(group().name)).closest('tr')!
  fireEvent.click(within(row).getByTitle('Edit'))
  const name = await screen.findByLabelText('Name') as HTMLInputElement
  expect(name.value).toBe(group().name)
  fireEvent.change(name, { target: { value: 'Period 3 renamed' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
  await waitFor(() => expect(calls(`/api/groups/${group().id}`, 'PATCH')).toHaveLength(1))
  expect(JSON.parse(calls(`/api/groups/${group().id}`, 'PATCH')[0][1].body)).toEqual(expect.objectContaining({ name: 'Period 3 renamed', owner_id: group().owner_id }))
  fireEvent.click(within((await screen.findByText(group().name)).closest('tr')!).getByTitle('Delete'))
  await waitFor(() => expect(calls(`/api/groups/${group().id}`, 'DELETE')).toHaveLength(1))
})

it('F11-11.4-01 group management is hidden when the license lacks user_groups', async () => {
  state.license = { ...WIRE.license, tier: 'pro', features: WIRE.license.features.filter((f: string) => f !== 'user_groups' && f !== 'job_approval') }
  const view = show()
  await waitFor(() => expect(calls('/api/license', 'GET').length).toBeGreaterThan(0))
  await new Promise(r => setTimeout(r, 20))
  expect(view.container).toBeEmptyDOMElement()
  expect(calls('/api/groups', 'GET')).toHaveLength(0)
})
