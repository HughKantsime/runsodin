import { fetchAPI } from './client.js'

const API_BASE = '/api'

// Config
export const config = {
  get: () => fetchAPI('/config'),
  update: (data) => fetchAPI('/config', { method: 'PUT', body: JSON.stringify(data) }),
  getNetwork: () => fetchAPI('/setup/network'),
  saveNetwork: (data) => fetchAPI('/setup/network', { method: 'POST', body: JSON.stringify(data) }),
  testSpoolman: () => fetchAPI('/spoolman/test'),
}

// Backups
export const backups = {
  list: () => fetchAPI('/backups'),
  create: () => fetchAPI('/backups', { method: 'POST' }),
  remove: (filename) => fetchAPI(`/backups/${filename}`, { method: 'DELETE' }),
  download: async (filename) => {
    const res = await fetch(`${API_BASE}/backups/${filename}`, { credentials: 'include' })
    if (!res.ok) throw new Error('Download failed')
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  },
}

export const adminBundle = {
  download: () => fetch('/api/admin/support-bundle', { credentials: 'include' })
    .then(res => {
      if (!res.ok) throw new Error('Failed to generate bundle')
      return res.blob()
    })
    .then(blob => {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `odin-support-bundle-${new Date().toISOString().slice(0, 10)}.zip`
      a.click()
      URL.revokeObjectURL(url)
    }),
};

export const adminLogs = {
  get: (source = 'backend', lines = 200, level = null, search = null) => {
    const params = new URLSearchParams({ source, lines })
    if (level) params.set('level', level)
    if (search) params.set('search', search)
    return fetchAPI(`/admin/logs?${params}`)
  },
  streamUrl: (source = 'backend', level = null, search = null) => {
    const params = new URLSearchParams({ source })
    if (level) params.set('level', level)
    if (search) params.set('search', search)
    return `/api/admin/logs/stream?${params}`
  },
};

export const smtpConfig = {
  get: () => fetchAPI('/smtp-config'),
  update: (data) => fetchAPI('/smtp-config', {
    method: 'PUT',
    body: JSON.stringify(data),
  }),
  testEmail: () => fetchAPI('/alerts/test-email', { method: 'POST' }),
};

// Data Retention
export const retention = {
  get: () => fetchAPI('/config/retention'),
  set: (data) => fetchAPI('/config/retention', {
    method: 'PUT', body: JSON.stringify(data)
  }),
  runCleanup: () => fetchAPI('/admin/retention/cleanup', { method: 'POST' }),
}

export const getEnergyRate = () => fetchAPI('/settings/energy-rate')
export const setEnergyRate = (rate) => fetchAPI('/settings/energy-rate', { method: 'PUT', body: JSON.stringify({ energy_cost_per_kwh: rate }) })

// ---- Education Mode ----
export const getEducationMode = () => fetchAPI('/settings/education-mode')
export const setEducationMode = (enabled) => fetchAPI('/settings/education-mode', { method: 'PUT', body: JSON.stringify({ enabled }) })

// ---- Language / i18n ----
export const getLanguage = () => fetchAPI('/settings/language')
export const setLanguage = (lang) => fetchAPI('/settings/language', { method: 'PUT', body: JSON.stringify({ language: lang }) })
