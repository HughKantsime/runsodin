const API_BASE = '/api'

// API Key for authentication - leave empty if auth is disabled
const API_KEY = import.meta.env.VITE_API_KEY

export async function fetchAPI(endpoint, options = {}) {
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
    if (window.location.pathname !== "/login") {
      window.location.href = "/login"
    }
    return
  }
  if (!response.ok) {
    throw new Error('API error: ' + response.status)
  }
  if (response.status === 204) return null
  return response.json()
}

export const printers = {
  list: (activeOnly = false, tag = '', orgId = null) => fetchAPI('/printers?active_only=' + activeOnly + (tag ? '&tag=' + encodeURIComponent(tag) : '') + (orgId != null ? '&org_id=' + orgId : '')),
  allTags: () => fetchAPI('/printers/tags'),
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
  list: (status, orgId = null) => fetchAPI('/jobs' + (status ? '?status=' + status : '') + (orgId != null ? (status ? '&' : '?') + 'org_id=' + orgId : '')),
  get: (id) => fetchAPI('/jobs/' + id),
  create: (data) => fetchAPI('/jobs', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI('/jobs/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI('/jobs/' + id, { method: 'DELETE' }),
  start: (id) => fetchAPI('/jobs/' + id + '/start', { method: 'POST' }),
  complete: (id) => fetchAPI('/jobs/' + id + '/complete', { method: 'POST' }),
  cancel: (id) => fetchAPI('/jobs/' + id + '/cancel', { method: 'POST' }),
  reorder: (jobIds) => fetchAPI('/jobs/reorder', { method: 'PATCH', body: JSON.stringify({ job_ids: jobIds }) }),
  repeat: (id) => fetchAPI('/jobs/' + id + '/repeat', { method: 'POST' }),
}

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
  timeAccuracy: (days = 30) => fetchAPI(`/analytics/time-accuracy?days=${days}`),
  failures: (days = 30) => fetchAPI(`/analytics/failures?days=${days}`),
}

export const educationReports = {
  getUsageReport: (days = 30) => fetchAPI(`/education/usage-report?days=${days}`),
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
    const uploadHeaders = {}
    if (API_KEY) uploadHeaders['X-API-Key'] = API_KEY
    const tk = localStorage.getItem('token')
    if (tk) uploadHeaders['Authorization'] = 'Bearer ' + tk
    const response = await fetch(`${API_BASE}/print-files/upload`, {
      method: 'POST',
      headers: uploadHeaders,
      body: formData
    })
    if (response.status === 401) {
    localStorage.removeItem("token")
    localStorage.removeItem("user")
    if (window.location.pathname !== "/login") {
      window.location.href = "/login"
    }
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
    if (window.location.pathname !== "/login") {
      window.location.href = "/login"
    }
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
  },
  mfaVerify: (mfa_token, code) => fetchAPI('/auth/mfa/verify', {
    method: 'POST', body: JSON.stringify({ mfa_token, code })
  }),
  mfaSetup: () => fetchAPI('/auth/mfa/setup', { method: 'POST' }),
  mfaConfirm: (code) => fetchAPI('/auth/mfa/confirm', {
    method: 'POST', body: JSON.stringify({ code })
  }),
  mfaDisable: (code) => fetchAPI('/auth/mfa', {
    method: 'DELETE', body: JSON.stringify({ code })
  }),
  mfaStatus: () => fetchAPI('/auth/mfa/status'),
  adminDisableMfa: (userId) => fetchAPI(`/admin/users/${userId}/mfa`, { method: 'DELETE' }),
}

// Session management
export const sessions = {
  list: () => fetchAPI('/sessions'),
  revoke: (id) => fetchAPI(`/sessions/${id}`, { method: 'DELETE' }),
  revokeAll: () => fetchAPI('/sessions', { method: 'DELETE' }),
}

// Scoped API tokens
export const apiTokens = {
  list: () => fetchAPI('/tokens'),
  create: (data) => fetchAPI('/tokens', { method: 'POST', body: JSON.stringify(data) }),
  revoke: (id) => fetchAPI(`/tokens/${id}`, { method: 'DELETE' }),
}

// Quotas
export const quotas = {
  getMine: () => fetchAPI('/quotas'),
  adminList: () => fetchAPI('/admin/quotas'),
  adminSet: (userId, data) => fetchAPI(`/admin/quotas/${userId}`, {
    method: 'PUT', body: JSON.stringify(data)
  }),
}

// IP Allowlist
export const ipAllowlist = {
  get: () => fetchAPI('/config/ip-allowlist'),
  set: (data) => fetchAPI('/config/ip-allowlist', {
    method: 'PUT', body: JSON.stringify(data)
  }),
}

// GDPR
export const gdpr = {
  exportData: (userId) => fetchAPI(`/users/${userId}/export`),
  eraseData: (userId) => fetchAPI(`/users/${userId}/erase`, { method: 'DELETE' }),
}

// Data Retention
export const retention = {
  get: () => fetchAPI('/config/retention'),
  set: (data) => fetchAPI('/config/retention', {
    method: 'PUT', body: JSON.stringify(data)
  }),
  runCleanup: () => fetchAPI('/admin/retention/cleanup', { method: 'POST' }),
}

// Scheduled Reports
export const reportSchedules = {
  list: () => fetchAPI('/report-schedules'),
  create: (data) => fetchAPI('/report-schedules', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/report-schedules/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/report-schedules/${id}`, { method: 'DELETE' }),
}

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
}

// Chargebacks
export const chargebacks = {
  report: (startDate, endDate) => {
    let q = '/reports/chargebacks?'
    if (startDate) q += `start_date=${startDate}&`
    if (endDate) q += `end_date=${endDate}`
    return fetchAPI(q)
  },
}

// Model Versioning
export const modelRevisions = {
  list: (modelId) => fetchAPI(`/models/${modelId}/revisions`),
  create: async (modelId, changelog, file) => {
    const formData = new FormData()
    formData.append('changelog', changelog)
    if (file) formData.append('file', file)
    const token = localStorage.getItem('token')
    const res = await fetch(`${API_BASE}/models/${modelId}/revisions`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}`, 'X-API-Key': API_KEY },
      body: formData,
    })
    if (!res.ok) throw new Error('Failed to create revision')
    return res.json()
  },
  revert: (modelId, revNumber) => fetchAPI(`/models/${modelId}/revisions/${revNumber}/revert`, { method: 'POST' }),
}

