import { fetchAPI } from './client'
import type {
  Organization,
  OrgCreate,
  OrgUpdate,
  OrgSettings,
  Group,
  GroupCreate,
  GroupUpdate,
} from '../types'

// Organizations
export const orgs = {
  list: (): Promise<Organization[]> => fetchAPI('/orgs'),
  create: (data: OrgCreate): Promise<Organization> => fetchAPI('/orgs', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: OrgUpdate): Promise<Organization> => fetchAPI(`/orgs/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI(`/orgs/${id}`, { method: 'DELETE' }),
  addMember: (orgId: number, userId: number): Promise<unknown> => fetchAPI(`/orgs/${orgId}/members`, {
    method: 'POST', body: JSON.stringify({ user_id: userId })
  }),
  assignPrinter: (orgId: number, printerId: number): Promise<unknown> => fetchAPI(`/orgs/${orgId}/printers`, {
    method: 'POST', body: JSON.stringify({ printer_id: printerId })
  }),
  getSettings: (orgId: number): Promise<OrgSettings> => fetchAPI(`/orgs/${orgId}/settings`),
  updateSettings: (orgId: number, data: OrgSettings): Promise<OrgSettings> => fetchAPI(`/orgs/${orgId}/settings`, {
    method: 'PUT', body: JSON.stringify(data)
  }),
}

// ============== Groups (Education/Enterprise) ==============
export const groups = {
  list: (): Promise<Group[]> => fetchAPI('/groups'),
  get: (id: number): Promise<Group> => fetchAPI(`/groups/${id}`),
  create: (data: GroupCreate): Promise<Group> => fetchAPI('/groups', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: GroupUpdate): Promise<Group> => fetchAPI(`/groups/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI(`/groups/${id}`, { method: 'DELETE' }),
}
