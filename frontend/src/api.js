const API_BASE = '/api'

// API Key for authentication - leave empty if auth is disabled
const API_KEY = '5464389e808f206efd9f9febef7743ff7a16911797cb0f058e805c82b33396ce'

async function fetchAPI(endpoint, options = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
  }
  
  // Add API key if configured
  if (API_KEY) {
    headers['X-API-Key'] = API_KEY
  }
  
  const response = await fetch(API_BASE + endpoint, {
    headers,
    ...options,
  })
  if (!response.ok) {
    throw new Error('API error: ' + response.status)
  }
  if (response.status === 204) return null
  return response.json()
}

export const printers = {
  list: (activeOnly = false) => fetchAPI('/printers?active_only=' + activeOnly),
  get: (id) => fetchAPI('/printers/' + id),
  create: (data) => fetchAPI('/printers', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI('/printers/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI('/printers/' + id, { method: 'DELETE' }),
  reorder: (ids) => fetchAPI('/printers/reorder', { method: 'POST', body: JSON.stringify({ printer_ids: ids }) }),
  updateSlot: (printerId, slotNumber, data) => 
    fetchAPI('/printers/' + printerId + '/slots/' + slotNumber, { method: 'PATCH', body: JSON.stringify(data) }),
}

export const jobs = {
  list: (status) => fetchAPI('/jobs' + (status ? '?status=' + status : '')),
  get: (id) => fetchAPI('/jobs/' + id),
  create: (data) => fetchAPI('/jobs', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI('/jobs/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI('/jobs/' + id, { method: 'DELETE' }),
  start: (id) => fetchAPI('/jobs/' + id + '/start', { method: 'POST' }),
  complete: (id) => fetchAPI('/jobs/' + id + '/complete', { method: 'POST' }),
  cancel: (id) => fetchAPI('/jobs/' + id + '/cancel', { method: 'POST' }),
}

export const models = {
  list: () => fetchAPI('/models'),
  get: (id) => fetchAPI('/models/' + id),
  create: (data) => fetchAPI('/models', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI('/models/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI('/models/' + id, { method: 'DELETE' }),
}

export const scheduler = {
  run: () => fetchAPI('/scheduler/run', { method: 'POST' }),
  getTimeline: (days = 7) => fetchAPI('/timeline?days=' + days),
}

export const timeline = {
  get: (startDate, days = 7) => fetchAPI('/timeline?days=' + days + (startDate ? '&start_date=' + startDate : '')),
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

export const analytics = {
  get: () => fetchAPI('/analytics'),
}

export const jobActions = {
  move: (jobId, printerId, scheduledStart) => 
    fetchAPI('/jobs/' + jobId + '/move', { 
      method: 'PATCH', 
      body: JSON.stringify({ printer_id: printerId, scheduled_start: scheduledStart })
    }),
}

// Print Jobs (MQTT tracked)
export const printJobs = {
  list: (params = {}) => {
    const query = new URLSearchParams(params).toString()
    return fetchAPI('/print-jobs' + (query ? '?' + query : ''))
  },
  stats: () => fetchAPI('/print-jobs/stats'),
}

export const printFiles = {
  upload: async (file) => {
    const formData = new FormData()
    formData.append('file', file)
    const response = await fetch(`${API_BASE}/print-files/upload`, {
      method: 'POST',
      headers: { 'X-API-Key': API_KEY },
      body: formData
    })
    if (!response.ok) {
      const err = await response.json()
      throw new Error(err.detail || 'Upload failed')
    }
    return response.json()
  },
  list: () => fetchAPI('/print-files'),
  get: (id) => fetchAPI(`/print-files/${id}`),
  delete: (id) => fetchAPI(`/print-files/${id}`, { method: 'DELETE' }),
  schedule: (fileId, printerId) => {
    const url = printerId 
      ? `/print-files/${fileId}/schedule?printer_id=${printerId}`
      : `/print-files/${fileId}/schedule`
    return fetchAPI(url, { method: 'POST' })
  }
}
