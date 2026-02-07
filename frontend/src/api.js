const API_BASE = '/api'

// API Key for authentication - leave empty if auth is disabled
const API_KEY = import.meta.env.VITE_API_KEY

async function fetchAPI(endpoint, options = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
  }
  
  // Add API key if configured
  if (API_KEY) {
    headers['X-API-Key'] = API_KEY
  }

  // Add JWT token if available
  const token = localStorage.getItem("token")
  if (token) {
    headers["Authorization"] = "Bearer " + token
  }
  
  const response = await fetch(API_BASE + endpoint, {
    headers,
    ...options,
  })
  if (response.status === 401) {
    localStorage.removeItem("token")
    localStorage.removeItem("user")
    window.location.href = "/login"
    return
  }
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
  toggleLights: (id) => fetchAPI(`/printers/${id}/lights`, { method: 'POST' }),
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
  getVariants: (id) => fetchAPI(`/models/${id}/variants`),
  deleteVariant: (modelId, variantId) => fetchAPI(`/models/${modelId}/variants/${variantId}`, { method: 'DELETE' }),
  list: () => fetchAPI('/models'),
  listWithPricing: (category) => fetchAPI('/models-with-pricing' + (category ? '?category=' + category : '')),
  get: (id) => fetchAPI('/models/' + id),
  create: (data) => fetchAPI('/models', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI('/models/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI('/models/' + id, { method: 'DELETE' }),
  schedule: (id, printerId) => fetchAPI('/models/' + id + '/schedule' + (printerId ? '?printer_id=' + printerId : ''), { method: 'POST' }),
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
    if (response.status === 401) {
    localStorage.removeItem("token")
    localStorage.removeItem("user")
    window.location.href = "/login"
    return
  }
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

// Auth
export const auth = {
  login: async (username, password) => {
    const formData = new URLSearchParams()
    formData.append('username', username)
    formData.append('password', password)
    const response = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: formData
    })
    if (response.status === 401) {
    localStorage.removeItem("token")
    localStorage.removeItem("user")
    window.location.href = "/login"
    return
  }
  if (!response.ok) {
      const err = await response.json()
      throw new Error(err.detail || 'Login failed')
    }
    return response.json()
  },
  me: async () => {
    const token = localStorage.getItem('token')
    if (!token) return null
    const response = await fetch(`${API_BASE}/auth/me`, {
      headers: { 'Authorization': `Bearer ${token}` }
    })
    if (!response.ok) return null
    return response.json()
  }
}

// Users (admin only)
export const users = {
  list: async () => {
    const token = localStorage.getItem('token')
    const response = await fetch(`${API_BASE}/users`, {
      headers: { 'Authorization': `Bearer ${token}` }
    })
    if (!response.ok) throw new Error('Failed to fetch users')
    return response.json()
  },
  create: async (data) => {
    const token = localStorage.getItem('token')
    const response = await fetch(`${API_BASE}/users`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      body: JSON.stringify(data)
    })
    if (!response.ok) throw new Error('Failed to create user')
    return response.json()
  },
  update: async (id, data) => {
    const token = localStorage.getItem('token')
    const response = await fetch(`${API_BASE}/users/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      body: JSON.stringify(data)
    })
    if (!response.ok) throw new Error('Failed to update user')
    return response.json()
  },
  delete: async (id) => {
    const token = localStorage.getItem('token')
    const response = await fetch(`${API_BASE}/users/${id}`, {
      method: 'DELETE',
      headers: { 'Authorization': `Bearer ${token}` }
    })
    if (!response.ok) throw new Error('Failed to delete user')
    return response.json()
  }
}


export const maintenance = {
  getStatus: () => fetchAPI('/maintenance/status'),
  getTasks: () => fetchAPI('/maintenance/tasks'),
  createTask: (data) => fetchAPI('/maintenance/tasks', { method: 'POST', body: JSON.stringify(data) }),
  updateTask: (id, data) => fetchAPI('/maintenance/tasks/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteTask: (id) => fetchAPI('/maintenance/tasks/' + id, { method: 'DELETE' }),
  getLogs: (printerId) => fetchAPI('/maintenance/logs' + (printerId ? '?printer_id=' + printerId : '')),
  createLog: (data) => fetchAPI('/maintenance/logs', { method: 'POST', body: JSON.stringify(data) }),
  deleteLog: (id) => fetchAPI('/maintenance/logs/' + id, { method: 'DELETE' }),
  seedDefaults: () => fetchAPI('/maintenance/seed-defaults', { method: 'POST' }),
}


export const permissions = {
  get: () => fetchAPI('/permissions'),
  update: (data) => fetchAPI('/permissions', { method: 'PUT', body: JSON.stringify(data) }),
  reset: () => fetchAPI('/permissions/reset', { method: 'POST' }),
}
// Pricing Config
export const pricingConfig = {
  get: () => fetchAPI('/pricing-config'),
  update: (data) => fetchAPI('/pricing-config', { method: 'PUT', body: JSON.stringify(data) }),
}

// Model Cost Calculator
export const modelCost = {
  calculate: (modelId) => fetchAPI(`/models/${modelId}/cost`),
}


// Products (v0.14.0)
export const products = {
  list: () => fetchAPI('/products'),
  get: (id) => fetchAPI(`/products/${id}`),
  create: (data) => fetchAPI('/products', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/products/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/products/${id}`, { method: 'DELETE' }),
  addComponent: (productId, data) => fetchAPI(`/products/${productId}/components`, { method: 'POST', body: JSON.stringify(data) }),
  removeComponent: (productId, componentId) => fetchAPI(`/products/${productId}/components/${componentId}`, { method: 'DELETE' }),
}

