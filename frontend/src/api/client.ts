const API_BASE = '/api'

export async function fetchAPI<T = unknown>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    ...options.headers,
  }

  // credentials: 'include' sends the httpOnly session cookie automatically.
  // No need to read/inject a Bearer token from localStorage.
  let response: Response
  try {
    response = await fetch(API_BASE + endpoint, {
      headers,
      credentials: 'include',
      ...options,
    })
  } catch (networkError) {
    throw new Error('Network error. Check your connection and try again.')
  }
  if (response.status === 401) {
    if (window.location.pathname !== "/login" && window.location.pathname !== "/setup") {
      window.location.href = "/login"
    }
    return undefined as unknown as T
  }
  if (response.status === 403) {
    const err = await response.json().catch(() => ({}))
    throw new Error(err.detail || 'You do not have permission to perform this action.')
  }
  if (response.status === 404) {
    const err = await response.json().catch(() => ({}))
    throw new Error(err.detail || 'The requested resource was not found.')
  }
  if (!response.ok) {
    const err = await response.json().catch(() => ({}))
    if (err.detail) console.error('[API] Error detail:', err.detail)
    throw new Error(err.detail || err.message || 'Request failed. Please try again.')
  }
  if (response.status === 204) return null as unknown as T
  return response.json()
}

// Generic blob download helper (for exports)
export const downloadBlob = async (endpoint: string, filename: string): Promise<void> => {
  const res = await fetch(`${API_BASE}${endpoint}`, { credentials: 'include' })
  if (!res.ok) throw new Error('Export failed')
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
