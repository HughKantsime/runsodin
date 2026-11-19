// Cert F / cluster A — real MFASetup + real api client (fetchAPI), fetch boundary only.
// Wire fixtures were captured from the real backend by tests/test_contracts/test_cert_F_A_mfa.py
// The TOTP secret and the QR image are generated per run (no secret literal in the repository);
// the response shape (provisioning_uri, qr_code, secret) is the producer's.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import MFASetup from './MFASetup'

const BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
const TOTP_SECRET = Array.from(crypto.getRandomValues(new Uint8Array(32)), b => BASE32[b % 32]).join('')
// 1x1 PNG standing in for the server-rendered QR code (only its data: URL shape matters to the UI).
const QR_PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='

const WIRE = {
  statusDisabled: {"mfa_enabled":false},
  statusEnabled: {"mfa_enabled":true},
  setup: { provisioning_uri: `otpauth://totp/O.D.I.N.:mfa_person?secret=${TOTP_SECRET}&issuer=O.D.I.N.`, qr_code: QR_PNG, secret: TOTP_SECRET },
  confirm: {"message":"MFA enabled successfully","status":"ok"},
}

type Call = { url: string; method: string; body: unknown; credentials?: string }
let calls: Call[] = []
let enabled = false
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

beforeEach(() => {
  calls = []; enabled = false
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input), method = (init.method || 'GET').toUpperCase()
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : init.body
    calls.push({ url, method, body, credentials: init.credentials })
    if (method === 'GET' && url === '/api/auth/mfa/status') return json(200, enabled ? WIRE.statusEnabled : WIRE.statusDisabled)
    if (method === 'GET' && url === '/api/auth/mfa/recovery-codes') return json(200, { remaining: 3, generated_at: null })
    if (method === 'POST' && url === '/api/auth/mfa/setup') return json(200, WIRE.setup)
    if (method === 'POST' && url === '/api/auth/mfa/confirm') {
      if ((body as { code?: string })?.code !== '246810') return json(400, { detail: 'Invalid code. Scan the QR code and try again.' })
      enabled = true; return json(200, { ...WIRE.confirm, recovery_codes: ['example-code'] })
    }
    return json(404, { detail: 'unexpected' })
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('F11-11.2-01 MFASetup renders the server QR code and secret, then confirms with a 6-digit code', async () => {
  render(<MFASetup />)
  fireEvent.click(await screen.findByRole('button', { name: 'Set Up Two-Factor Authentication' }))
  const qr = await screen.findByAltText('MFA QR Code')
  expect(qr).toHaveAttribute('src', WIRE.setup.qr_code)
  expect(WIRE.setup.qr_code.startsWith('data:image/png;base64,')).toBe(true)
  expect(screen.getByText(WIRE.setup.secret)).toBeVisible()
  expect(WIRE.setup.provisioning_uri).toContain('secret=' + WIRE.setup.secret)
  const setupCall = calls.find(c => c.url === '/api/auth/mfa/setup')!
  expect(setupCall).toMatchObject({ method: 'POST', credentials: 'include' })

  const input = screen.getByPlaceholderText('000000')
  const confirm = screen.getByRole('button', { name: 'Confirm' })
  fireEvent.change(input, { target: { value: '13579' } }); expect(confirm).toBeDisabled()
  fireEvent.change(input, { target: { value: '135791' } }); fireEvent.click(confirm)
  expect(await screen.findByText('Invalid code. Scan the QR code and try again.')).toBeVisible()
  expect(screen.getByAltText('MFA QR Code')).toBeVisible()

  fireEvent.change(input, { target: { value: '246810' } }); fireEvent.click(confirm)
  expect(await screen.findByText('MFA enabled successfully')).toBeVisible()
  expect(screen.queryByAltText('MFA QR Code')).not.toBeInTheDocument()
  expect(await screen.findByText('example-code')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'I saved these' }))
  expect(screen.queryByText('example-code')).not.toBeInTheDocument()
  expect(screen.getByText('Enabled')).toBeVisible()
  expect(calls.filter(c => c.url === '/api/auth/mfa/confirm').map(c => c.body)).toEqual([{ code: '135791' }, { code: '246810' }])
})

it('F11-11.2-01 an already-enrolled account is not offered setup', async () => {
  enabled = true
  render(<MFASetup />)
  expect(await screen.findByText('Your account is protected with two-factor authentication.')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Set Up Two-Factor Authentication' })).not.toBeInTheDocument()
  expect(calls.some(c => c.url === '/api/auth/mfa/setup')).toBe(false)
})

it('replaces a low set, shows server errors, and disables with an unused recovery code', async () => {
  enabled = true
  const request = vi.mocked(fetch)
  render(<MFASetup />)
  expect(await screen.findByText('3 recovery codes remaining.')).toBeVisible()
  expect(screen.getByText(/running low/)).toBeVisible()
  fireEvent.click(screen.getByRole('button', {name: 'Regenerate recovery codes'}))
  const lock = 'Too many wrong codes. Try again in 4 minutes.'
  for (const [status, detail] of [[400, 'Enter an authenticator or recovery code.'], [401, 'Invalid MFA code'], [429, lock]] as const) {
    request.mockResolvedValueOnce(json(status, {detail}))
    fireEvent.change(screen.getByLabelText(/generate a new set/), {target: {value: 'example-code'}})
    fireEvent.click(screen.getByText('Generate new codes'))
    await screen.findByText(detail)
  }
  const codes = Array.from({length: 10}, (_, i) => 'example-' + i)
  request.mockResolvedValueOnce(json(200, {recovery_codes: codes}))
  fireEvent.change(screen.getByLabelText(/generate a new set/), {target: {value: 'example-code'}})
  fireEvent.click(screen.getByText('Generate new codes'))
  await screen.findByRole('dialog')
  expect(request).toHaveBeenLastCalledWith('/api/auth/mfa/recovery-codes', expect.objectContaining({
    method: 'POST', body: JSON.stringify({code: 'example-code'}), cache: 'no-store',
  }))
  fireEvent.click(screen.getByText('I saved these'))
  expect(screen.queryByText(codes[0])).not.toBeInTheDocument()
  expect(screen.getByText('10 recovery codes remaining.')).toBeVisible()
  expect(screen.queryByText(/running low/)).not.toBeInTheDocument()
  request.mockResolvedValueOnce(json(429, {detail: lock})).mockResolvedValueOnce(json(200, {status: 'ok'}))
  for (let i = 0; i < 2; i++) {
    fireEvent.change(screen.getByLabelText(/disable MFA/), {target: {value: codes[0]}})
    fireEvent.click(screen.getByRole('button', {name: 'Disable MFA'}))
    if (i === 0) await screen.findByText(lock)
  }
  await screen.findByText('MFA disabled')
  expect(request).toHaveBeenLastCalledWith('/api/auth/mfa', expect.objectContaining({
    method: 'DELETE', body: JSON.stringify({recovery_code: codes[0]}),
  }))
})
