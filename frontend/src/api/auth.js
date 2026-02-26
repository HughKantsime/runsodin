import { fetchAPI } from './client.js'

const API_BASE = '/api'

// Auth
export const auth = {
  login: async (username, password) => {
    const formData = new URLSearchParams()
    formData.append('username', username)
    formData.append('password', password)
    const response = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      credentials: 'include',  // receive Set-Cookie from server
      body: formData
    })
    if (!response.ok) {
      const err = await response.json().catch(() => ({}))
      throw new Error(err.detail || 'Login failed')
    }
    return response.json()
  },
  logout: async () => {
    await fetch(`${API_BASE}/auth/logout`, {
      method: 'POST',
      credentials: 'include',
    })
  },
  me: async () => {
    const response = await fetch(`${API_BASE}/auth/me`, {
      credentials: 'include',
    })
    if (!response.ok) return null
    return response.json()
  },
  wsToken: () => fetchAPI('/auth/ws-token', { method: 'POST' }),
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
  oidcExchange: (code) => fetchAPI('/auth/oidc/exchange', {
    method: 'POST',
    body: JSON.stringify({ code }),
  }),
  forgotPassword: (email) => fetchAPI('/auth/forgot-password', {
    method: 'POST',
    body: JSON.stringify({ email }),
  }),
  resetPassword: (token, new_password) => fetchAPI('/auth/reset-password', {
    method: 'POST',
    body: JSON.stringify({ token, new_password }),
  }),
  capabilities: () => fetchAPI('/auth/capabilities'),
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

// Users (admin only)
export const users = {
  list: () => fetchAPI('/users'),
  create: (data) => fetchAPI('/users', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/users/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/users/${id}`, { method: 'DELETE' }),
  resetPasswordEmail: (id) => fetchAPI(`/users/${id}/reset-password-email`, { method: 'POST' }),
  importCsv: async (file) => {
    const formData = new FormData()
    formData.append('file', file)
    const response = await fetch(`${API_BASE}/users/import`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    })
    if (!response.ok) {
      const err = await response.json().catch(() => ({}))
      throw new Error(err.detail || 'Import failed')
    }
    return response.json()
  },
}

// GDPR
export const gdpr = {
  exportData: (userId) => fetchAPI(`/users/${userId}/export`),
  eraseData: (userId) => fetchAPI(`/users/${userId}/erase`, { method: 'DELETE' }),
}

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
    return fetch('/api/license/upload', { method: 'POST', credentials: 'include', body: formData })
      .then(r => { if (!r.ok) throw new Error('Upload failed'); return r.json() })
  },
  remove: () => fetchAPI('/license', { method: 'DELETE' }),
  activate: (key) => fetchAPI('/license/activate', { method: 'POST', body: JSON.stringify({ key }) }),
  getInstallationId: () => fetchAPI('/license/installation-id'),
  getActivationRequest: () => fetchAPI('/license/activation-request'),
}

export const permissions = {
  get: () => fetchAPI('/permissions'),
  update: (data) => fetchAPI('/permissions', { method: 'PUT', body: JSON.stringify(data) }),
  reset: () => fetchAPI('/permissions/reset', { method: 'POST' }),
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
