import { afterEach, expect, it, vi } from 'vitest'
import { webcrypto, createHash } from 'node:crypto'
import { ImageBudget, MiB, boundedBytes, jpegDimensions, preview } from './maintenanceMedia'
import { ownedJpeg } from './ownedJpeg.test-fixture'
const OriginalURL = URL
function urls() { const revoke = vi.fn(); vi.stubGlobal('URL', class extends OriginalURL { static createObjectURL = vi.fn(() => 'blob:owned'); static revokeObjectURL = revoke }); return revoke }
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
it('reserves aggregate decoded/encoded and transient allocations before acquisition; exhaustion and release are idempotent', () => {
  urls(); const budget = new ImageBudget(), a = budget.reserve('owner', true, vi.fn()), b = budget.reserve('owner', true, vi.fn())
  expect([budget.encoded, budget.decoded, budget.working]).toEqual([16 * MiB, 128 * MiB, 48 * MiB]); expect(() => budget.reserve('owner', false, vi.fn())).toThrow(/budget/)
  a.release(); a.release(); expect([budget.encoded, budget.decoded, budget.working]).toEqual([8 * MiB, 64 * MiB, 24 * MiB]); b.release(); expect([budget.encoded, budget.decoded, budget.working]).toEqual([0, 0, 0])
})
it('evicts a ready nonselected URL but protects selected preview and cleans owner allocations', () => {
  const revoke = urls(), evict = vi.fn(), budget = new ImageBudget(), a = budget.reserve('owner', false, evict); a.publish('blob:old')
  const selected = budget.reserve('owner', true, vi.fn()); const next = budget.reserve('owner', false, vi.fn())
  expect(evict).toHaveBeenCalledTimes(1); expect(revoke).toHaveBeenCalledWith('blob:old'); expect(selected.controller.signal.aborted).toBe(false); expect(next.controller.signal.aborted).toBe(false)
  budget.clear('owner'); budget.clear('owner'); expect([budget.encoded, budget.decoded, budget.working]).toEqual([0, 0, 0])
})
it('hashes exact bounded JPEG bytes before allocating its displayed blob and retires on cleanup', async () => {
  const revoke = urls(); vi.stubGlobal('crypto', webcrypto); vi.stubGlobal('fetch', vi.fn(async () => new Response(ownedJpeg, { headers: { 'content-type': 'image/jpeg' } })))
  const budget = new ImageBudget(), lease = budget.reserve('owner', true, vi.fn()); const image = await preview('9/frame.jpg', lease)
  expect(image.digest).toBe(createHash('sha256').update(ownedJpeg).digest('hex')); expect(image).toMatchObject({ width: 32, height: 32, url: 'blob:owned' }); expect(budget.working).toBe(0); expect(budget.decoded).toBe(32 * 32 * 4)
  lease.release(); expect(revoke).toHaveBeenCalledWith('blob:owned')
})
it.each([8 * MiB, 80 * MiB])('aborts streamed oversized response before combining bytes (%i)', async limit => {
  const controller = new AbortController(); const stream = new ReadableStream<Uint8Array>({ start(c) { c.enqueue(new Uint8Array(limit)); c.enqueue(new Uint8Array(1)); c.close() } })
  await expect(boundedBytes(new Response(stream), limit, controller)).rejects.toThrow(/size/); expect(controller.signal.aborted).toBe(true)
})
it('rejects malformed JPEG, oversize dimensions and unsupported orientation before browser decode', () => {
  expect(() => jpegDimensions(new Uint8Array([1, 2]))).toThrow(/JPEG/)
  const huge = ownedJpeg.slice(); const marker = huge.findIndex((v, i) => v === 255 && huge[i + 1] === 192); huge[marker + 7] = 32; huge[marker + 8] = 0
  expect(() => jpegDimensions(huge)).toThrow(/dimensions/)
  const exif = new Uint8Array([...ownedJpeg.slice(0, 2), 255, 225, 0, 8, 69, 120, 105, 102, 0, 0, ...ownedJpeg.slice(2)])
  expect(() => jpegDimensions(exif)).toThrow(/orientation/)
})
it('10s preview deadline aborts acquisition; invalid path never requests or allocates URL', async () => {
  urls(); vi.useFakeTimers(); vi.stubGlobal('fetch', vi.fn((_url, options) => new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))))
  const budget = new ImageBudget(), lease = budget.reserve('owner', true, vi.fn()); const result = preview('9/frame.jpg', lease); const rejected = expect(result).rejects.toThrow(/aborted/); await vi.advanceTimersByTimeAsync(10_000); await rejected; lease.release()
  const invalid = budget.reserve('owner', true, vi.fn()); await expect(preview('../foreign.jpg', invalid)).rejects.toThrow(/path/); invalid.release(); expect(fetch).toHaveBeenCalledTimes(1); expect(URL.createObjectURL).not.toHaveBeenCalled()
})

it('refuses insecure-context hashing with useful guidance and releases all reservations before acquisition', async () => {
  urls(); vi.stubGlobal('crypto', {}); vi.stubGlobal('fetch', vi.fn())
  const budget = new ImageBudget(), lease = budget.reserve('owner', true, vi.fn())
  await expect(preview('9/frame.jpg', lease)).rejects.toThrow(/HTTPS or localhost/)
  expect(lease.controller.signal.aborted).toBe(true)
  expect([budget.encoded, budget.decoded, budget.working]).toEqual([0, 0, 0])
  expect(fetch).not.toHaveBeenCalled(); expect(URL.createObjectURL).not.toHaveBeenCalled()
})
