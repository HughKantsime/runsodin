import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  canAccessPage,
  canDo,
  clearSensitiveBrowserState,
  getCurrentUser,
  purgeLegacySensitiveStorage,
  refreshPermissions,
} from './permissions'


function response(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response
}


describe('in-memory permission state', () => {
  beforeEach(async () => {
    window.localStorage.clear()
    window.sessionStorage.clear()
    vi.restoreAllMocks()
    await clearSensitiveBrowserState()
  })

  it('purges legacy identity, permission, and token storage at bootstrap', () => {
    for (const key of ['odin_user', 'rbac_permissions', 'odin_token', 'access_token', 'mfa_token']) {
      window.localStorage.setItem(key, 'sensitive')
      window.sessionStorage.setItem(key, 'sensitive')
    }
    window.localStorage.setItem('odin-theme', 'dark')

    purgeLegacySensitiveStorage()

    expect(window.localStorage.getItem('odin-theme')).toBe('dark')
    expect(window.localStorage.getItem('odin_user')).toBeNull()
    expect(window.localStorage.getItem('rbac_permissions')).toBeNull()
    expect(window.sessionStorage.getItem('access_token')).toBeNull()
  })

  it('fails closed until server identity and permissions load into memory', async () => {
    expect(getCurrentUser()).toBeNull()
    expect(canAccessPage('settings')).toBe(false)
    expect(canDo('jobs.approve')).toBe(false)

    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(response({ username: 'admin@school.test', role: 'admin' }))
      .mockResolvedValueOnce(response({
        page_access: { settings: ['admin'] },
        action_access: { 'jobs.approve': ['admin', 'operator'] },
      })))

    expect(await refreshPermissions()).not.toBeNull()
    expect(getCurrentUser()).toEqual({ username: 'admin@school.test', role: 'admin' })
    expect(canAccessPage('settings')).toBe(true)
    expect(canDo('jobs.approve')).toBe(true)
    expect(window.localStorage.getItem('odin_user')).toBeNull()
    expect(window.localStorage.getItem('rbac_permissions')).toBeNull()
  })

  it('clears in-memory authorization and preserves non-sensitive preferences', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(response({ username: 'student@school.test', role: 'viewer' }))
      .mockResolvedValueOnce(response({ page_access: { jobs: ['viewer'] }, action_access: {} })))
    await refreshPermissions()
    window.localStorage.setItem('odin-theme', 'light')
    window.localStorage.setItem('odin_user', 'legacy')

    await clearSensitiveBrowserState()

    expect(getCurrentUser()).toBeNull()
    expect(canAccessPage('jobs')).toBe(false)
    expect(window.localStorage.getItem('odin_user')).toBeNull()
    expect(window.localStorage.getItem('odin-theme')).toBe('light')
  })
})
