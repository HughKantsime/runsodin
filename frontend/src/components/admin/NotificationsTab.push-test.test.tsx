// Settings > Notifications "Send test notification": POST /api/push/test as the signed-in admin, one plain-words
// line per registered phone. Not gated on browser push support (iOS Safari has none).
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import NotificationsTab from './NotificationsTab'

let role = 'admin'
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 1, role, group_id: null }) }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ tier: 'enterprise', isPro: true, hasFeature: () => true, isProPage: () => false }) }))
vi.mock('../../hooks/usePushNotifications', () => ({
  default: () => ({ isSupported: false, isSubscribed: false, permission: 'default', loading: false, subscribe: vi.fn(), unsubscribe: vi.fn() }),
}))

let pushTestBody: unknown
let pushTestStatus = 200
let posts: string[] = []
function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

beforeEach(() => {
  role = 'admin'
  posts = []
  pushTestStatus = 200
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    if (url === '/api/push/test' && method === 'POST') { posts.push(url); return json(pushTestBody, pushTestStatus) }
    if (url === '/api/alert-preferences') return json([])
    if (url === '/api/smtp-config') return json({ enabled: false, host: '', port: 587, username: '', password_set: false, from_address: '', use_tls: true, base_url: '' })
    return json({ detail: 'unexpected' }, 500)
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('sends the test and shows each phone result in plain words', async () => {
  pushTestBody = { sent: 1, total_devices: 2, devices: [
    { device_id: 4, status: 'sent', apns_id: 'A1', reason: null, token_fingerprint: 'c0ffee' },
    { device_id: 5, status: 'bad_device_token', apns_id: null, reason: 'BadDeviceToken', token_fingerprint: 'abc123' },
  ] }
  render(<NotificationsTab />)
  fireEvent.click(await screen.findByRole('button', { name: 'Send test notification' }))
  await waitFor(() => expect(posts).toEqual(['/api/push/test']))
  expect(await screen.findByText('Phone ending c0ffee: Sent. Check the phone.')).toBeInTheDocument()
  expect(screen.getByText(/Phone ending abc123: Not sent: Apple no longer accepts this phone.*\(BadDeviceToken\)/)).toBeInTheDocument()
})

it('says so when no phone is registered', async () => {
  pushTestBody = { sent: 0, total_devices: 0, devices: [] }
  render(<NotificationsTab />)
  fireEvent.click(await screen.findByRole('button', { name: 'Send test notification' }))
  expect(await screen.findByText(/No phones are registered to your account/)).toBeInTheDocument()
})

it('is hidden from non-admins, whom the endpoint refuses', async () => {
  role = 'operator'
  render(<NotificationsTab />)
  await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/alert-preferences', expect.anything()))
  expect(screen.queryByRole('button', { name: 'Send test notification' })).toBeNull()
})

it('shows the server refusal instead of a result', async () => {
  pushTestBody = { detail: 'Insufficient permissions' }
  pushTestStatus = 403
  render(<NotificationsTab />)
  fireEvent.click(await screen.findByRole('button', { name: 'Send test notification' }))
  expect(await screen.findByText(/^Test failed: /)).toBeInTheDocument()
})

it('describes a not-configured server and an unexpected failure without blaming Apple', async () => {
  pushTestBody = { sent: 0, total_devices: 2, devices: [
    { device_id: 4, status: 'not_configured', apns_id: null, reason: null, token_fingerprint: 'c0ffee' },
    { device_id: 7, status: 'other', apns_id: null, reason: null, token_fingerprint: '' },
  ] }
  render(<NotificationsTab />)
  fireEvent.click(await screen.findByRole('button', { name: 'Send test notification' }))
  expect(await screen.findByText('Phone ending c0ffee: Not sent: phone push is unavailable or not set up on this server.')).toBeInTheDocument()
  expect(screen.getByText('Phone 7: Not sent: the test could not be sent because of an unexpected error.')).toBeInTheDocument()
})
