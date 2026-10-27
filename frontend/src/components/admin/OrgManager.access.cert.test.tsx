// Cert F / cluster B — F11-11.5-02/03 Organization management in Settings -> Access. Real Settings page, real
// OrgManager/Admin user editor, real LicenseProvider and real permissions module (refreshPermissions with the
// captured /api/auth/me + /api/permissions), real api adapters; only global fetch is stubbed. Claim surfaces answer
// with bodies captured from the real backend (tests/test_contracts/test_cert_F_B_groups_orgs.py, LiveOdin);
// unrelated Settings panels get empty bodies.
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Settings from '../../pages/admin/Settings'
import { LicenseProvider } from '../../LicenseContext'
import { refreshPermissions } from '../../permissions'
vi.mock('../../BrandingContext', () => ({ useBrandingUpdate: () => vi.fn(), applyBrandingFavicon: vi.fn(), useBranding: () => ({}) }))

// WIRE-BEGIN
const WIRE: any = {
  "orgs_list": [
    {
      "id": 3,
      "name": "Org Alpha",
      "description": "Tenant A",
      "owner_id": 1,
      "member_count": 1,
      "created_at": "2026-10-15 00:58:19",
      "has_settings": false
    },
    {
      "id": 4,
      "name": "Org Beta",
      "description": "",
      "owner_id": 1,
      "member_count": 1,
      "created_at": "2026-10-15 00:58:19",
      "has_settings": false
    }
  ],
  "users_list_with_orgs": [
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
      "group_id": 1,
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
    },
    {
      "id": 6,
      "username": "org_alpha_op",
      "email": "org_alpha_op@example.test",
      "role": "operator",
      "is_active": 1,
      "last_login": "2026-10-15 00:58:19.876672+00:00",
      "created_at": "2026-10-15 00:58:19",
      "group_id": 3,
      "mfa_enabled": false
    },
    {
      "id": 7,
      "username": "org_beta_op",
      "email": "org_beta_op@example.test",
      "role": "operator",
      "is_active": 1,
      "last_login": "2026-10-15 00:58:20.338625+00:00",
      "created_at": "2026-10-15 00:58:20",
      "group_id": 4,
      "mfa_enabled": false
    },
    {
      "id": 8,
      "username": "org_temp_member",
      "email": "org_temp_member@example.test",
      "role": "viewer",
      "is_active": 1,
      "last_login": null,
      "created_at": "2026-10-15 00:58:20",
      "group_id": null,
      "mfa_enabled": false
    }
  ],
  "printers_list": [
    {
      "name": "Alpha printer",
      "model": null,
      "slot_count": 4,
      "is_active": true,
      "api_type": "moonraker",
      "api_host": "192.0.2.41",
      "camera_url": null,
      "camera_enabled": true,
      "camera_source": null,
      "nickname": null,
      "bed_temp": null,
      "bed_target_temp": null,
      "nozzle_temp": null,
      "nozzle_target_temp": null,
      "gcode_state": null,
      "print_stage": null,
      "hms_errors": null,
      "lights_on": null,
      "nozzle_type": null,
      "nozzle_diameter": null,
      "fan_speed": null,
      "bed_x_mm": null,
      "bed_y_mm": null,
      "last_seen": null,
      "total_print_hours": 0.0,
      "total_print_count": 0,
      "hours_since_maintenance": 0.0,
      "prints_since_maintenance": 0,
      "last_error_code": null,
      "last_error_message": null,
      "last_error_at": null,
      "camera_discovered": false,
      "tags": [],
      "timelapse_enabled": false,
      "machine_type": null,
      "id": 1,
      "created_at": "2026-10-15T00:58:20",
      "updated_at": "2026-10-15T00:58:20",
      "filament_slots": [
        {
          "slot_number": 1,
          "filament_type": "empty",
          "color": null,
          "color_hex": null,
          "spoolman_spool_id": null,
          "assigned_spool_id": null,
          "spool_confirmed": false,
          "id": 1,
          "printer_id": 1,
          "loaded_at": "2026-10-15T00:58:20",
          "remaining": null,
          "material_type": null
        },
        {
          "slot_number": 2,
          "filament_type": "empty",
          "color": null,
          "color_hex": null,
          "spoolman_spool_id": null,
          "assigned_spool_id": null,
          "spool_confirmed": false,
          "id": 2,
          "printer_id": 1,
          "loaded_at": "2026-10-15T00:58:20",
          "remaining": null,
          "material_type": null
        },
        {
          "slot_number": 3,
          "filament_type": "empty",
          "color": null,
          "color_hex": null,
          "spoolman_spool_id": null,
          "assigned_spool_id": null,
          "spool_confirmed": false,
          "id": 3,
          "printer_id": 1,
          "loaded_at": "2026-10-15T00:58:20",
          "remaining": null,
          "material_type": null
        },
        {
          "slot_number": 4,
          "filament_type": "empty",
          "color": null,
          "color_hex": null,
          "spoolman_spool_id": null,
          "assigned_spool_id": null,
          "spool_confirmed": false,
          "id": 4,
          "printer_id": 1,
          "loaded_at": "2026-10-15T00:58:20",
          "remaining": null,
          "material_type": null
        }
      ],
      "loaded_colors": [],
      "shared": false,
      "org_id": null,
      "has_api_key": false,
      "filament_source": null,
      "filament_source_status": null,
      "installed_nozzles": null,
      "maintenance": {
        "state": "ok",
        "note": "",
        "set_by": null,
        "set_by_name": null,
        "set_at": null
      }
    },
    {
      "name": "Shared floor printer",
      "model": null,
      "slot_count": 4,
      "is_active": true,
      "api_type": "moonraker",
      "api_host": "192.0.2.42",
      "camera_url": null,
      "camera_enabled": true,
      "camera_source": null,
      "nickname": null,
      "bed_temp": null,
      "bed_target_temp": null,
      "nozzle_temp": null,
      "nozzle_target_temp": null,
      "gcode_state": null,
      "print_stage": null,
      "hms_errors": null,
      "lights_on": null,
      "nozzle_type": null,
      "nozzle_diameter": null,
      "fan_speed": null,
      "bed_x_mm": null,
      "bed_y_mm": null,
      "last_seen": null,
      "total_print_hours": 0.0,
      "total_print_count": 0,
      "hours_since_maintenance": 0.0,
      "prints_since_maintenance": 0,
      "last_error_code": null,
      "last_error_message": null,
      "last_error_at": null,
      "camera_discovered": false,
      "tags": [],
      "timelapse_enabled": false,
      "machine_type": null,
      "id": 2,
      "created_at": "2026-10-15T00:58:20",
      "updated_at": "2026-10-15T00:58:20",
      "filament_slots": [
        {
          "slot_number": 1,
          "filament_type": "empty",
          "color": null,
          "color_hex": null,
          "spoolman_spool_id": null,
          "assigned_spool_id": null,
          "spool_confirmed": false,
          "id": 5,
          "printer_id": 2,
          "loaded_at": "2026-10-15T00:58:20",
          "remaining": null,
          "material_type": null
        },
        {
          "slot_number": 2,
          "filament_type": "empty",
          "color": null,
          "color_hex": null,
          "spoolman_spool_id": null,
          "assigned_spool_id": null,
          "spool_confirmed": false,
          "id": 6,
          "printer_id": 2,
          "loaded_at": "2026-10-15T00:58:20",
          "remaining": null,
          "material_type": null
        },
        {
          "slot_number": 3,
          "filament_type": "empty",
          "color": null,
          "color_hex": null,
          "spoolman_spool_id": null,
          "assigned_spool_id": null,
          "spool_confirmed": false,
          "id": 7,
          "printer_id": 2,
          "loaded_at": "2026-10-15T00:58:20",
          "remaining": null,
          "material_type": null
        },
        {
          "slot_number": 4,
          "filament_type": "empty",
          "color": null,
          "color_hex": null,
          "spoolman_spool_id": null,
          "assigned_spool_id": null,
          "spool_confirmed": false,
          "id": 8,
          "printer_id": 2,
          "loaded_at": "2026-10-15T00:58:20",
          "remaining": null,
          "material_type": null
        }
      ],
      "loaded_colors": [],
      "shared": false,
      "org_id": null,
      "has_api_key": false,
      "filament_source": null,
      "filament_source_status": null,
      "installed_nozzles": null,
      "maintenance": {
        "state": "ok",
        "note": "",
        "set_by": null,
        "set_by_name": null,
        "set_at": null
      }
    }
  ],
  "groups_list_with_orgs": [
    {
      "id": 3,
      "name": "Org Alpha",
      "description": "Tenant A",
      "owner_id": 1,
      "created_at": "2026-10-15 00:58:19",
      "updated_at": "2026-10-15 00:58:19",
      "owner_username": "certadmin",
      "member_count": 1
    },
    {
      "id": 4,
      "name": "Org Beta",
      "description": "",
      "owner_id": 1,
      "created_at": "2026-10-15 00:58:19",
      "updated_at": "2026-10-15 00:58:19",
      "owner_username": "certadmin",
      "member_count": 1
    },
    {
      "id": 1,
      "name": "Period 3B",
      "description": "Engineering",
      "owner_id": null,
      "created_at": "2026-10-15 00:58:18",
      "updated_at": "2026-10-15T00:58:19.322119+00:00",
      "owner_username": null,
      "member_count": 2
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
    "license_sha256": "b6afa8b2829680d87de0cd998e54611b61c9c1c33c490497c67f0e937fc86eb4",
    "device_bound": false
  },
  "permissions": {
    "page_access": {
      "dashboard": [
        "admin",
        "operator",
        "viewer"
      ],
      "timeline": [
        "admin",
        "operator",
        "viewer"
      ],
      "jobs": [
        "admin",
        "operator",
        "viewer"
      ],
      "printers": [
        "admin",
        "operator",
        "viewer"
      ],
      "models": [
        "admin",
        "operator",
        "viewer"
      ],
      "spools": [
        "admin",
        "operator",
        "viewer"
      ],
      "cameras": [
        "admin",
        "operator",
        "viewer"
      ],
      "analytics": [
        "admin",
        "operator",
        "viewer"
      ],
      "calculator": [
        "admin",
        "operator",
        "viewer"
      ],
      "upload": [
        "admin",
        "operator"
      ],
      "maintenance": [
        "admin",
        "operator"
      ],
      "settings": [
        "admin"
      ],
      "admin": [
        "admin"
      ],
      "branding": [
        "admin"
      ],
      "education_reports": [
        "admin",
        "operator"
      ],
      "education": [
        "admin",
        "operator",
        "viewer"
      ],
      "orders": [
        "admin",
        "operator",
        "viewer"
      ],
      "products": [
        "admin",
        "operator",
        "viewer"
      ],
      "alerts": [
        "admin",
        "operator",
        "viewer"
      ]
    },
    "action_access": {
      "settings.edit": [
        "admin"
      ],
      "jobs.create": [
        "admin",
        "operator"
      ],
      "jobs.edit": [
        "admin",
        "operator"
      ],
      "jobs.cancel": [
        "admin",
        "operator"
      ],
      "jobs.delete": [
        "admin",
        "operator"
      ],
      "jobs.start": [
        "admin",
        "operator"
      ],
      "jobs.complete": [
        "admin",
        "operator"
      ],
      "printers.add": [
        "admin"
      ],
      "printers.edit": [
        "admin",
        "operator"
      ],
      "printers.delete": [
        "admin"
      ],
      "printers.slots": [
        "admin",
        "operator"
      ],
      "printers.reorder": [
        "admin",
        "operator"
      ],
      "models.create": [
        "admin",
        "operator"
      ],
      "models.edit": [
        "admin",
        "operator"
      ],
      "models.delete": [
        "admin"
      ],
      "spools.edit": [
        "admin",
        "operator"
      ],
      "spools.delete": [
        "admin"
      ],
      "timeline.move": [
        "admin",
        "operator"
      ],
      "upload.upload": [
        "admin",
        "operator"
      ],
      "upload.schedule": [
        "admin",
        "operator"
      ],
      "upload.delete": [
        "admin",
        "operator"
      ],
      "maintenance.log": [
        "admin",
        "operator"
      ],
      "maintenance.tasks": [
        "admin"
      ],
      "dashboard.actions": [
        "admin",
        "operator"
      ],
      "orders.create": [
        "admin",
        "operator"
      ],
      "orders.edit": [
        "admin"
      ],
      "orders.delete": [
        "admin",
        "operator"
      ],
      "orders.ship": [
        "admin",
        "operator"
      ],
      "products.create": [
        "admin",
        "operator"
      ],
      "products.edit": [
        "admin",
        "operator"
      ],
      "products.delete": [
        "admin"
      ],
      "jobs.approve": [
        "admin",
        "operator"
      ],
      "jobs.reject": [
        "admin",
        "operator"
      ],
      "jobs.resubmit": [
        "admin",
        "operator",
        "viewer"
      ],
      "alerts.read": [
        "admin",
        "operator",
        "viewer"
      ],
      "printers.plug": [
        "admin",
        "operator"
      ]
    }
  },
  "auth_me_admin": {
    "id": 1,
    "username": "certadmin",
    "email": "certadmin@example.test",
    "role": "admin",
    "group_id": null
  }
}
// WIRE-END

