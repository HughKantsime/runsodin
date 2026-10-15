// Cert F / cluster A — real Login + LoginContinuation + api client, fetch boundary only.
// Wire fixtures below were captured from the real backend (LiveOdin) by
// tests/test_contracts/test_cert_F_A_*.py (CERT_F_A_FIXTURES); JWT values are redacted there and
// replaced here by synthetic unsigned tokens of the same claim shape.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Login from './Login'

const WIRE = {
  capabilitiesDisabled: {"smtp_enabled":false},
  capabilitiesEnabled: {"smtp_enabled":true},
  loginFull: {"access_token":"<jwt>","token_type":"bearer"},
  loginMfaRequired: {"access_token":"<pending-jwt>","mfa_required":true,"token_type":"bearer"},
  mfaVerify: {"access_token":"<jwt>","token_type":"bearer"},
  forgot: {"message":"If that email is registered, a reset link has been sent.","status":"ok"},
  me: {"email":"transport_viewer@example.test","group_id":null,"id":2,"role":"viewer","username":"transport_viewer"},
  permissions: {"action_access":{"alerts.read":["admin","operator","viewer"],"dashboard.actions":["admin","operator"],"jobs.approve":["admin","operator"],"jobs.cancel":["admin","operator"],"jobs.complete":["admin","operator"],"jobs.create":["admin","operator"],"jobs.delete":["admin","operator"],"jobs.edit":["admin","operator"],"jobs.reject":["admin","operator"],"jobs.resubmit":["admin","operator","viewer"],"jobs.start":["admin","operator"],"maintenance.log":["admin","operator"],"maintenance.tasks":["admin"],"models.create":["admin","operator"],"models.delete":["admin"],"models.edit":["admin","operator"],"orders.create":["admin","operator"],"orders.delete":["admin","operator"],"orders.edit":["admin"],"orders.ship":["admin","operator"],"printers.add":["admin"],"printers.delete":["admin"],"printers.edit":["admin","operator"],"printers.plug":["admin","operator"],"printers.reorder":["admin","operator"],"printers.slots":["admin","operator"],"products.create":["admin","operator"],"products.delete":["admin"],"products.edit":["admin","operator"],"settings.edit":["admin"],"spools.delete":["admin"],"spools.edit":["admin","operator"],"timeline.move":["admin","operator"],"upload.delete":["admin","operator"],"upload.schedule":["admin","operator"],"upload.upload":["admin","operator"]},"page_access":{"admin":["admin"],"alerts":["admin","operator","viewer"],"analytics":["admin","operator","viewer"],"branding":["admin"],"calculator":["admin","operator","viewer"],"cameras":["admin","operator","viewer"],"dashboard":["admin","operator","viewer"],"education":["admin","operator","viewer"],"education_reports":["admin","operator"],"jobs":["admin","operator","viewer"],"maintenance":["admin","operator"],"models":["admin","operator","viewer"],"orders":["admin","operator","viewer"],"printers":["admin","operator","viewer"],"products":["admin","operator","viewer"],"settings":["admin"],"spools":["admin","operator","viewer"],"timeline":["admin","operator","viewer"],"upload":["admin","operator"]}},
}

function b64(value: object) { return btoa(JSON.stringify(value)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_') }
function jwt(claims: object) { return `${b64({ alg: 'HS256', typ: 'JWT' })}.${b64(claims)}.synthetic-signature` }
const exp = () => Math.floor(Date.now() / 1000) + 600
const fullToken = () => jwt({ sub: WIRE.me.username, role: WIRE.me.role, exp: exp(), jti: 'synthetic-jti' })
const pendingToken = () => jwt({ sub: WIRE.me.username, role: WIRE.me.role, mfa_pending: true, exp: exp(), jti: 'synthetic-pending' })

type Call = { url: string; method: string; body: unknown; credentials?: string }
let calls: Call[] = []
let routes: Record<string, (body: unknown) => [number, unknown]> = {}
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

beforeEach(() => {
  calls = []
  vi.stubGlobal('__APP_VERSION__', 'cert')
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input)
    const method = (init.method || 'GET').toUpperCase()
    let body: unknown = init.body
    if (init.body instanceof URLSearchParams) body = Object.fromEntries(init.body)
    else if (typeof init.body === 'string') body = JSON.parse(init.body)
    calls.push({ url, method, body, credentials: init.credentials })
    const handler = routes[`${method} ${url}`]
    if (handler) { const [status, payload] = handler(body); return json(status, payload) }
    if (url === '/api/auth/oidc/config') return json(200, { enabled: false })
    if (url === '/api/auth/me') return json(200, WIRE.me)
    if (url === '/api/permissions') return json(200, WIRE.permissions)
    return json(404, { detail: 'unexpected ' + method + ' ' + url })
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })

function mount() {
  render(<MemoryRouter initialEntries={['/login']}><Routes>
    <Route path="/login" element={<Login />} />
    <Route path="/" element={<p>Signed-in home</p>} />
  </Routes></MemoryRouter>)
}
async function signIn() {
  fireEvent.change(await screen.findByLabelText('Username'), { target: { value: WIRE.me.username } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: crypto.randomUUID() + 'aA1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign In' }))
}
function storageValues() {
  const all: string[] = []
  for (const store of [localStorage, sessionStorage]) for (let i = 0; i < store.length; i++) all.push(store.getItem(store.key(i)!) || '')
  return all.join('\n')
}

