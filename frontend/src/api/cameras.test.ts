import { afterEach, expect, test, vi } from 'vitest'
import { timelapses } from './cameras'
afterEach(() => vi.unstubAllGlobals())
test('detail reads actual ETag and DELETE carries exact header and credentials', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ id: 17 }), { headers: { ETag: '"actual-nonce"' } })).mockResolvedValueOnce(new Response(null, { status: 204 })); vi.stubGlobal('fetch', fetch)
  const detail = await timelapses.detail(17); await timelapses.delete(17, detail.etag)
  expect(fetch.mock.calls[1]).toEqual(['/api/timelapses/17', { method: 'DELETE', credentials: 'include', headers: { 'If-Match': '"actual-nonce"' }, signal: undefined }])
})
test('missing or malformed ETag never fabricates a deletion precondition', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 17 }))))
  await expect(timelapses.detail(17)).rejects.toThrow(/no verified lifetime/)
  await expect(timelapses.delete(17, '17')).rejects.toThrow(/Exact lifetime/); expect(fetch).toHaveBeenCalledTimes(1)
})
test('412 is preserved for explicit UI refresh, and non204 never claims completed retirement', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'changed' }), { status: 412 })).mockResolvedValueOnce(new Response('{}')))
  await expect(timelapses.delete(17, '"original"')).rejects.toMatchObject({ status: 412 })
  await expect(timelapses.delete(17, '"original"')).rejects.toThrow(/not confirmed/)
})

test.each([{ all_ready: true } as const, { timelapse_ids: [17, 19] }])('re-check posts selection with session cookies and no raw credential: %j', async selection => {
  const page = { results: [{ timelapse_id: 17, status: 're-admitted', changed: true }], next_after_id: 100 }
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(page))); vi.stubGlobal('fetch', fetch)
  const controller = new AbortController()
  expect(await timelapses.recheckFacts(selection, 100, controller.signal)).toEqual(page)
  expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/timelapses/recheck-facts?after_id=100', {
    method: 'POST', credentials: 'include', signal: controller.signal,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(selection),
  })
})

test('re-check propagates refusal without retry', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Superadmin access required' }), { status: 403 })); vi.stubGlobal('fetch', fetch)
  await expect(timelapses.recheckFacts({ all_ready: true })).rejects.toMatchObject({ status: 403, message: 'Superadmin access required' })
  expect(fetch).toHaveBeenCalledTimes(1)
})
