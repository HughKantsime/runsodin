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