it('F11-11.1-01 password sign-in relies on the session cookie and never persists the body JWT', async () => {
  const token = fullToken()
  routes = { 'GET /api/auth/capabilities': () => [200, WIRE.capabilitiesDisabled],
    'POST /api/auth/login': () => [200, { ...WIRE.loginFull, access_token: token }] }
  mount(); await signIn()
  await screen.findByText('Signed-in home')
  const login = calls.find(c => c.url === '/api/auth/login')!
  expect(login.method).toBe('POST')
  expect(login.body).toMatchObject({ username: WIRE.me.username })
  // identity confirmed through the cookie-carrying /auth/me call, not through a stored token
  const me = calls.filter(c => c.url === '/api/auth/me')
  expect(me.length).toBeGreaterThan(0)
  expect(me.every(c => c.credentials === 'include')).toBe(true)
  expect(calls.some(c => JSON.stringify(c).includes('Bearer'))).toBe(false)
  expect(storageValues()).not.toContain(token)
  expect(document.cookie).not.toContain(token)
})

it('F11-11.2-02 MFA challenge accepts only six digits and posts the pending token to /api/auth/mfa/verify', async () => {
  const pending = pendingToken(), full = fullToken()
  routes = { 'GET /api/auth/capabilities': () => [200, WIRE.capabilitiesDisabled],
    'POST /api/auth/login': () => [200, { ...WIRE.loginMfaRequired, access_token: pending }],
    'POST /api/auth/mfa/verify': () => [200, { ...WIRE.mfaVerify, access_token: full }] }
  mount(); await signIn()
  expect(await screen.findByRole('heading', { name: 'Two-factor authentication' })).toBeVisible()
  const input = screen.getByLabelText('Authentication code')
  const submit = screen.getByRole('button', { name: 'Verify and sign in' })
  fireEvent.change(input, { target: { value: '12a45' } })
  expect(input).toHaveValue('1245'); expect(submit).toBeDisabled()
  fireEvent.change(input, { target: { value: '12345' } }); expect(submit).toBeDisabled()
  fireEvent.change(input, { target: { value: '123456' } }); expect(submit).toBeEnabled()
  expect(calls.some(c => c.url === '/api/auth/mfa/verify')).toBe(false)
  fireEvent.click(submit)
  await screen.findByText('Signed-in home')
  const verify = calls.filter(c => c.url === '/api/auth/mfa/verify')
  expect(verify).toHaveLength(1)
  expect(verify[0].body).toEqual({ mfa_token: pending, code: '123456' })
  expect(storageValues()).not.toContain(pending); expect(storageValues()).not.toContain(full)
})

it('F11-11.2-02 a code rejected by the backend (401 Invalid MFA code) never signs in and requires restart', async () => {
  routes = { 'GET /api/auth/capabilities': () => [200, WIRE.capabilitiesDisabled],
    'POST /api/auth/login': () => [200, { ...WIRE.loginMfaRequired, access_token: pendingToken() }],
    'POST /api/auth/mfa/verify': () => [401, { detail: 'Invalid MFA code' }] }
  mount(); await signIn()
  fireEvent.change(await screen.findByLabelText('Authentication code'), { target: { value: '000000' } })
  fireEvent.click(screen.getByRole('button', { name: 'Verify and sign in' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Sign-in could not be confirmed. Restart sign-in.')
  expect(screen.queryByText('Signed-in home')).not.toBeInTheDocument()
})

it('F11-11.1-06/08 smtp_enabled=true shows the forgot-password link which requests a reset email', async () => {
  routes = { 'GET /api/auth/capabilities': () => [200, WIRE.capabilitiesEnabled],
    'POST /api/auth/forgot-password': () => [200, WIRE.forgot] }
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Forgot your password?' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Account email' }), { target: { value: 'reader@example.test' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send Reset Link' }))
  expect(await screen.findByText('If that email is registered, a reset link has been sent.')).toBeVisible()
  const req = calls.filter(c => c.url === '/api/auth/forgot-password')
  expect(req).toHaveLength(1)
  expect(req[0]).toMatchObject({ method: 'POST', body: { email: 'reader@example.test' } })
})

it('F11-11.1-08 smtp_enabled=false hides the reset action and shows administrator guidance', async () => {
  routes = { 'GET /api/auth/capabilities': () => [200, WIRE.capabilitiesDisabled] }
  mount()
  await waitFor(() => expect(calls.some(c => c.url === '/api/auth/capabilities')).toBe(true))
  expect(await screen.findByText('Forgot your password? Contact your administrator.')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Forgot your password?' })).not.toBeInTheDocument()
})

// DEFECT: frontend/src/pages/auth/Login.tsx:187-188 + backend/modules/organizations/password_reset.py:25-35.
// With SMTP configured through Settings but no (unconfigurable) base_url, the backend refuses with 503 and
// sends nothing, yet the page tells the user a reset link has been sent.
it.fails('DEFECT F11-11.1-06 a refused reset request (503) is not reported as sent', async () => {
  routes = { 'GET /api/auth/capabilities': () => [200, WIRE.capabilitiesEnabled],
    'POST /api/auth/forgot-password': () => [503, { detail: 'Password reset requires a valid application base URL.' }] }
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Forgot your password?' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Account email' }), { target: { value: 'reader@example.test' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send Reset Link' }))
  await waitFor(() => expect(calls.some(c => c.url === '/api/auth/forgot-password')).toBe(true))
  await new Promise(resolve => setTimeout(resolve, 50))
  expect(screen.queryByText('If that email is registered, a reset link has been sent.')).not.toBeInTheDocument()
})
