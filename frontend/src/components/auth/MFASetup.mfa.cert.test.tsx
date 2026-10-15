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
    if (method === 'POST' && url === '/api/auth/mfa/setup') return json(200, WIRE.setup)
    if (method === 'POST' && url === '/api/auth/mfa/confirm') {
      if ((body as { code?: string })?.code !== '246810') return json(400, { detail: 'Invalid code. Scan the QR code and try again.' })
      enabled = true; return json(200, WIRE.confirm)
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
