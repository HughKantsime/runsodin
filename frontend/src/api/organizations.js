import { fetchAPI } from './client.js'

// Organizations
export const orgs = {
  list: () => fetchAPI('/orgs'),
  create: (data) => fetchAPI('/orgs', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/orgs/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/orgs/${id}`, { method: 'DELETE' }),
  addMember: (orgId, userId) => fetchAPI(`/orgs/${orgId}/members`, {
    method: 'POST', body: JSON.stringify({ user_id: userId })
  }),
  assignPrinter: (orgId, printerId) => fetchAPI(`/orgs/${orgId}/printers`, {
    method: 'POST', body: JSON.stringify({ printer_id: printerId })
  }),
  getSettings: (orgId) => fetchAPI(`/orgs/${orgId}/settings`),
  updateSettings: (orgId, data) => fetchAPI(`/orgs/${orgId}/settings`, {
    method: 'PUT', body: JSON.stringify(data)
  }),
}

// ============== Groups (Education/Enterprise) ==============
export const groups = {
  list: () => fetchAPI('/groups'),
  get: (id) => fetchAPI(`/groups/${id}`),
  create: (data) => fetchAPI('/groups', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/groups/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/groups/${id}`, { method: 'DELETE' }),
}
