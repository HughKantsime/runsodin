// Cert F / cluster A — real Admin user table + real api client + real react-hot-toast <Toaster/>.
// Only the fetch boundary is substituted; responses are wire fixtures captured from the real backend
// by tests/test_contracts/test_cert_F_A_email_reset.py and test_cert_F_A_mfa.py. License context is
// substituted (license gating is not part of these claims).
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'react-hot-toast'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Admin from './Admin'

vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ hasFeature: () => false, atUserLimit: () => false, maxUsers: 0 }) }))

const WIRE = {
  users: [{"created_at":"2026-10-15 00:37:52","email":"certadmin@example.test","group_id":null,"id":1,"is_active":1,"last_login":null,"mfa_enabled":false,"role":"admin","username":"certadmin"},{"created_at":"2026-10-15 00:37:52","email":"reset_reader@example.test","group_id":null,"id":2,"is_active":1,"last_login":null,"mfa_enabled":false,"role":"viewer","username":"reset_reader"},{"created_at":"2026-10-15 00:37:53","email":"reset_operator@example.test","group_id":null,"id":3,"is_active":1,"last_login":null,"mfa_enabled":false,"role":"operator","username":"reset_operator"},{"created_at":"2026-10-15 00:37:53","email":"welcome_nosmtp@example.test","group_id":null,"id":4,"is_active":1,"last_login":null,"mfa_enabled":false,"role":"viewer","username":"welcome_nosmtp"},{"created_at":"2026-10-15 00:37:53","email":"welcome_one@example.test","group_id":null,"id":5,"is_active":1,"last_login":"2026-10-15 00:37:54.264568+00:00","mfa_enabled":false,"role":"viewer","username":"welcome_one"},{"created_at":"2026-10-15 00:37:54","email":"welcome_two@example.test","group_id":null,"id":6,"is_active":1,"last_login":null,"mfa_enabled":false,"role":"viewer","username":"welcome_two"},{"created_at":"2026-10-15 00:37:54","email":"welcome_three@example.test","group_id":null,"id":7,"is_active":1,"last_login":null,"mfa_enabled":false,"role":"viewer","username":"welcome_three"}],
  usersMfa: [{"created_at":"2026-10-15 00:39:00","email":"certadmin@example.test","group_id":null,"id":1,"is_active":1,"last_login":null,"mfa_enabled":false,"role":"admin","username":"certadmin"},{"created_at":"2026-10-15 00:39:00","email":"mfa_person@example.test","group_id":null,"id":2,"is_active":1,"last_login":"2026-10-15 00:39:02.449251+00:00","mfa_enabled":true,"role":"viewer","username":"mfa_person"},{"created_at":"2026-10-15 00:39:00","email":"mfa_operator@example.test","group_id":null,"id":3,"is_active":1,"last_login":"2026-10-15 00:39:03.427647+00:00","mfa_enabled":false,"role":"operator","username":"mfa_operator"}],
  resetEmail: {"status":"password_reset_email_sent"},
  createWelcome: {"status":"created","welcome_email":"sent"},
  mfaReset: {"message":"MFA disabled for mfa_person","status":"ok"},
}

type Call = { url: string; method: string; body: unknown }
let calls: Call[] = []
let list: unknown[] = []
let routes: Record<string, (body: unknown) => [number, unknown]> = {}
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

beforeEach(() => {
  calls = []; routes = {}
  // jsdom lacks matchMedia (used by react-hot-toast for reduced-motion)
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }))
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input), method = (init.method || 'GET').toUpperCase()
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : init.body
    calls.push({ url, method, body })
    const handler = routes[`${method} ${url}`]
    if (handler) { const [status, payload] = handler(body); return json(status, payload) }
    if (method === 'GET' && url === '/api/users') return json(200, list)
    return json(404, { detail: 'unexpected ' + method + ' ' + url })
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><Admin /><Toaster /></QueryClientProvider>)
}
const row = async (username: string) => (await screen.findByText(username)).closest('tr') as HTMLElement

