// Review H1: superadmin-only push key rotation action in Settings > Notifications.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import NotificationsTab from './NotificationsTab'
import { getCurrentUser } from '../../permissions'

vi.mock('../../permissions', async (importOriginal) => ({ ...(await importOriginal<any>()), canDo: () => true, getCurrentUser: vi.fn() }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, hasFeature: () => true }) }))
const push = vi.hoisted(() => ({ subscribe: vi.fn() }))
vi.mock('../../hooks/usePushNotifications', () => ({ default: () => ({ isSupported: true, isSubscribed: true, permission: 'granted', loading: false, subscribe: push.subscribe, unsubscribe: vi.fn() }) }))

let calls: { method: string; path: string }[] = []
beforeEach(() => {
  calls = []
  globalThis.fetch = vi.fn(async (input: any, init: any = {}) => {
    const method = (init.method || 'GET').toUpperCase(); const path = String(input).replace(/^\/api/, '')
    calls.push({ method, path })
    const body = path === '/push/vapid-key/rotate' ? { status: 'ok', public_key: 'BNew', subscriptions_invalidated: 3 }
      : path === '/alert-preferences' ? [] : path === '/smtp-config' ? { enabled: false } : {}
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }) as any
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

test('superadmin rotates the push keys and this browser re-registers', async () => {
  vi.mocked(getCurrentUser).mockReturnValue({ id: 3, username: 'admin', role: 'admin', group_id: null } as any)
  render(<NotificationsTab />)
  fireEvent.click(await screen.findByRole('button', { name: 'Rotate push keys' }))
  await waitFor(() => expect(calls).toContainEqual({ method: 'POST', path: '/push/vapid-key/rotate' }))
  await waitFor(() => expect(push.subscribe).toHaveBeenCalledTimes(1))
})

test.each([[{ role: 'admin', group_id: 1 }], [{ role: 'operator', group_id: null }]])('no rotation action for %o', async (who) => {
  vi.mocked(getCurrentUser).mockReturnValue({ id: 4, username: 'x', ...who } as any)
  render(<NotificationsTab />)
  await screen.findByText('Browser Push Notifications')
  expect(screen.queryByRole('button', { name: 'Rotate push keys' })).toBeNull()
})
