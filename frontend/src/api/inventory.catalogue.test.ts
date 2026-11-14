import { afterEach, expect, it, vi } from 'vitest'
import { filaments } from './inventory'

afterEach(() => vi.unstubAllGlobals())
it('sends JSON Content-Type and session credentials for a File without a MIME type', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ created: 1 }), { status: 200 }))
  vi.stubGlobal('fetch', fetch)
  const file = new File(['{}'], 'invented.json')
  expect(file.type).toBe('')
  await filaments.importBambu(file)
  expect(fetch).toHaveBeenCalledOnce()
  const [url, options] = fetch.mock.calls[0]
  expect(url).toBe('/api/filaments/import-bambu')
  expect(options.method).toBe('POST')
  expect(options.body).toBe(file)
  expect(options.credentials).toBe('include')
  expect(new Headers(options.headers).get('Content-Type')).toBe('application/json')
})
