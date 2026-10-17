import { fetchAPI } from './client'
import { clearSensitiveBrowserState } from '../permissions'
import type {
  User,
  UserCreate,
  UserCreateResult,
  UserUpdate,
  LoginResponse,
  MfaSetupResponse,
  MfaStatus,
  WsTokenResponse,
  AuthCapabilities,
  Session,
  ApiToken,
  ApiTokenCreate,
  ApiTokenCreateResponse,
  License,
  PermissionsConfig,
  Quota,
  QuotaUpdate,
  IpAllowlistConfig,
  AuditLogPage,
  AuditLogParams,
  SetupStatus,
  PublicLicense,
  GdprExportData,
} from '../types'

const API_BASE = '/api'

// Auth
export const auth = {
  login: async (username: string, password: string): Promise<LoginResponse> => {
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
  logout: async (): Promise<void> => {
    try {
      await fetch(`${API_BASE}/auth/logout`, {
        method: 'POST',
        credentials: 'include',
      })
    } finally {
      await clearSensitiveBrowserState()
    }
  },
  me: async (): Promise<User | null> => {
    const response = await fetch(`${API_BASE}/auth/me`, {
      credentials: 'include',
    })
    if (!response.ok) return null
    return response.json()
  },
  wsToken: (): Promise<WsTokenResponse> => fetchAPI('/auth/ws-token', { method: 'POST' }),
  mfaVerify: (mfa_token: string, code: string): Promise<LoginResponse> => fetchAPI('/auth/mfa/verify', {
    method: 'POST', body: JSON.stringify({ mfa_token, code })
  }),
  mfaSetup: (): Promise<MfaSetupResponse> => fetchAPI('/auth/mfa/setup', { method: 'POST' }),
  mfaConfirm: (code: string): Promise<unknown> => fetchAPI('/auth/mfa/confirm', {
    method: 'POST', body: JSON.stringify({ code })
  }),
  mfaDisable: (code: string): Promise<void> => fetchAPI('/auth/mfa', {
    method: 'DELETE', body: JSON.stringify({ code })
  }),
  mfaStatus: (): Promise<MfaStatus> => fetchAPI('/auth/mfa/status'),
  adminDisableMfa: (userId: number): Promise<void> => fetchAPI(`/admin/users/${userId}/mfa`, { method: 'DELETE' }),
  oidcExchange: (code: string): Promise<LoginResponse> => fetchAPI('/auth/oidc/exchange', {
    method: 'POST',
    body: JSON.stringify({ code }),
  }),
  forgotPassword: (email: string): Promise<unknown> => fetchAPI('/auth/forgot-password', {
    method: 'POST',
    body: JSON.stringify({ email }),
  }),
  resetPassword: (token: string, new_password: string): Promise<unknown> => fetchAPI('/auth/reset-password', {
    method: 'POST',
    body: JSON.stringify({ token, new_password }),
  }),
  capabilities: (): Promise<AuthCapabilities> => fetchAPI('/auth/capabilities'),
}

// Session management
export const sessions = {
  list: (): Promise<Session[]> => fetchAPI('/sessions'),
  revoke: (id: number): Promise<void> => fetchAPI(`/sessions/${id}`, { method: 'DELETE' }),
  revokeAll: (): Promise<void> => fetchAPI('/sessions', { method: 'DELETE' }),
}

// Scoped API tokens
export const apiTokens = {
  list: (): Promise<ApiToken[]> => fetchAPI('/tokens'),
  create: (data: ApiTokenCreate): Promise<ApiTokenCreateResponse> => fetchAPI('/tokens', { method: 'POST', body: JSON.stringify(data) }),
  revoke: (id: number): Promise<void> => fetchAPI(`/tokens/${id}`, { method: 'DELETE' }),
}

// Users (admin only)
export const users = {
  list: (): Promise<User[]> => fetchAPI('/users'),
  create: (data: UserCreate): Promise<UserCreateResult> => fetchAPI('/users', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: UserUpdate): Promise<User> => fetchAPI(`/users/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI(`/users/${id}`, { method: 'DELETE' }),
  resetPasswordEmail: (id: number): Promise<unknown> => fetchAPI(`/users/${id}/reset-password-email`, { method: 'POST' }),
  importCsv: async (file: File): Promise<unknown> => {
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
  exportData: (userId: number): Promise<GdprExportData> => fetchAPI(`/users/${userId}/export`),
  eraseData: (userId: number): Promise<void> => fetchAPI(`/users/${userId}/erase`, { method: 'DELETE' }),
}

export const setup = {
  status: (): Promise<SetupStatus> => fetchAPI('/setup/status'),
  createAdmin: (data: UserCreate): Promise<User> => fetchAPI('/setup/admin', { method: 'POST', body: JSON.stringify(data) }),
  testPrinter: (data: Record<string, unknown>): Promise<unknown> => fetchAPI('/setup/test-printer', { method: 'POST', body: JSON.stringify(data) }),
  createPrinter: (data: Record<string, unknown>): Promise<unknown> => fetchAPI('/setup/printer', { method: 'POST', body: JSON.stringify(data) }),
  complete: (): Promise<unknown> => fetchAPI('/setup/complete', { method: 'POST' }),
}

export const license = {
  get: (): Promise<PublicLicense> => fetchAPI('/license'),
  upload: (file: File): Promise<License> => {
    const formData = new FormData()
    formData.append('file', file)
    return fetch('/api/license/upload', { method: 'POST', credentials: 'include', body: formData })
      .then(r => { if (!r.ok) throw new Error('Upload failed'); return r.json() })
  },
  remove: (): Promise<void> => fetchAPI('/license', { method: 'DELETE' }),
  activate: (key: string): Promise<License> => fetchAPI('/license/activate', { method: 'POST', body: JSON.stringify({ key }) }),
  getInstallationId: (): Promise<{ installation_id: string }> => fetchAPI('/license/installation-id'),
  // Offline (air-gapped) exchange: the nonce is the challenge code the license
  // portal issues for this installation ID; ODIN signs it with the device key.
  getActivationRequest: (key: string, nonce: string): Promise<unknown> =>
    fetchAPI('/license/activation-request', { method: 'POST', body: JSON.stringify({ key, nonce }) }),
  getUnactivationRequest: (nonce: string): Promise<unknown> =>
    fetchAPI('/license/unactivation-request', { method: 'POST', body: JSON.stringify({ nonce }) }),
  // Pro trial: ODIN signs the request with the device key. Online it goes to the
  // license server; offline it comes back as a code to paste on the portal.
  requestTrial: (email: string, offline: boolean): Promise<{ status?: 'request_sent'; request_code?: string; expires_in_minutes?: number }> =>
    fetchAPI('/license/trial-request', { method: 'POST', body: JSON.stringify({ email, offline }) }),
}

export const permissions = {
  get: (): Promise<PermissionsConfig> => fetchAPI('/permissions'),
  update: (data: PermissionsConfig): Promise<PermissionsConfig> => fetchAPI('/permissions', { method: 'PUT', body: JSON.stringify(data) }),
  reset: (): Promise<PermissionsConfig> => fetchAPI('/permissions/reset', { method: 'POST' }),
}

// Quotas
export const quotas = {
  getMine: (): Promise<Quota> => fetchAPI('/quotas'),
  adminList: (): Promise<Quota[]> => fetchAPI('/admin/quotas'),
  adminSet: (userId: number, data: QuotaUpdate): Promise<Quota> => fetchAPI(`/admin/quotas/${userId}`, {
    method: 'PUT', body: JSON.stringify(data)
  }),
}

// IP Allowlist
export const ipAllowlist = {
  get: (): Promise<IpAllowlistConfig> => fetchAPI('/config/ip-allowlist'),
  set: (data: IpAllowlistConfig): Promise<IpAllowlistConfig> => fetchAPI('/config/ip-allowlist', {
    method: 'PUT', body: JSON.stringify(data)
  }),
}

// Audit Logs
function auditQuery(params: AuditLogParams, paging = true): string {
  const query = new URLSearchParams()
  if (paging && params.limit !== undefined) query.set('limit', String(params.limit))
  if (paging && params.offset !== undefined) query.set('offset', String(params.offset))
  if (params.entity_type) query.set('entity_type', params.entity_type)
  if (params.action) query.set('action', params.action)
  if (params.date_from) query.set('date_from', params.date_from)
  if (params.date_to) query.set('date_to', params.date_to)
  if (params.q) query.set('q', params.q)
  const qs = query.toString()
  return qs ? '?' + qs : ''
}

export const auditLogs = {
  list: (params: AuditLogParams = {}): Promise<AuditLogPage> => fetchAPI(`/audit-logs${auditQuery(params)}`),
  exportCsv: (params: AuditLogParams = {}): Promise<string> => fetchAPI(`/export/audit-logs${auditQuery(params, false)}`, {}, 'csv'),
}
