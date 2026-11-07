import { afterEach, expect, test, vi } from 'vitest'
import { adminLogs, diagnostics } from './system'
afterEach(() => vi.restoreAllMocks())

test('log pages acknowledge through a POST header, never a query flag', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ lines: [] })))
  await adminLogs.get('backend', 20, null, null, null, true)
  const [url, options] = fetch.mock.calls[0]
  expect(String(url)).not.toContain('acknowledge_controlled')
  expect(options?.method).toBe('POST')
  expect(new Headers(options?.headers).get('X-ODIN-Controlled-Ack')).toBe('true')
})

test('stream uses the token returned by an acknowledged POST', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ token: 'actor-bound-token' })))
  const url = await adminLogs.streamUrl('backend', 'ERROR', 'filter', true)
  expect(fetch.mock.calls[0][0]).toBe('/api/admin/logs/stream-token')
  expect(fetch.mock.calls[0][1]?.method).toBe('POST')
  expect(new Headers(fetch.mock.calls[0][1]?.headers).get('X-ODIN-Controlled-Ack')).toBe('true')
  expect(new URL(url, 'http://localhost').searchParams.get('token')).toBe('actor-bound-token')
  expect(url).not.toContain('acknowledge_controlled')
})

test('diagnostics uses POST with optional explicit acknowledgement', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('{}'))
  await diagnostics.fetch()
  await diagnostics.fetch(true)
  expect(fetch.mock.calls.every(call => call[1]?.method === 'POST')).toBe(true)
  expect(new Headers(fetch.mock.calls[0][1]?.headers).has('X-ODIN-Controlled-Ack')).toBe(false)
  expect(new Headers(fetch.mock.calls[1][1]?.headers).get('X-ODIN-Controlled-Ack')).toBe('true')
})
