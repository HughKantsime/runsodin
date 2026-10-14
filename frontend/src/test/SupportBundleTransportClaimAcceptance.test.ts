import { afterEach, expect, test, vi } from 'vitest'
import { adminBundle } from '../api/system'

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

test('actual support adapter downloads returned ZIP bytes and releases URL', async () => {
  const blob = new Blob(['PK\u0003\u0004owned-test-bytes'], { type: 'application/zip' })
  const fetch = vi.fn().mockResolvedValue({ ok: true, blob: async () => blob })
  vi.stubGlobal('fetch', fetch)
  const create = vi.fn().mockReturnValue('blob:owned-support-bundle')
  const release = vi.fn()
  vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: release })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  await adminBundle.download()
  expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/admin/support-bundle', { credentials: 'include' })
  expect(create).toHaveBeenCalledExactlyOnceWith(blob)
  expect(click).toHaveBeenCalledOnce()
  expect(click.mock.instances[0].download).toMatch(/^odin-support-bundle-\d{4}-\d{2}-\d{2}\.zip$/)
  expect(release).toHaveBeenCalledExactlyOnceWith('blob:owned-support-bundle')
})

test('server refusal does not fabricate a bundle download', async () => {
  const body = vi.fn()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 403, blob: body }))
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  await expect(adminBundle.download()).rejects.toThrow('Failed to generate bundle')
  expect(body).not.toHaveBeenCalled()
  expect(click).not.toHaveBeenCalled()
})
