import { clearSensitiveBrowserState } from '../permissions'

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

function apiError(err: { detail?: unknown; message?: unknown }, fallback: string, status?: number): Error {
  const error = new Error(errorDetailMessage(err.detail) || errorDetailMessage(err.message) || fallback) as Error & { detail?: unknown; status?: number }
  if (err.detail !== undefined) error.detail = err.detail
  error.status = status
  return error
}

export async function fetchAPI<T = unknown>(endpoint: string, options: RequestInit & { quiet?: boolean } = {}, responseFormat: 'json' | 'csv' = 'json'): Promise<T> {
  const { quiet = false, ...requestOptions } = options
  const headers = new Headers(options.headers)
  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  // credentials: 'include' sends the httpOnly session cookie automatically.
  // No need to read/inject a Bearer token from localStorage.
  let response: Response
  try {
    response = await fetch(API_BASE + endpoint, {
      headers,
      credentials: 'include',
      ...requestOptions,
    })
  } catch (networkError) {
    throw new Error('Network error. Check your connection and try again.')
  }
  if (response.status === 401) {
    await clearSensitiveBrowserState()
    if (window.location.pathname !== "/login" && window.location.pathname !== "/setup") {
      window.location.href = "/login"
    }
    return undefined as unknown as T
  }
  if (response.status === 403) {
    const err = await response.json().catch(() => ({}))
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
  if (response.status === 204) return null as unknown as T
  if (responseFormat === 'csv') {
    const mediaType = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase()
    if (mediaType !== 'text/csv') throw new Error('Unexpected export response')
    return await response.text() as T
  }
  return response.json()
}

// Generic blob download helper (for exports)
export const downloadBlob = async (endpoint: string, filename: string): Promise<void> => {
  const res = await fetch(`${API_BASE}${endpoint}`, { credentials: 'include' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw apiError(err, 'Export failed')
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