const state = vi.hoisted(() => ({ fetch: vi.fn() }))
function respond(data: any, status = 200) { return { ok: status < 400, status, json: async () => data, text: async () => JSON.stringify(data), headers: new Headers({ 'Content-Type': 'application/json' }) } }
function calls(path: string, method: string) { return state.fetch.mock.calls.filter(([u, o]) => String(u) === path && (o?.method || 'GET') === method) }
const clients: QueryClient[] = []
const alpha = () => WIRE.orgs_list.find((o: any) => o.name === 'Org Alpha')
async function access() {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c)
  render(<QueryClientProvider client={c}><LicenseProvider><MemoryRouter initialEntries={['/settings']}><Settings /></MemoryRouter></LicenseProvider></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Access' }))
}
function orgRoot() { return screen.getByRole('heading', { name: 'Organizations' }).closest('div')!.parentElement!.parentElement! as HTMLElement }
async function organizations() {
  await access()
  fireEvent.click(await screen.findByRole('button', { name: 'Organizations' }))
  await screen.findByRole('heading', { name: 'Organizations' })
  await within(orgRoot()).findByText(alpha().name)
  await waitFor(() => expect(within(orgRoot()).getAllByRole('option', { name: WIRE.printers_list[0].name }).length).toBeGreaterThan(0))
  return within(orgRoot())
}
beforeEach(async () => {
  state.fetch.mockReset()
  state.fetch.mockImplementation(async (url: unknown, options: any = {}) => {
    const path = String(url).split('?')[0]; const method = options.method || 'GET'
    if (method === 'GET') {
      const map: Record<string, any> = { '/api/auth/me': WIRE.auth_me_admin, '/api/permissions': WIRE.permissions, '/api/license': WIRE.license,
        '/api/orgs': WIRE.orgs_list, '/api/users': WIRE.users_list_with_orgs, '/api/printers': WIRE.printers_list, '/api/groups': WIRE.groups_list_with_orgs }
      if (path in map) return respond(map[path])
      if (/settings$/.test(path) && path.startsWith('/api/orgs/')) return respond({})
      return respond(/s$|history|logs|backups|webhooks|schedules|sessions|tokens/.test(path) ? [] : {})
    }
    if (path === '/api/orgs' && method === 'POST') return respond({ id: 99, name: JSON.parse(options.body).name, status: 'ok' })
    return respond({ status: 'ok' })
  })
  vi.stubGlobal('fetch', state.fetch)
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  expect(await refreshPermissions()).not.toBeNull()
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('F11-11.5-03 Settings -> Access -> Organizations lists the wire organizations with member counts', async () => {
  const org = await organizations()
  for (const o of WIRE.orgs_list) expect(org.getByText(o.name)).toBeInTheDocument()
  expect(org.getAllByText(`${alpha().member_count} members`).length).toBe(WIRE.orgs_list.filter((o: any) => o.member_count === alpha().member_count).length)
  expect(org.getByRole('button', { name: /New Org/ })).toBeInTheDocument()
})

it('F11-11.5-01/03 creates an organization through POST /api/orgs', async () => {
  const org = await organizations()
  fireEvent.click(org.getByRole('button', { name: /New Org/ }))
  fireEvent.change(org.getByPlaceholderText('Organization name'), { target: { value: 'Org Gamma' } })
  fireEvent.click(org.getByRole('button', { name: 'Create' }))
  await waitFor(() => expect(calls('/api/orgs', 'POST')).toHaveLength(1))
  expect(JSON.parse(calls('/api/orgs', 'POST')[0][1].body)).toEqual({ name: 'Org Gamma' })
})

it('F11-11.5-02 adds a member and assigns a printer through the org endpoints', async () => {
  const org = await organizations()
  const card = org.getByText(alpha().name).closest('div.p-4') as HTMLElement
  const cardScope = within(card)
  const selects = [...card.querySelectorAll('select')]
  const memberSelect = selects.find(s => [...s.options].some(o => o.textContent === 'Add member...'))!
  const printerSelect = selects.find(s => [...s.options].some(o => o.textContent === 'Assign printer...'))!
  const unassigned = WIRE.users_list_with_orgs.find((u: any) => u.group_id === null && u.role !== 'admin')
  const printer = WIRE.printers_list[0]
  fireEvent.change(memberSelect, { target: { value: String(unassigned.id) } })
  fireEvent.click(cardScope.getByRole('button', { name: 'Add' }))
  await waitFor(() => expect(calls(`/api/orgs/${alpha().id}/members`, 'POST')).toHaveLength(1))
  expect(JSON.parse(calls(`/api/orgs/${alpha().id}/members`, 'POST')[0][1].body)).toEqual({ user_id: unassigned.id })
  fireEvent.change(printerSelect, { target: { value: String(printer.id) } })
  fireEvent.click(cardScope.getByRole('button', { name: 'Assign' }))
  await waitFor(() => expect(calls(`/api/orgs/${alpha().id}/printers`, 'POST')).toHaveLength(1))
  expect(JSON.parse(calls(`/api/orgs/${alpha().id}/printers`, 'POST')[0][1].body)).toEqual({ printer_id: printer.id })
  expect(card).toBeTruthy()
})

it('F11-11.5-02 removes a member through the Access user editor (group -> none)', async () => {
  await access()
  const member = WIRE.users_list_with_orgs.find((u: any) => u.group_id === alpha().id)
  const row = (await screen.findByText(member.username, { selector: 'p' })).closest('tr')!
  fireEvent.click(within(row).getByTitle('Edit'))
  await screen.findByRole('heading', { name: 'Edit User' })
  const group = screen.getByText('Group', { selector: 'label', exact: true }).parentElement!.querySelector('select')!
  expect(group).toHaveValue(String(alpha().id))
  fireEvent.change(group, { target: { value: '' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
  await waitFor(() => expect(calls(`/api/users/${member.id}`, 'PATCH')).toHaveLength(1))
  expect(JSON.parse(calls(`/api/users/${member.id}`, 'PATCH')[0][1].body)).toEqual(expect.objectContaining({ group_id: null }))
})
