import { afterEach, expect, test, vi } from 'vitest'
import { config } from './system'
import { fetchAPI } from './client'

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

test.each([404, 500, 503, 599])('remote status HTTP %s errors do not log response details', async status => {
  const error = vi.spyOn(console, 'error').mockImplementation(() => {})
  const fetcher = vi.fn().mockResolvedValue({ status, ok: false, json: async () => ({ detail: 'proxy ::1' }) })
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  await expect(config.getRemoteAccess(controller.signal)).rejects.toThrow()
  expect(error).not.toHaveBeenCalled()
  expect(fetcher).toHaveBeenCalledWith('/api/settings/remote-access', expect.objectContaining({ signal: controller.signal, credentials: 'include' }))
  expect(fetcher.mock.calls[0][1]).not.toHaveProperty('quiet')
})

test('other API callers retain error reporting', async () => {
  const error = vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 500, ok: false, json: async () => ({ detail: 'synthetic failure' }) }))
  await expect(fetchAPI('/config')).rejects.toThrow('synthetic failure')
  expect(error).toHaveBeenCalledWith('[API] Error detail:', 'synthetic failure')
})
