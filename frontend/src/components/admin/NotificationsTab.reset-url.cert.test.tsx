// Cert F / cluster A — F11-11.1-06/07: the application URL used in password-reset links is set in
// Settings > Notifications (SMTP) and saved through PUT /api/smtp-config (backend half:
// tests/test_contracts/test_cert_F_A_email_reset.py). Real component + real api client; only window.fetch
// is replaced, answering with the GET /api/smtp-config response shape of the real backend.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import NotificationsTab from './NotificationsTab'

vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ tier: 'enterprise', isPro: true, hasFeature: () => true, isProPage: () => false }) }))
// Browser push is a device boundary.
vi.mock('../../hooks/usePushNotifications', () => ({
  default: () => ({ isSupported: false, isSubscribed: false, permission: 'default', loading: false, subscribe: vi.fn(), unsubscribe: vi.fn() }),
}))

const SMTP_GET = { enabled: true, host: '127.0.0.1', port: 2525, username: '', password_set: false, from_address: 'odin@stub.smtp.test', use_tls: false, base_url: '' }

let calls: { url: string; method: string; body: any }[] = []
let stored: typeof SMTP_GET

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

beforeEach(() => {
  calls = []
  stored = { ...SMTP_GET }
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    const body = init.body ? JSON.parse(String(init.body)) : undefined
    calls.push({ url, method, body })
    if (url === '/api/alert-preferences' && method === 'GET') return json([])
    if (url === '/api/smtp-config' && method === 'GET') return json(stored)
    if (url === '/api/smtp-config' && method === 'PUT') {
      if (body.base_url && !/^https?:\/\/[^/?#\s@]+/.test(body.base_url)) {
        return json({ detail: 'Application URL must be an http(s) URL such as https://odin.example.com (no credentials, query or fragment).' }, 400)
      }
      stored = { ...stored, base_url: body.base_url ?? stored.base_url }
      return json({ status: 'ok', message: 'SMTP configuration updated' })
    }
    return json({ detail: 'unexpected' }, 500)
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('F11-11.1-06 suggests this page origin as the application URL when none is stored and saves it', async () => {
  render(<NotificationsTab />)
  const field = await screen.findByLabelText('Application URL (used in password-reset links)')
  expect(field).toHaveValue(window.location.origin)
  fireEvent.change(field, { target: { value: 'https://odin.example.test' } })
  fireEvent.click(screen.getByRole('button', { name: /Save SMTP/ }))
  await waitFor(() => expect(calls.some(c => c.method === 'PUT')).toBe(true))
  const put = calls.find(c => c.method === 'PUT')!
  expect(put.url).toBe('/api/smtp-config')
  expect(put.body).toMatchObject({ enabled: true, host: '127.0.0.1', base_url: 'https://odin.example.test' })
})

it('F11-11.1-06 shows a stored application URL and the server refusal of an invalid one', async () => {
  stored = { ...SMTP_GET, base_url: 'https://odin.example.test' }
  render(<NotificationsTab />)
  const field = await screen.findByLabelText('Application URL (used in password-reset links)')
  expect(field).toHaveValue('https://odin.example.test')
  fireEvent.change(field, { target: { value: 'odin' } })
  fireEvent.click(screen.getByRole('button', { name: /Save SMTP/ }))
  expect(await screen.findByText(/Failed to save SMTP configuration: Application URL must be an http\(s\) URL/)).toBeInTheDocument()
})
