import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { usePlatePicture } from './usePlatePicture'

const fetchMock = vi.fn()
const imageResponse = () => new Response(new Blob(['plate'], { type: 'image/png' }), {
  status: 200, headers: { 'Content-Type': 'image/png' },
})
const advance = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

beforeEach(() => {
  vi.useFakeTimers()
  fetchMock.mockReset().mockImplementation(async () => new Response(null, { status: 404 }))
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => 'blob:plate')
    static revokeObjectURL = vi.fn()
  })
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })

it('loads a newly cached picture after a 404 without changing the print or rerendering', async () => {
  fetchMock.mockImplementationOnce(async () => new Response(null, { status: 404 }))
    .mockImplementationOnce(async () => imageResponse())
  const { result } = renderHook(() => usePlatePicture(199, true, 'first'))
  await advance()
  expect(result.current).toBeNull()
  await advance(9999)
  expect(fetchMock).toHaveBeenCalledTimes(1)
  await advance(1)
  expect(result.current).toBe('blob:plate')
  expect(fetchMock).toHaveBeenCalledTimes(2)
  await advance(120000)
  expect(fetchMock).toHaveBeenCalledTimes(2)
})

it('stops after six retries when the picture stays unavailable, even across rerenders', async () => {
  const { result, rerender } = renderHook(() => usePlatePicture(199, true, 'first'))
  await advance(60000)
  expect(fetchMock).toHaveBeenCalledTimes(7)
  for (let i = 0; i < 5; i++) rerender()
  await advance(120000)
  expect(fetchMock).toHaveBeenCalledTimes(7)
  expect(result.current).toBeNull()
  expect(vi.getTimerCount()).toBe(0)
})

it('cancels a pending retry when disabled and starts fresh when re-enabled', async () => {
  const { rerender } = renderHook(({ enabled }) => usePlatePicture(199, enabled, 'first'), {
    initialProps: { enabled: true },
  })
  await advance()
  expect(vi.getTimerCount()).toBe(1)
  rerender({ enabled: false })
  expect(vi.getTimerCount()).toBe(0)
  expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true)
  await advance(120000)
  expect(fetchMock).toHaveBeenCalledTimes(1)
  rerender({ enabled: true })
  await advance(10000)
  expect(fetchMock).toHaveBeenCalledTimes(3)
})

it('cancels a pending retry on unmount', async () => {
  const { unmount } = renderHook(() => usePlatePicture(199, true, 'first'))
  await advance()
  expect(vi.getTimerCount()).toBe(1)
  unmount()
  expect(vi.getTimerCount()).toBe(0)
  await advance(120000)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it.each([
  { printerId: 199, runKey: 'second' },
  { printerId: 200, runKey: 'first' },
])('restarts retries when the print identity changes to $printerId/$runKey', async next => {
  const { rerender } = renderHook(({ printerId, runKey }) => usePlatePicture(printerId, true, runKey), {
    initialProps: { printerId: 199, runKey: 'first' },
  })
  await advance(5000)
  rerender(next)
  await advance(5000)
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true)
  await advance(5000)
  expect(fetchMock).toHaveBeenCalledTimes(3)
  expect(fetchMock.mock.calls[2][0]).toBe(`/api/printers/${next.printerId}/current-plate-thumbnail`)
})

it.each([false, true])('ignores a late retry response after disabling (has image: %s)', async hasImage => {
  let resolve!: (response: Response) => void
  fetchMock.mockImplementationOnce(async () => new Response(null, { status: 404 }))
    .mockImplementationOnce(() => new Promise<Response>(done => { resolve = done }))
  const { result, rerender } = renderHook(({ enabled }) => usePlatePicture(199, enabled, 'first'), {
    initialProps: { enabled: true },
  })
  await advance(10000)
  expect(fetchMock).toHaveBeenCalledTimes(2)
  // Slow responses must not cause overlapping requests.
  await advance(60000)
  expect(fetchMock).toHaveBeenCalledTimes(2)
  rerender({ enabled: false })
  expect(fetchMock.mock.calls[1][1].signal.aborted).toBe(true)
  resolve(hasImage ? imageResponse() : new Response(null, { status: 404 }))
  await advance(120000)
  expect(result.current).toBeNull()
  expect(URL.createObjectURL).not.toHaveBeenCalled()
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(0)
})
