// Cert-E F13-13.2-01 (per-org quiet hours UI), F13-13.1-03/04/05/06/07 (webhook UI),
// F13-13.1-02 (SMTP UI): real components + real API client replaying responses
// captured from the real backend (tests/test_contracts/test_cert_E_notif_wire.py).
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import OrgManager from './OrgManager'
import WebhookSettings from './WebhookSettings'
import NotificationsTab from './NotificationsTab'
import orgFixture from '../../test-fixtures/cert-E-notif-org-settings.json'
import adminFixture from '../../test-fixtures/cert-E-notif-admin.json'

vi.mock('../../permissions', async (importOriginal) => ({ ...(await importOriginal<any>()), canDo: () => true }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, hasFeature: () => true }) }))
vi.mock('../../hooks/usePushNotifications', () => ({ default: () => ({ isSupported: false, isSubscribed: false, permission: 'default', loading: false, subscribe: vi.fn(), unsubscribe: vi.fn() }) }))

type Call = { method: string; path: string; body: any }
let calls: Call[] = []
function wire(routes: Record<string, unknown>) {
  calls = []
  globalThis.fetch = vi.fn(async (input: any, init: any = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    const path = String(input).replace(/^\/api/, '')
    calls.push({ method, path, body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined })
    const body = routes[`${method} ${path}`]
    return new Response(JSON.stringify(body ?? { detail: 'unmapped' }), { status: body === undefined ? 500 : 200, headers: { 'Content-Type': 'application/json' } })
  }) as any
}
const withQuery = (node: React.ReactNode) => <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>
afterEach(() => { cleanup(); vi.clearAllMocks() })

// ---------------- Per-org quiet hours ----------------

test('CertENotif: org settings show the stored quiet-hours window and save an edited window', async () => {
  wire({ 'GET /orgs': orgFixture.orgs, 'GET /users': [], 'GET /printers': [], 'GET /orgs/1/settings': orgFixture.settings,
         'PUT /orgs/1/settings': orgFixture.put_response })
  render(withQuery(<OrgManager />))
  const card = (await screen.findByText('school')).closest('.rounded-md.p-4') as HTMLElement
  fireEvent.click(within(card).getByRole('button', { name: 'Organization settings' }))
  const toggle = await within(card).findByRole('checkbox', { name: 'Quiet hours' })
  await waitFor(() => expect(toggle).toBeChecked())
  expect(within(card).getByDisplayValue('21:30')).toBeInTheDocument()
  fireEvent.change(within(card).getByDisplayValue('06:45'), { target: { value: '07:15' } })
  fireEvent.click(within(card).getByRole('button', { name: 'Save Settings' }))
  await waitFor(() => expect(calls.find(c => c.method === 'PUT')).toBeTruthy())
  const put = calls.find(c => c.method === 'PUT')!
  expect(put.path).toBe('/orgs/1/settings')
  expect(put.body).toMatchObject({ quiet_hours_enabled: true, quiet_hours_start: '21:30', quiet_hours_end: '07:15',
                                   webhook_url: 'https://hooks.cert-e.test/org-1', webhook_type: 'discord' })
  // The sibling org's settings were never touched.
  expect(calls.some(c => c.path.startsWith('/orgs/2'))).toBe(false)
})

// ---------------- Webhooks UI ----------------

function webhookWire() {
  wire({ 'GET /webhooks': adminFixture.webhooks, 'POST /webhooks': { success: true, message: 'Webhook created' } })
}

test('CertENotif: configured Discord webhook from the real list renders with type and events', async () => {
  webhookWire()
  render(<WebhookSettings />)
  expect(await screen.findByText('Synthetic Discord')).toBeInTheDocument()
  expect(screen.getByText('discord')).toBeInTheDocument()
  expect(screen.getByText('Enabled')).toBeInTheDocument()
})

test('CertENotif: Add Webhook creates a Discord webhook via POST /webhooks', async () => {
  webhookWire()
  render(<WebhookSettings />)
  fireEvent.click(await screen.findByRole('button', { name: /Add Webhook/ }))
  fireEvent.change(screen.getByPlaceholderText('https://discord.com/api/webhooks/...'), { target: { value: 'https://discord.cert-e.test/api/webhooks/2/y' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create Webhook' }))
  await waitFor(() => expect(calls.find(c => c.method === 'POST')).toBeTruthy())
  expect(calls.find(c => c.method === 'POST')!.body).toMatchObject({ url: 'https://discord.cert-e.test/api/webhooks/2/y', webhook_type: 'discord' })
})

// DEFECT: frontend/src/components/admin/WebhookSettings.tsx:18-24 event ids are uppercase ('PRINT_FAILED', 'HMS_ERROR');
// backend/modules/notifications/event_selection.py:31 matches dispatched lowercase types case-sensitively.
test.fails('CertENotif: selecting "Print Failed" submits the canonical dispatched event id', async () => {
  webhookWire()
  render(<WebhookSettings />)
  fireEvent.click(await screen.findByRole('button', { name: /Add Webhook/ }))
  fireEvent.change(screen.getByPlaceholderText('https://discord.com/api/webhooks/...'), { target: { value: 'https://discord.cert-e.test/x' } })
  fireEvent.click(screen.getByRole('button', { name: 'Print Failed' }))
  fireEvent.click(screen.getByRole('button', { name: 'Create Webhook' }))
  await waitFor(() => expect(calls.find(c => c.method === 'POST')).toBeTruthy())
  expect(calls.find(c => c.method === 'POST')!.body.alert_types).toEqual(['print_failed'])
})

// DEFECT: frontend/src/components/admin/WebhookSettings.tsx:162-163 offers only Discord and Slack, although the
// panel advertises "Discord, Slack, ntfy, or Telegram" and the backend dispatches ntfy/telegram/pushover/whatsapp.
test.fails('CertENotif: webhook type selector offers every advertised channel', async () => {
  webhookWire()
  render(<WebhookSettings />)
  fireEvent.click(await screen.findByRole('button', { name: /Add Webhook/ }))
  const options = within(screen.getAllByRole('combobox')[0]).getAllByRole('option').map(o => (o as HTMLOptionElement).value)
  expect(options).toEqual(expect.arrayContaining(['discord', 'slack', 'ntfy', 'telegram', 'pushover', 'whatsapp']))
})

// ---------------- SMTP UI ----------------

test('CertENotif: SMTP card shows the stored configuration and saves without resending the password', async () => {
  wire({ 'GET /alert-preferences': [], 'GET /smtp-config': adminFixture.smtp_config,
         'PUT /smtp-config': { status: 'ok', message: 'SMTP configuration updated' } })
  render(<NotificationsTab />)
  const host = await screen.findByLabelText('SMTP Host')
  await waitFor(() => expect(host).toHaveValue('smtp.cert-e.test'))
  expect(screen.getByLabelText('Port')).toHaveValue(2525)
  expect(screen.getByPlaceholderText(/\(saved\)$/)).toBeInTheDocument()
  fireEvent.change(host, { target: { value: 'mail.cert-e.test' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save SMTP' }))
  await waitFor(() => expect(calls.find(c => c.method === 'PUT')).toBeTruthy())
  const put = calls.find(c => c.method === 'PUT')!
  expect(put.path).toBe('/smtp-config')
  expect(put.body).toEqual({ enabled: true, host: 'mail.cert-e.test', port: 2525, username: 'odin', from_address: 'odin@example.test' })
})
