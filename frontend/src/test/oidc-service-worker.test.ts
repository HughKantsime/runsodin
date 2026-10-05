// @vitest-environment node
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { expect, it, vi } from 'vitest'

function worker() {
  const listeners: Record<string, Function> = {}
  const cache = { keys: vi.fn().mockResolvedValue([]), delete: vi.fn().mockResolvedValue(true) }
  const caches = { keys: vi.fn().mockResolvedValue(['odin-v1.9.16']), open: vi.fn().mockResolvedValue(cache), delete: vi.fn() }
  const claim = vi.fn()
  runInNewContext(readFileSync(new URL('../../public/sw.js', import.meta.url), 'utf8'), {
    URL, console: { log() {} }, caches, self: { addEventListener: (name: string, fn: Function) => { listeners[name] = fn }, clients: { claim } },
  })
  return { listeners, cache, caches, claim }
}
it.each(['/?oidc_code=a', '/?oidc_code=', '/?error=x', '/login?oidc_code=a'])('bypasses cache for %s', path => {
  const { listeners, caches } = worker()
  const respondWith = vi.fn()
  listeners.fetch({ request: { url: `https://odin.test${path}`, method: 'GET' }, respondWith })
  expect(respondWith).not.toHaveBeenCalled()
  expect(caches.open).not.toHaveBeenCalled()
})
it('purges legacy callback keys before claiming clients, retaining ordinary assets', async () => {
  const { listeners, cache, claim } = worker()
  const callback = { url: 'https://odin.test/?oidc_code=synthetic' }
  cache.keys.mockResolvedValue([callback, { url: 'https://odin.test/' }])
  let activation!: Promise<void>
  listeners.activate({ waitUntil: (promise: Promise<void>) => { activation = promise } })
  await activation
  expect(cache.delete).toHaveBeenCalledExactlyOnceWith(callback)
  expect(claim).toHaveBeenCalledTimes(1)
  expect(cache.delete.mock.invocationCallOrder[0]).toBeLessThan(claim.mock.invocationCallOrder[0])
})
