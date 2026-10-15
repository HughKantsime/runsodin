// Cert-E F13-13.1-01: the real usePushNotifications hook against stubbed browser
// Push APIs (service worker / PushManager = device OS boundary). The server
// contract is backend/modules/notifications/schemas.py PushSubscriptionCreate
// {endpoint, p256dh_key, auth_key}, exercised in tests/test_contracts/test_cert_E_notif_push.py.
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import usePushNotifications from './usePushNotifications'

// Synthetic key material built at runtime (no credential-shaped literals in source):
// a 65-byte uncompressed P-256 point shape (0x04 || 64 bytes) and a 16-byte auth secret.
const b64url = (bytes: number[]) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const KEY = b64url([4, ...Array.from({ length: 64 }, (_, i) => (i * 37 + 11) % 256)])
const AUTH = b64url(Array.from({ length: 16 }, (_, i) => (i * 53 + 7) % 256))
let posted: any[] = []
let subscribeOptions: any = null

beforeEach(() => {
  posted = []; subscribeOptions = null
  const subscription = { endpoint: 'https://push.cert-e.test/sub/1', toJSON: () => ({ endpoint: 'https://push.cert-e.test/sub/1', expirationTime: null, keys: { p256dh: KEY, auth: AUTH } }) }
  const registration = { pushManager: { getSubscription: async () => null, subscribe: async (opts: any) => { subscribeOptions = opts; return subscription } } }
  Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: { ready: Promise.resolve(registration), getRegistration: async () => registration, register: async () => registration } })
  ;(window as any).PushManager = class {}
  ;(window as any).Notification = { permission: 'default', requestPermission: async () => 'granted' }
  globalThis.fetch = vi.fn(async (input: any, init: any = {}) => {
    if (String(input) === '/api/push/vapid-key') return new Response(JSON.stringify({ public_key: KEY, enabled: true }))
    posted.push(JSON.parse(init.body))
    return new Response(JSON.stringify({ status: 'ok' }))
  }) as any
})
afterEach(() => { vi.restoreAllMocks() })

test('CertENotif: subscribe uses the server VAPID public key as applicationServerKey', async () => {
  const { result } = renderHook(() => usePushNotifications())
  await waitFor(() => expect(result.current.loading).toBe(false))
  await act(async () => { await result.current.subscribe() })
  expect(Array.from(subscribeOptions.applicationServerKey as Uint8Array)).toHaveLength(65)
  expect(subscribeOptions.userVisibleOnly).toBe(true)
  expect(posted).toHaveLength(1)
})

// DEFECT: frontend/src/hooks/usePushNotifications.ts:96 posts PushSubscription.toJSON() ({keys:{p256dh,auth}});
// backend/modules/notifications/schemas.py:93-96 requires top-level p256dh_key/auth_key (422).
test.fails('CertENotif: subscription body matches the backend PushSubscriptionCreate contract', async () => {
  const { result } = renderHook(() => usePushNotifications())
  await waitFor(() => expect(result.current.loading).toBe(false))
  await act(async () => { await result.current.subscribe() })
  expect(posted[0]).toEqual({ endpoint: 'https://push.cert-e.test/sub/1', p256dh_key: KEY, auth_key: AUTH })
})
