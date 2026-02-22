/**
 * RBAC Permissions — reads from cached server config, falls back to hardcoded defaults.
 * On login, the app fetches /api/permissions and stores in localStorage.
 * This module reads that cache. If missing (upgrade, first boot), defaults kick in.
 */

const DEFAULT_PAGE_ACCESS = {
  dashboard:   ['admin', 'operator', 'viewer'],
  timeline:    ['admin', 'operator', 'viewer'],
  jobs:        ['admin', 'operator', 'viewer'],
  printers:    ['admin', 'operator', 'viewer'],
  models:      ['admin', 'operator', 'viewer'],
  spools:      ['admin', 'operator', 'viewer'],
  cameras:     ['admin', 'operator', 'viewer'],
  analytics:   ['admin', 'operator', 'viewer'],
  calculator:  ['admin', 'operator', 'viewer'],
  upload:      ['admin', 'operator'],
  maintenance: ['admin', 'operator'],
  settings:    ['admin'],
  admin:       ['admin'],
  branding:    ['admin'],
  audit:              ['admin'],
  orders:             ['admin', 'operator', 'viewer'],
  products:            ['admin', 'operator', 'viewer'],
  alerts:              ['admin', 'operator', 'viewer'],
  education_reports:   ['admin', 'operator'],
}

const DEFAULT_ACTION_ACCESS = {
  'jobs.create':       ['admin', 'operator'],
  'jobs.edit':         ['admin', 'operator'],
  'jobs.cancel':       ['admin', 'operator'],
  'jobs.delete':       ['admin', 'operator'],
  'jobs.start':        ['admin', 'operator'],
  'jobs.complete':     ['admin', 'operator'],
  'printers.add':      ['admin'],
  'printers.edit':     ['admin', 'operator'],
  'printers.delete':   ['admin', 'operator'],
  'printers.slots':    ['admin', 'operator'],
  'printers.reorder':  ['admin', 'operator'],
  'models.create':     ['admin', 'operator'],
  'models.edit':       ['admin', 'operator'],
  'models.delete':     ['admin', 'operator'],
  'spools.edit':       ['admin', 'operator'],
  'spools.delete':     ['admin', 'operator'],
  'timeline.move':     ['admin', 'operator'],
  'upload.upload':     ['admin', 'operator'],
  'upload.schedule':   ['admin', 'operator'],
  'upload.delete':     ['admin', 'operator'],
  'maintenance.log':   ['admin', 'operator'],
  'maintenance.tasks': ['admin'],
  'dashboard.actions': ['admin', 'operator'],
  'orders.create':      ['admin', 'operator'],
  'orders.edit':        ['admin'],
  'orders.delete':      ['admin', 'operator'],
  'orders.ship':        ['admin', 'operator'],
  'products.create':    ['admin', 'operator'],
  'products.edit':      ['admin', 'operator'],
  'products.delete':    ['admin'],
  'jobs.approve':       ['admin', 'operator'],
  'jobs.reject':        ['admin', 'operator'],
  'jobs.resubmit':      ['admin', 'operator', 'viewer'],
  'alerts.read':        ['admin', 'operator', 'viewer'],
  'printers.plug':      ['admin', 'operator'],
}

function getCachedPermissions() {
  try {
    const raw = localStorage.getItem('rbac_permissions')
    if (raw) return JSON.parse(raw)
  } catch {}
  return null
}

function getPageAccess() {
  const cached = getCachedPermissions()
  return cached?.page_access || DEFAULT_PAGE_ACCESS
}

function getActionAccess() {
  const cached = getCachedPermissions()
  return cached?.action_access || DEFAULT_ACTION_ACCESS
}

export function getCurrentUser() {
  // With httpOnly cookie auth, we can't decode the JWT from JS.
  // User info (username, role) is cached in localStorage by refreshPermissions().
  // The token itself is never accessible to JS — only the cookie-secured session.
  try {
    const raw = localStorage.getItem('odin_user')
    if (raw) return JSON.parse(raw)
  } catch {}
  return null
}

export function canAccessPage(page) {
  const user = getCurrentUser()
  if (!user) return false
  const access = getPageAccess()
  const allowed = access[page]
  return allowed ? allowed.includes(user.role) : false
}

export function canDo(action) {
  const user = getCurrentUser()
  if (!user) return false
  const access = getActionAccess()
  const allowed = access[action]
  return allowed ? allowed.includes(user.role) : false
}

/**
 * Call this after login to cache server permissions.
 * Also call after admin saves permission changes.
 */
export async function refreshPermissions() {
  try {
    const headers = { 'Content-Type': 'application/json' }

    // Fetch user info and permissions — session cookie sent automatically
    const [meRes, permRes] = await Promise.all([
      fetch('/api/auth/me', { headers, credentials: 'include' }),
      fetch('/api/permissions', { headers, credentials: 'include' }),
    ])

    if (meRes.ok) {
      const me = await meRes.json()
      localStorage.setItem('odin_user', JSON.stringify({ username: me.username, role: me.role }))
    }

    if (permRes.ok) {
      const data = await permRes.json()
      localStorage.setItem('rbac_permissions', JSON.stringify(data))
      return data
    }
  } catch {}
  return null
}
