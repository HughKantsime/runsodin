export const PAGE_ACCESS = {
  dashboard:  ['admin', 'operator', 'viewer'],
  timeline:   ['admin', 'operator', 'viewer'],
  jobs:       ['admin', 'operator', 'viewer'],
  printers:   ['admin', 'operator', 'viewer'],
  models:     ['admin', 'operator', 'viewer'],
  spools:     ['admin', 'operator', 'viewer'],
  cameras:    ['admin', 'operator', 'viewer'],
  analytics:  ['admin', 'operator', 'viewer'],
  calculator: ['admin', 'operator', 'viewer'],
  upload:     ['admin', 'operator'],
  settings:   ['admin'],
  admin:      ['admin'],
}

export const ACTION_ACCESS = {
  'jobs.create':       ['admin', 'operator'],
  'jobs.edit':         ['admin', 'operator'],
  'jobs.cancel':       ['admin', 'operator'],
  'jobs.delete':       ['admin', 'operator'],
  'jobs.start':        ['admin', 'operator'],
  'jobs.complete':     ['admin', 'operator'],
  'printers.add':      ['admin'],
  'printers.edit':     ['admin', 'operator'],
  'printers.delete':   ['admin'],
  'printers.slots':    ['admin', 'operator'],
  'printers.reorder':  ['admin', 'operator'],
  'models.create':     ['admin', 'operator'],
  'models.edit':       ['admin', 'operator'],
  'models.delete':     ['admin'],
  'spools.edit':       ['admin', 'operator'],
  'spools.delete':     ['admin'],
  'timeline.move':     ['admin', 'operator'],
  'upload.upload':     ['admin', 'operator'],
  'upload.schedule':   ['admin', 'operator'],
  'upload.delete':     ['admin', 'operator'],
  'dashboard.actions': ['admin', 'operator'],
}

export function getCurrentUser() {
  const token = localStorage.getItem('token')
  if (!token) return null
  try {
    const payload = JSON.parse(atob(token.split('.')[1]))
    if (payload.exp * 1000 < Date.now()) return null
    return { username: payload.sub, role: payload.role || 'viewer' }
  } catch {
    return null
  }
}

export function canAccessPage(page) {
  const user = getCurrentUser()
  if (!user) return false
  const allowed = PAGE_ACCESS[page]
  return allowed ? allowed.includes(user.role) : false
}

export function canDo(action) {
  const user = getCurrentUser()
  if (!user) return false
  const allowed = ACTION_ACCESS[action]
  return allowed ? allowed.includes(user.role) : false
}
