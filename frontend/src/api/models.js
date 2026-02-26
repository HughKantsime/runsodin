import { fetchAPI } from './client.js'

const API_BASE = '/api'

export const models = {
  getVariants: (id) => fetchAPI(`/models/${id}/variants`),
  deleteVariant: (modelId, variantId) => fetchAPI(`/models/${modelId}/variants/${variantId}`, { method: 'DELETE' }),
  list: (orgId = null) => fetchAPI('/models' + (orgId != null && typeof orgId !== 'object' ? '?org_id=' + orgId : '')),
  listWithPricing: (category, orgId = null) => fetchAPI('/models-with-pricing' + (category ? '?category=' + category : '') + (orgId != null ? (category ? '&' : '?') + 'org_id=' + orgId : '')),
  get: (id) => fetchAPI('/models/' + id),
  create: (data) => fetchAPI('/models', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI('/models/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI('/models/' + id, { method: 'DELETE' }),
  schedule: (id, printerId) => fetchAPI('/models/' + id + '/schedule' + (printerId ? '?printer_id=' + printerId : ''), { method: 'POST' }),
}

// Model Versioning
export const modelRevisions = {
  list: (modelId) => fetchAPI(`/models/${modelId}/revisions`),
  create: async (modelId, changelog, file) => {
    const formData = new FormData()
    formData.append('changelog', changelog)
    if (file) formData.append('file', file)
    const res = await fetch(`${API_BASE}/models/${modelId}/revisions`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    })
    if (!res.ok) throw new Error('Failed to create revision')
    return res.json()
  },
  revert: (modelId, revNumber) => fetchAPI(`/models/${modelId}/revisions/${revNumber}/revert`, { method: 'POST' }),
}

// Model Cost Calculator
export const modelCost = {
  calculate: (modelId) => fetchAPI(`/models/${modelId}/cost`),
}

// ============== Profiles ==============
export const profilesApi = {
  list: (params = {}) => {
    const q = new URLSearchParams()
    if (params.slicer) q.set('slicer', params.slicer)
    if (params.category) q.set('category', params.category)
    if (params.printer_id) q.set('printer_id', params.printer_id)
    if (params.filament_type) q.set('filament_type', params.filament_type)
    if (params.search) q.set('search', params.search)
    if (params.page) q.set('page', params.page)
    const qs = q.toString()
    return fetchAPI(`/profiles${qs ? '?' + qs : ''}`)
  },
  get: (id) => fetchAPI(`/profiles/${id}`),
  create: (data) => fetchAPI('/profiles', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/profiles/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/profiles/${id}`, { method: 'DELETE' }),
  exportUrl: (id) => `/api/profiles/${id}/export`,
  apply: (id, printerId) => fetchAPI(`/profiles/${id}/apply`, {
    method: 'POST', body: JSON.stringify({ printer_id: printerId }),
  }),
  import: async (file) => {
    const formData = new FormData()
    formData.append('file', file)
    const res = await fetch('/api/profiles/import', { method: 'POST', body: formData, credentials: 'include' })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || 'Import failed')
    }
    return res.json()
  },
}