it('F11-11.1-07 the user table sends a reset email for the chosen user', async () => {
  list = WIRE.users
  const target = (WIRE.users as any[]).find(u => u.username === 'reset_reader')
  routes = { [`POST /api/users/${target.id}/reset-password-email`]: () => [200, WIRE.resetEmail] }
  mount()
  const tr = await row('reset_reader')
  fireEvent.click(within(tr).getByTitle('Send password reset link'))
  expect(await screen.findByText('Password reset link sent. Their password has not changed.')).toBeInTheDocument() // toast (opacity animated in jsdom)
  const sent = calls.filter(c => c.url.endsWith('/reset-password-email'))
  expect(sent).toEqual([{ url: `/api/users/${target.id}/reset-password-email`, method: 'POST', body: undefined }])
  // every listed user with an email has the action
  for (const u of WIRE.users as any[]) expect(within(await row(u.username)).getByTitle('Send password reset link')).toBeVisible()
})

it('F11-11.1-07 a refused reset email is reported as failed', async () => {
  list = WIRE.users
  const target = (WIRE.users as any[]).find(u => u.username === 'reset_reader')
  routes = { [`POST /api/users/${target.id}/reset-password-email`]: () => [503, { detail: 'Password reset requires a valid application base URL.' }] }
  mount()
  fireEvent.click(within(await row('reset_reader')).getByTitle('Send password reset link'))
  expect(await screen.findByText('Unable to send a reset link. Check SMTP and application URL settings.')).toBeInTheDocument() // toast (opacity animated in jsdom)
})

it('F11-11.1-05 Add User with "Send welcome email" posts send_welcome_email and no password', async () => {
  list = WIRE.users
  routes = { 'POST /api/users': () => [200, WIRE.createWelcome] }
  mount()
  await row('reset_reader')
  fireEvent.click(screen.getByRole('button', { name: 'Add User' }))
  const field = (label: string) => screen.getByText(label).parentElement!.querySelector('input')!
  fireEvent.change(field('Username'), { target: { value: 'welcome_ui' } })
  fireEvent.change(field('Email'), { target: { value: 'welcome_ui@example.test' } })
  fireEvent.click(screen.getByLabelText('Send welcome email with auto-generated password'))
  expect(screen.queryByPlaceholderText('Min 8 characters')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Create User' }))
  await waitFor(() => expect(calls.some(c => c.method === 'POST' && c.url === '/api/users')).toBe(true))
  const post = calls.find(c => c.method === 'POST' && c.url === '/api/users')!
  expect(post.body).toMatchObject({ username: 'welcome_ui', email: 'welcome_ui@example.test', send_welcome_email: true, password: '' })
  await waitFor(() => expect(screen.queryByRole('heading', { name: 'New User' })).not.toBeInTheDocument())
  expect(screen.queryByText(/welcome email delivery was not confirmed/)).not.toBeInTheDocument()
})

it('F11-11.2-03 admin resets (force-disables) MFA for an enrolled user from the table', async () => {
  list = WIRE.usersMfa
  const target = (WIRE.usersMfa as any[]).find(u => u.mfa_enabled)
  routes = { [`DELETE /api/admin/users/${target.id}/mfa`]: () => { list = (WIRE.usersMfa as any[]).map(u => u.id === target.id ? { ...u, mfa_enabled: false } : u); return [200, WIRE.mfaReset] } }
  mount()
  fireEvent.click(await screen.findByRole('button', { name: `Reset MFA for ${target.username}` }))
  for (const u of (WIRE.usersMfa as any[]).filter(u => !u.mfa_enabled))
    expect(screen.queryByRole('button', { name: `Reset MFA for ${u.username}` })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Disable MFA' }))
  expect(await screen.findByText('MFA disabled. The user can enroll again in their account settings.')).toBeInTheDocument() // toast (opacity animated in jsdom)
  expect(calls.filter(c => c.method === 'DELETE')).toEqual([{ url: `/api/admin/users/${target.id}/mfa`, method: 'DELETE', body: undefined }])
  await waitFor(() => expect(screen.queryByRole('button', { name: `Reset MFA for ${target.username}` })).not.toBeInTheDocument())
  // there is no per-user control to force-enable MFA
  expect(screen.queryByRole('button', { name: /enable mfa|require mfa|enforce mfa/i })).not.toBeInTheDocument()
})
