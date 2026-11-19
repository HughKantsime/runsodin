import { afterEach, expect, test, vi } from 'vitest'
import { fetchAPI, dataFolderLockMessage } from './client'
afterEach(() => vi.restoreAllMocks())

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

test.each(['http', 'education'])('reads the storage refusal from the %s error envelope', async kind => {
  const message = 'Set the printer lock file inside the ODIN data folder to mode 600. No command was sent.'
  const body = kind === 'http' ? { detail: { message, storage_reason: 'lock_permissions' } }
    : { detail: message, error: { code: 'revision_conflict', detail: message, storage_reason: 'lock_permissions' } }
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json(body, { status: 409 }))
  const refusal = await fetchAPI('/owned-test', { quiet: true }).catch(dataFolderLockMessage)
  expect(refusal).toBe(message)
})
