const API_BASE = '/api'

export async function fetchAPI(endpoint, options = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
  }

  // credentials: 'include' sends the httpOnly session cookie automatically.
  // No need to read/inject a Bearer token from localStorage.
  const response = await fetch(API_BASE + endpoint, {
    headers,
    credentials: 'include',
    ...options,
  })
  if (response.status === 401) {
    if (window.location.pathname !== "/login" && window.location.pathname !== "/setup") {
      window.location.href = "/login"
    }
    return
  }
  if (!response.ok) {
    const err = await response.json().catch(() => ({}))
    if (err.detail) console.error('[API] Error detail:', err.detail)
    throw new Error('Request failed. Please try again.')
  }
  if (response.status === 204) return null
  return response.json()
}

// Generic blob download helper (for exports)
export const downloadBlob = async (endpoint, filename) => {
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