// Bulk Operations
export const bulkOps = {
  jobs: (jobIds, action, extra = {}) => fetchAPI('/jobs/bulk-update', {
    method: 'POST', body: JSON.stringify({ job_ids: jobIds, action, ...extra })
  }),
  printers: (printerIds, action, extra = {}) => fetchAPI('/printers/bulk-update', {
    method: 'POST', body: JSON.stringify({ printer_ids: printerIds, action, ...extra })
  }),
  spools: (spoolIds, action, extra = {}) => fetchAPI('/spools/bulk-update', {
    method: 'POST', body: JSON.stringify({ spool_ids: spoolIds, action, ...extra })
  }),
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


// Printer Telemetry & Nozzle Lifecycle
export const printerTelemetry = {
  get: (printerId, hours = 24) => fetchAPI(`/printers/${printerId}/telemetry?hours=${hours}`),
  hmsHistory: (printerId, days = 30) => fetchAPI(`/printers/${printerId}/hms-history?days=${days}`),
  nozzle: (printerId) => fetchAPI(`/printers/${printerId}/nozzle`),
  installNozzle: (printerId, data) => fetchAPI(`/printers/${printerId}/nozzle`, { method: 'POST', body: JSON.stringify(data) }),
  retireNozzle: (printerId, nozzleId) => fetchAPI(`/printers/${printerId}/nozzle/${nozzleId}/retire`, { method: 'PATCH' }),
  nozzleHistory: (printerId) => fetchAPI(`/printers/${printerId}/nozzle/history`),
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

// Products (v0.14.0)
export const products = {
  list: () => fetchAPI('/products'),
  get: (id) => fetchAPI(`/products/${id}`),
  create: (data) => fetchAPI('/products', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/products/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/products/${id}`, { method: 'DELETE' }),
  addComponent: (productId, data) => fetchAPI(`/products/${productId}/components`, { method: 'POST', body: JSON.stringify(data) }),
  removeComponent: (productId, componentId) => fetchAPI(`/products/${productId}/components/${componentId}`, { method: 'DELETE' }),
  addConsumable: (productId, data) => fetchAPI(`/products/${productId}/consumables`, { method: 'POST', body: JSON.stringify(data) }),
  removeConsumable: (productId, linkId) => fetchAPI(`/products/${productId}/consumables/${linkId}`, { method: 'DELETE' }),
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

export const presets = {
  list: () => fetchAPI('/presets'),
  create: (data) => fetchAPI('/presets', { method: 'POST', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/presets/${id}`, { method: 'DELETE' }),
  schedule: (id) => fetchAPI(`/presets/${id}/schedule`, { method: 'POST' }),
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
  logDrying: (spoolId, data) => {
    const params = new URLSearchParams({ duration_hours: data.duration_hours, method: data.method || 'dryer' })
    if (data.temp_c) params.set('temp_c', data.temp_c)
    if (data.notes) params.set('notes', data.notes)
    return fetchAPI(`/spools/${spoolId}/dry?${params}`, { method: 'POST' })
  },
  dryingHistory: (spoolId) => fetchAPI(`/spools/${spoolId}/drying-history`),
};


// ============== Groups (Education/Enterprise) ==============

export const groups = {
  list: () => fetchAPI('/groups'),
  get: (id) => fetchAPI(`/groups/${id}`),
  create: (data) => fetchAPI('/groups', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/groups/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/groups/${id}`, { method: 'DELETE' }),
}

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


// === Job Approval Workflow (v0.18.0) ===

export async function approveJob(jobId) {
  return fetchAPI(`/jobs/${jobId}/approve`, { method: 'POST' });
}

export async function rejectJob(jobId, reason) {
  return fetchAPI(`/jobs/${jobId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
}

export async function resubmitJob(jobId) {
  return fetchAPI(`/jobs/${jobId}/resubmit`, { method: 'POST' });
}

export async function getApprovalSetting() {
  return fetchAPI('/config/require-job-approval');
}

export async function setApprovalSetting(enabled) {
  return fetchAPI('/config/require-job-approval', {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  });
}


// Failure logging
export async function getFailureReasons() {
  return fetchAPI('/failure-reasons');
}
export async function updateJobFailure(jobId, failReason, failNotes) {
  return fetchAPI('/jobs/' + jobId + '/failure', {
    method: 'PATCH',
    body: JSON.stringify({ fail_reason: failReason, fail_notes: failNotes }),
  });
}

// License
export const licenseApi = {
  get: () => fetchAPI('/license'),
  upload: (formData) => {
    const h = {}
    if (API_KEY) h['X-API-Key'] = API_KEY
    const tk = localStorage.getItem('token')
    if (tk) h['Authorization'] = 'Bearer ' + tk
    return fetch('/api/license/upload', {
      method: 'POST',
      headers: h,
      body: formData,
    }).then(r => r.json())
  },
  remove: () => fetchAPI('/license', { method: 'DELETE' }),
}



// ---- Smart Plug ----
export const getPlugConfig = (printerId) => fetchAPI(`/printers/${printerId}/plug`)
export const updatePlugConfig = (printerId, config) => fetchAPI(`/printers/${printerId}/plug`, { method: 'PUT', body: JSON.stringify(config) })
export const removePlugConfig = (printerId) => fetchAPI(`/printers/${printerId}/plug`, { method: 'DELETE' })
export const plugPowerOn = (printerId) => fetchAPI(`/printers/${printerId}/plug/on`, { method: 'POST' })
export const plugPowerOff = (printerId) => fetchAPI(`/printers/${printerId}/plug/off`, { method: 'POST' })
export const plugPowerToggle = (printerId) => fetchAPI(`/printers/${printerId}/plug/toggle`, { method: 'POST' })
export const getPlugEnergy = (printerId) => fetchAPI(`/printers/${printerId}/plug/energy`)
export const getPlugState = (printerId) => fetchAPI(`/printers/${printerId}/plug/state`)
export const getEnergyRate = () => fetchAPI('/settings/energy-rate')
export const setEnergyRate = (rate) => fetchAPI('/settings/energy-rate', { method: 'PUT', body: JSON.stringify({ energy_cost_per_kwh: rate }) })

// ---- AMS Environmental Monitoring ----
export const getAmsEnvironment = (printerId, hours = 24, unit = null) => {
  let url = `/printers/${printerId}/ams/environment?hours=${hours}`
  if (unit !== null) url += `&unit=${unit}`
  return fetchAPI(url)
}
export const getAmsCurrent = (printerId) => fetchAPI(`/printers/${printerId}/ams/current`)

// ---- Language / i18n ----
export const getLanguage = () => fetchAPI('/settings/language')
export const setLanguage = (lang) => fetchAPI('/settings/language', { method: 'PUT', body: JSON.stringify({ language: lang }) })

// ---- Vision AI ----
// Audit Logs
export const auditLogs = {
  list: (params = {}) => {
    const query = new URLSearchParams()
    if (params.limit) query.set('limit', params.limit)
    if (params.offset) query.set('offset', params.offset)
    if (params.entity_type) query.set('entity_type', params.entity_type)
    if (params.action) query.set('action', params.action)
    if (params.date_from) query.set('date_from', params.date_from)
    if (params.date_to) query.set('date_to', params.date_to)
    const qs = query.toString()
    return fetchAPI(`/audit-logs${qs ? '?' + qs : ''}`)
  },
}

// ============== Timelapses ==============
export const timelapses = {
  list: (params = {}) => {
    const query = new URLSearchParams()
    if (params.printer_id) query.set('printer_id', params.printer_id)
    if (params.status) query.set('status', params.status)
    if (params.limit) query.set('limit', params.limit)
    if (params.offset) query.set('offset', params.offset)
    const qs = query.toString()
    return fetchAPI(`/timelapses${qs ? '?' + qs : ''}`)
  },
  videoUrl: (id) => {
    const token = localStorage.getItem('token')
    return `/api/timelapses/${id}/video${token ? '?token=' + token : ''}`
  },
  delete: (id) => fetchAPI(`/timelapses/${id}`, { method: 'DELETE' }),
}

export const vision = {
  getDetections: (params) => fetchAPI('/vision/detections?' + new URLSearchParams(params)),
  getDetection: (id) => fetchAPI('/vision/detections/' + id),
  reviewDetection: (id, status) => fetchAPI(`/vision/detections/${id}`, { method: 'PATCH', body: JSON.stringify({ status }) }),
  getSettings: () => fetchAPI('/vision/settings'),
  updateSettings: (data) => fetchAPI('/vision/settings', { method: 'PATCH', body: JSON.stringify(data) }),
  getPrinterSettings: (id) => fetchAPI(`/printers/${id}/vision`),
  updatePrinterSettings: (id, data) => fetchAPI(`/printers/${id}/vision`, { method: 'PATCH', body: JSON.stringify(data) }),
  getModels: () => fetchAPI('/vision/models'),
  activateModel: (id) => fetchAPI(`/vision/models/${id}/activate`, { method: 'PATCH' }),
  getStats: (days) => fetchAPI('/vision/stats?days=' + (days || 7)),
}
