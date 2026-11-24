import { afterEach, expect, test, vi } from 'vitest'
import { fetchAPI, dataFolderLockMessage } from './client'
import { auth, sessions } from './auth'
import { clearSensitiveBrowserState } from '../permissions'
vi.mock('../permissions', () => ({clearSensitiveBrowserState: vi.fn()}))
afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks(); vi.unstubAllGlobals() })

test('caller headers keep the JSON Content-Type on a JSON body', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}'))
  await fetchAPI('/system/troubleshooting/report', { method: 'POST', headers: { 'X-ODIN-Controlled-Ack': 'true' }, body: JSON.stringify({ problem: 'x' }) })
  const headers = new Headers(fetch.mock.calls[0][1]?.headers)
  expect(headers.get('Content-Type')).toBe('application/json')
  expect(headers.get('X-ODIN-Controlled-Ack')).toBe('true')
})

test('an empty headers object keeps the JSON Content-Type', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}'))
  await fetchAPI('/x', { method: 'POST', headers: {}, body: '{}' })
  expect(new Headers(fetch.mock.calls[0][1]?.headers).get('Content-Type')).toBe('application/json')
})

test('a caller Content-Type is kept and credentials default to include', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}'))
  await fetchAPI('/x', { method: 'POST', headers: { 'Content-Type': 'application/sdp' }, body: 'v=0' })
  const options = fetch.mock.calls[0][1]
  expect(new Headers(options?.headers).get('Content-Type')).toBe('application/sdp')
  expect(options?.credentials).toBe('include')
})

test.each([
  ['client default', () => fetchAPI('/x')],
  ['setup', auth.mfaSetup], ['confirm', () => auth.mfaConfirm('123456')],
  ['recovery count', auth.recoveryStatus], ['MFA status', auth.mfaStatus],
  ['sessions', sessions.list], ['end session', () => sessions.revoke(1)],
  ['admin sessions', sessions.adminList], ['admin end session', () => sessions.adminRevoke(1)],
  ['end other sessions', sessions.revokeAll],
] as const)('%s clears state and redirects on 401', async (_, request) => {
  vi.stubGlobal('location', {pathname: '/account', href: '/account'})
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', {status: 401}))
  await request()
  expect(clearSensitiveBrowserState).toHaveBeenCalledOnce()
  expect(window.location.href).toBe('/login')
})

test.each([
  ['client option', () => fetchAPI('/x', {showUnauthorized: true, quiet: true})],
  ['regenerate', () => auth.regenerateCodes('123456')], ['disable', () => auth.mfaDisable('123456')],
] as const)('%s returns the 401 detail and keeps the session', async (_, request) => {
  vi.stubGlobal('location', {pathname: '/account', href: '/account'})
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{"detail":"Invalid MFA code"}', {status: 401}))
  await expect(request()).rejects.toMatchObject({status: 401, message: 'Invalid MFA code'})
  expect(clearSensitiveBrowserState).not.toHaveBeenCalled()
  expect(window.location.href).toBe('/account')
})

test.each(['http', 'education'])('reads the storage refusal from the %s error envelope', async kind => {
  const message = 'Set the printer lock file inside the ODIN data folder to mode 600. No command was sent.'
  const body = kind === 'http' ? { detail: { message, storage_reason: 'lock_permissions' } }
    : { detail: message, error: { code: 'resource_conflict', detail: message, retriable: false, storage_reason: 'lock_permissions' } }
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json(body, { status: 409 }))
  const refusal = await fetchAPI('/owned-test', { quiet: true }).catch(dataFolderLockMessage)
  expect(refusal).toBe(message)
})