// Orders (v0.14.0)
export const orders = {
  list: (status, platform) => {
    const params = new URLSearchParams()
    if (status) params.append('status_filter', status)
    if (platform) params.append('platform', platform)
    const query = params.toString()
    return fetchAPI('/orders' + (query ? '?' + query : ''))
  },
  get: (id) => fetchAPI(`/orders/${id}`),
  create: (data) => fetchAPI('/orders', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/orders/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/orders/${id}`, { method: 'DELETE' }),
  addItem: (orderId, data) => fetchAPI(`/orders/${orderId}/items`, { method: 'POST', body: JSON.stringify(data) }),
  updateItem: (orderId, itemId, data) => fetchAPI(`/orders/${orderId}/items/${itemId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  removeItem: (orderId, itemId) => fetchAPI(`/orders/${orderId}/items/${itemId}`, { method: 'DELETE' }),
  schedule: (id) => fetchAPI(`/orders/${id}/schedule`, { method: 'POST' }),
  ship: (id, data) => fetchAPI(`/orders/${id}/ship`, { method: 'PATCH', body: JSON.stringify(data) }),
}

export const search = {
  query: (q) => fetchAPI('/search?q=' + encodeURIComponent(q)),
}


// QR code spool scanning
export const spools = {
  lookup: (qrCode) => fetchAPI(`/spools/lookup/${qrCode}`),
  scanAssign: (qrCode, printerId, slot) => fetchAPI('/spools/scan-assign', {
    method: 'POST',
    body: JSON.stringify({ qr_code: qrCode, printer_id: printerId, slot: slot }),
  }),
};


// ============== Alerts & Notifications (v0.17.0) ==============

export const alerts = {
  list: (params = {}) => {
    const query = new URLSearchParams();
    if (params.severity) query.set('severity', params.severity);
    if (params.alert_type) query.set('alert_type', params.alert_type);
    if (params.is_read !== undefined) query.set('is_read', params.is_read);
    if (params.limit) query.set('limit', params.limit);
    if (params.offset) query.set('offset', params.offset);
    const qs = query.toString();
    return fetchAPI(`/alerts${qs ? '?' + qs : ''}`);
  },
  unreadCount: () => fetchAPI('/alerts/unread-count'),
  summary: () => fetchAPI('/alerts/summary'),
  markRead: (id) => fetchAPI(`/alerts/${id}/read`, { method: 'PATCH' }),
  markAllRead: () => fetchAPI('/alerts/mark-all-read', { method: 'POST' }),
  dismiss: (id) => fetchAPI(`/alerts/${id}/dismiss`, { method: 'PATCH' }),
};

export const alertPreferences = {
  get: () => fetchAPI('/alert-preferences'),
  update: (preferences) => fetchAPI('/alert-preferences', {
    method: 'PUT',
    body: JSON.stringify({ preferences }),
  }),
};

export const smtpConfig = {
  get: () => fetchAPI('/smtp-config'),
  update: (data) => fetchAPI('/smtp-config', {
    method: 'PUT',
    body: JSON.stringify(data),
  }),
  testEmail: () => fetchAPI('/alerts/test-email', { method: 'POST' }),
};

export const pushNotifications = {
  getVapidKey: () => fetchAPI('/push/vapid-key'),
  subscribe: (data) => fetchAPI('/push/subscribe', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  unsubscribe: () => fetchAPI('/push/subscribe', { method: 'DELETE' }),
};

export const setup = {
  status: () => fetchAPI('/setup/status'),
  createAdmin: (data) => fetchAPI('/setup/admin', { method: 'POST', body: JSON.stringify(data) }),
  testPrinter: (data) => fetchAPI('/setup/test-printer', { method: 'POST', body: JSON.stringify(data) }),
  createPrinter: (data) => fetchAPI('/setup/printer', { method: 'POST', body: JSON.stringify(data) }),
  complete: () => fetchAPI('/setup/complete', { method: 'POST' }),
}

export const license = {
  get: () => fetchAPI('/license'),
  upload: (file) => {
    const formData = new FormData()
    formData.append('file', file)
    const token = localStorage.getItem('token')
    const headers = {}
    const API_KEY = import.meta.env.VITE_API_KEY
    if (API_KEY) headers['X-API-Key'] = API_KEY
    if (token) headers['Authorization'] = 'Bearer ' + token
    return fetch('/api/license/upload', { method: 'POST', headers, body: formData })
      .then(r => { if (!r.ok) throw new Error('Upload failed'); return r.json() })
  },
  remove: () => fetchAPI('/license', { method: 'DELETE' }),
}
