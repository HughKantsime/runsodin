import { clearSensitiveBrowserState } from '../permissions'
import { notifyLicenseRefusal } from './licenseEvents'

const API_BASE = '/api'

/**
 * Human-readable text for a FastAPI error `detail`, which may be a string, an
 * object such as {message, sent_count, failed_count}, or a validation-error list.
 */
export function errorDetailMessage(detail: unknown): string | null {
  if (detail === null || detail === undefined || detail === '') return null
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail.map(item => errorDetailMessage(item && typeof item === 'object' && 'msg' in item ? (item as { msg: unknown }).msg : item)).filter(Boolean)
    return parts.length ? parts.join('; ') : null
  }
  if (typeof detail === 'object') {
    const record = detail as Record<string, unknown>
    const base = errorDetailMessage(record.message ?? record.detail ?? record.msg)
    const counts = typeof record.sent_count === 'number' && typeof record.failed_count === 'number'
      ? ` (sent ${record.sent_count}, failed ${record.failed_count})` : ''
    if (base) return base + counts
    try { return JSON.stringify(detail) } catch { return null }
  }
  return String(detail)
}

export function dataFolderLockMessage(error: unknown): string | null {
  return error instanceof Error && 'storageReason' in error && typeof error.storageReason === 'string' ? error.message : null
}

export interface ApiError extends Error {
  detail?: unknown
  status?: number
  storageReason?: string
  code?: string
  reason?: string
  retriable?: boolean
}

interface ApiErrorBody {
  detail?: unknown
  message?: unknown
  error?: { storage_reason?: string; code?: string; reason?: string; retriable?: boolean }
}

function apiError(err: ApiErrorBody, fallback: string, status?: number): ApiError {
  const error: ApiError = new Error(errorDetailMessage(err.detail) || errorDetailMessage(err.message) || fallback)
  if (err.detail !== undefined) error.detail = err.detail
  if (typeof err.error?.code === 'string') error.code = err.error.code
  if (typeof err.error?.reason === 'string') error.reason = err.error.reason
  if (typeof err.error?.retriable === 'boolean') error.retriable = err.error.retriable
  const detail = err.detail
  const reason = detail && typeof detail === 'object' && 'storage_reason' in detail ? detail.storage_reason : err.error?.storage_reason
  if (typeof reason === 'string') error.storageReason = reason
  error.status = status
  return error
}

export async function fetchAPI<T = unknown>(endpoint: string, options: RequestInit & { quiet?: boolean; showUnauthorized?: boolean | string; onResponse?: (response: Response) => void } = {}, responseFormat: 'json' | 'csv' = 'json'): Promise<T> {
  const { quiet = false, showUnauthorized = false, onResponse, ...requestOptions } = options
  const headers = new Headers(options.headers)
  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  // credentials: 'include' sends the httpOnly session cookie automatically.
  // No need to read/inject a Bearer token from localStorage.
  let response: Response
  try {
    // headers goes last: requestOptions still carries the caller's raw headers,
    // and spreading it after would drop the JSON Content-Type set above.
    response = await fetch(API_BASE + endpoint, {
      credentials: 'include',
      ...requestOptions,
      headers,
    })
  } catch (networkError) {
    throw new Error('Network error. Check your connection and try again.')
  }
  if (response.status === 401 && showUnauthorized !== true) {
    if (typeof showUnauthorized === 'string') {
      const err = await response.json().catch(() => null)
      if (err?.detail?.code === showUnauthorized) throw apiError(err, 'Request failed. Please try again.', response.status)
    }
    await clearSensitiveBrowserState()
    if (window.location.pathname !== "/login" && window.location.pathname !== "/setup") {
      window.location.href = "/login"
    }
    return undefined as unknown as T
  }
  if (response.status === 403) {
    const err = await response.json().catch(() => ({}))
    notifyLicenseRefusal(response, err.detail)
    throw apiError(err, 'You do not have permission to perform this action.', response.status)
  }
  if (response.status === 404) {
    const err = await response.json().catch(() => ({}))
    throw apiError(err, 'The requested resource was not found.', response.status)
  }
  if (!response.ok) {
    const err = await response.json().catch(() => ({}))
    if (err.detail && !quiet) console.error('[API] Error detail:', err.detail)
    throw apiError(err, 'Request failed. Please try again.', response.status)
  }
  if (response.status === 204) {
    onResponse?.(response)
    return null as unknown as T
  }
  if (responseFormat === 'csv') {
    const mediaType = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase()
    if (mediaType !== 'text/csv') throw new Error('Unexpected export response')
    const csv = await response.text()
    onResponse?.(response)
    return csv as T
  }
  onResponse?.(response)
  return response.json()
}

// Generic blob download helper (for exports)
export const downloadBlob = async (endpoint: string, filename: string): Promise<void> => {
  const res = await fetch(`${API_BASE}${endpoint}`, { credentials: 'include' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    notifyLicenseRefusal(res, err.detail)
    throw apiError(err, 'Export failed', res.status)
  }
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
