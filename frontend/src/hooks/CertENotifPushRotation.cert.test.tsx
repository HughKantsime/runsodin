// Review H1: after a server VAPID rotation the hook replaces a subscription bound to the old key.
import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import usePushNotifications from './usePushNotifications'

const b64url = (bytes: number[]) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const point = (seed: number) => [4, ...Array.from({ length: 64 }, (_, i) => (i * seed + 3) % 256)]
const OLD = b64url(point(31)), NEW = b64url(point(57))
const toBuffer = (key: string) => Uint8Array.from(atob(key.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - key.length % 4) % 4)), c => c.charCodeAt(0)).buffer
let posted: any[] = []; let unsubscribed = 0; let subscribedWith: string | null = null; let existing: any

function install(serverKey: string, existingKey: string | null, permission = 'granted') {
  posted = []; unsubscribed = 0; subscribedWith = null
  existing = existingKey === undefined ? null : {
    endpoint: 'https://push.cert-e.test/sub/old',
    options: existingKey ? { applicationServerKey: toBuffer(existingKey) } : {},
    unsubscribe: async () => { unsubscribed += 1; return true },
  }
  const fresh = { endpoint: 'https://push.cert-e.test/sub/new', toJSON: () => ({ endpoint: 'https://push.cert-e.test/sub/new', keys: { p256dh: NEW, auth: 'synthetic-auth' } }) }
  const registration = { pushManager: { getSubscription: async () => existing, subscribe: async (o: any) => { subscribedWith = b64url(Array.from(new Uint8Array(o.applicationServerKey))); return fresh } } }
  Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: { ready: Promise.resolve(registration), getRegistration: async () => registration } })
  ;(window as any).PushManager = class {}
  ;(window as any).Notification = { permission, requestPermission: async () => permission }
  globalThis.fetch = vi.fn(async (input: any, init: any = {}) => {
    if (String(input) === '/api/push/vapid-key') return new Response(JSON.stringify({ public_key: serverKey, enabled: true }))
    posted.push(JSON.parse(init.body)); return new Response(JSON.stringify({ status: 'ok' }))
  }) as any
}
beforeEach(() => { try { window.localStorage.clear() } catch { /* */ } })
afterEach(() => { vi.restoreAllMocks() })

test('subscription bound to a rotated-out key is replaced with one for the new key', async () => {
  install(NEW, OLD)
  const { result } = renderHook(() => usePushNotifications())
  await waitFor(() => expect(result.current.loading).toBe(false))
  expect(unsubscribed).toBe(1)
  expect(subscribedWith).toBe(NEW)
  expect(posted).toEqual([{ endpoint: 'https://push.cert-e.test/sub/new', p256dh_key: NEW, auth_key: 'synthetic-auth' }])
  expect(window.localStorage.getItem('odin.push.vapidPublicKey')).toBe(NEW)
  expect(result.current.isSubscribed).toBe(true)
})

test('cached key is used when the browser does not expose applicationServerKey', async () => {
  window.localStorage.setItem('odin.push.vapidPublicKey', OLD)
  install(NEW, null)
  const { result } = renderHook(() => usePushNotifications())
  await waitFor(() => expect(result.current.loading).toBe(false))
  expect(unsubscribed).toBe(1); expect(subscribedWith).toBe(NEW)
})

test('matching key keeps the subscription untouched', async () => {
  install(NEW, NEW)
  const { result } = renderHook(() => usePushNotifications())
  await waitFor(() => expect(result.current.loading).toBe(false))
  expect(unsubscribed).toBe(0); expect(subscribedWith).toBeNull(); expect(posted).toEqual([])
  expect(result.current.isSubscribed).toBe(true)
})

test('without granted permission the stale subscription is dropped, not silently kept', async () => {
  install(NEW, OLD, 'default')
  const { result } = renderHook(() => usePushNotifications())
  await waitFor(() => expect(result.current.loading).toBe(false))
  expect(unsubscribed).toBe(1); expect(subscribedWith).toBeNull()
  expect(result.current.isSubscribed).toBe(false)
})
