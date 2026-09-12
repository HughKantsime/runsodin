/**
 * In-memory RBAC state.
 *
 * Identity and authorization documents are deliberately never persisted in
 * browser storage: school lab machines are frequently shared between users.
 * ProtectedRoute reloads this state from the HttpOnly session after every
 * document bootstrap. Until that succeeds, authorization fails closed.
 */

interface CachedUser {
  username: string
  role: string
}

interface PermissionsConfig {
  page_access?: Record<string, string[]>
  action_access?: Record<string, string[]>
}

const LEGACY_SENSITIVE_KEYS = [
  'odin_user',
  'rbac_permissions',
  'odin_token',
  'access_token',
  'refresh_token',
  'mfa_token',
  'reset_token',
]

let currentUser: CachedUser | null = null
let permissions: PermissionsConfig | null = null

export function purgeLegacySensitiveStorage(): void {
  if (typeof window === 'undefined') return
  for (const key of LEGACY_SENSITIVE_KEYS) {
    window.localStorage.removeItem(key)
    window.sessionStorage.removeItem(key)
  }
}

export async function clearSensitiveBrowserState(): Promise<void> {
  currentUser = null
  permissions = null
  purgeLegacySensitiveStorage()

  if (typeof window === 'undefined') return
  if ('caches' in window) {
    const names = await window.caches.keys()
    await Promise.all(names.map((name) => window.caches.delete(name)))
  }
  const indexedDBWithList = window.indexedDB as IDBFactory & {
    databases?: () => Promise<Array<{ name?: string }>>
  }
  if (indexedDBWithList?.databases) {
    const databases = await indexedDBWithList.databases()
    await Promise.all(databases.map(database => new Promise<void>((resolve) => {
      if (!database.name) return resolve()
      const request = indexedDBWithList.deleteDatabase(database.name)
      request.onsuccess = () => resolve()
      request.onerror = () => resolve()
      request.onblocked = () => resolve()
    })))
  }
}

export function getCurrentUser(): CachedUser | null {
  return currentUser
}

export function canAccessPage(page: string): boolean {
  if (!currentUser || !permissions?.page_access) return false
  return permissions.page_access[page]?.includes(currentUser.role) ?? false
}

export function canDo(action: string): boolean {
  if (!currentUser || !permissions?.action_access) return false
  return permissions.action_access[action]?.includes(currentUser.role) ?? false
}

export async function refreshPermissions(): Promise<PermissionsConfig | null> {
  purgeLegacySensitiveStorage()
  try {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    const [meRes, permRes] = await Promise.all([
      fetch('/api/auth/me', { headers, credentials: 'include' }),
      fetch('/api/permissions', { headers, credentials: 'include' }),
    ])
    if (!meRes.ok || !permRes.ok) {
      await clearSensitiveBrowserState()
      return null
    }
    const me = await meRes.json()
    const data = await permRes.json()
    currentUser = { username: me.username, role: me.role }
    permissions = data
    return data
  } catch {
    await clearSensitiveBrowserState()
    return null
  }
}

// Remove sensitive values written by releases before the in-memory model.
purgeLegacySensitiveStorage()
