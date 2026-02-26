import { fetchAPI } from './client.js'

// QR code spool scanning + full CRUD
export const spools = {
  list: (filters = {}) => {
    const params = new URLSearchParams()
    if (filters.status) params.append('status', filters.status)
    if (filters.printer_id) params.append('printer_id', filters.printer_id)
    if (filters.org_id != null) params.append('org_id', filters.org_id)
    return fetchAPI(`/spools?${params}`)
  },
  get: (id) => fetchAPI(`/spools/${id}`),
  create: (data) => fetchAPI('/spools', { method: 'POST', body: JSON.stringify(data) }),
  update: ({ id, ...data }) => fetchAPI(`/spools/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  load: ({ id, printer_id, slot_number }) => fetchAPI(`/spools/${id}/load`, {
    method: 'POST', body: JSON.stringify({ printer_id, slot_number }),
  }),
  unload: ({ id, storage_location }) => fetchAPI(`/spools/${id}/unload?storage_location=${storage_location || ''}`, { method: 'POST' }),
  use: ({ id, weight_used_g, notes }) => fetchAPI(`/spools/${id}/use`, {
    method: 'POST', body: JSON.stringify({ weight_used_g, notes }),
  }),
  archive: (id) => fetchAPI(`/spools/${id}`, { method: 'DELETE' }),
  lookup: (qrCode) => fetchAPI(`/spools/lookup/${qrCode}`),
  scanAssign: (qrCode, printerId, slot) => fetchAPI('/spools/scan-assign', {
    method: 'POST',
    body: JSON.stringify({ qr_code: qrCode, printer_id: printerId, slot: slot }),
  }),
  logDrying: (spoolId, data) => {
    const params = new URLSearchParams({ duration_hours: data.duration_hours, method: data.method || 'dryer' })
    if (data.temp_c) params.set('temp_c', data.temp_c)
    if (data.notes) params.set('notes', data.notes)
    return fetchAPI(`/spools/${spoolId}/dry?${params}`, { method: 'POST' })
  },
  dryingHistory: (spoolId) => fetchAPI(`/spools/${spoolId}/drying-history`),
};

export const filaments = {
  list: () => fetchAPI('/filaments'),
  combined: () => fetchAPI('/filaments/combined'),
  add: (data) => fetchAPI('/filaments', { method: 'POST', body: JSON.stringify(data) }),
  create: (data) => fetchAPI('/filaments', { method: 'POST', body: JSON.stringify(data) }),
  update: ({ id, ...data }) => fetchAPI(`/filaments/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  remove: (id) => fetchAPI(`/filaments/${id}`, { method: 'DELETE' }),
}

// Consumables
export const consumables = {
  list: (status) => fetchAPI('/consumables' + (status ? '?status=' + status : '')),
  get: (id) => fetchAPI(`/consumables/${id}`),
  create: (data) => fetchAPI('/consumables', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/consumables/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/consumables/${id}`, { method: 'DELETE' }),
  adjust: (id, data) => fetchAPI(`/consumables/${id}/adjust`, { method: 'POST', body: JSON.stringify(data) }),
  lowStock: () => fetchAPI('/consumables/low-stock'),
}
