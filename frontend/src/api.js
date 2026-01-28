const API_BASE = '/api'

async function fetchAPI(endpoint, options = {}) {
  const response = await fetch(`${API_BASE}${endpoint}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  })
  if (!response.ok) {
    throw new Error(`API error: ${response.status}`)
  }
  if (response.status === 204) return null
  return response.json()
}

export const printers = {
  list: (activeOnly = false) => fetchAPI(`/printers?active_only=${activeOnly}`),
  get: (id) => fetchAPI(`/printers/${id}`),
  create: (data) => fetchAPI('/printers', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/printers/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/printers/${id}`, { method: 'DELETE' }),
  updateSlot: (printerId, slotNumber, data) => 
    fetchAPI(`/printers/${printerId}/slots/${slotNumber}`, { method: 'PATCH', body: JSON.stringify(data) }),
}

export const jobs = {
  list: (status) => fetchAPI(`/jobs${status ? `?status=${status}` : ''}`),
  get: (id) => fetchAPI(`/jobs/${id}`),
  create: (data) => fetchAPI('/jobs', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/jobs/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/jobs/${id}`, { method: 'DELETE' }),
  start: (id) => fetchAPI(`/jobs/${id}/start`, { method: 'POST' }),
  complete: (id) => fetchAPI(`/jobs/${id}/complete`, { method: 'POST' }),
  cancel: (id) => fetchAPI(`/jobs/${id}/cancel`, { method: 'POST' }),
}

export const models = {
  list: () => fetchAPI('/models'),
  get: (id) => fetchAPI(`/models/${id}`),
  create: (data) => fetchAPI('/models', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/models/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/models/${id}`, { method: 'DELETE' }),
}

export const scheduler = {
  run: () => fetchAPI('/scheduler/run', { method: 'POST' }),
  getTimeline: (days = 7) => fetchAPI(`/timeline?days=${days}`),
}

export const timeline = {
  get: (startDate, days = 7) => fetchAPI(`/timeline?days=${days}${startDate ? `&start_date=${startDate}` : ''}`),
}

export const stats = {
  get: () => fetchAPI('/stats'),
}

export const spoolman = {
  getSpools: () => fetchAPI('/spoolman/spools'),
  getFilaments: () => fetchAPI('/spoolman/filaments'),
}

export const filaments = {
  list: () => fetchAPI('/filaments'),
  combined: () => fetchAPI('/filaments/combined'),
  add: (data) => fetchAPI('/filaments', { method: 'POST', body: JSON.stringify(data) }),
}
