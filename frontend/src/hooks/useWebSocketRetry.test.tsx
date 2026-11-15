import { cleanup, renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import useWebSocket, { tokenRetryDelayMs } from './useWebSocket'

// Regression (screen check 2026-11-13): a refused live-updates token (HTTP 429 after several
// reloads) ended the hook for good, so the page showed no live updates until it was reloaded.

let sockets: any[]
class Socket {
  onopen: any; onclose: any; onmessage: any; onerror: any
  constructor(public url: string) { sockets.push(this) }
  close() {}
}
const wrapper = ({ children }: any) => <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
const reply = (status: number, token?: string) => ({ ok: status === 200, status, json: async () => ({ token }) })
const settle = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

beforeEach(() => { sockets = []; vi.useFakeTimers(); vi.stubGlobal('WebSocket', Socket) })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('live updates token retry', () => {
  it('backs off 5, 10, 20, 40 then 60 seconds', () => {
    expect([0, 1, 2, 3, 4, 5, 50].map(tokenRetryDelayMs)).toEqual([5000, 10000, 20000, 40000, 60000, 60000, 60000])
  })

  it('asks again after a rate-limited token and connects once one is issued', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(reply(429)).mockResolvedValueOnce(reply(503)).mockResolvedValue(reply(200, 'synthetic-local-token'))
    vi.stubGlobal('fetch', fetchMock)
    renderHook(() => useWebSocket(), { wrapper })
    await settle(0)
    expect(fetchMock).toHaveBeenCalledTimes(1); expect(sockets).toHaveLength(0)
    await settle(4999); expect(fetchMock).toHaveBeenCalledTimes(1)
    await settle(1); expect(fetchMock).toHaveBeenCalledTimes(2); expect(sockets).toHaveLength(0)
    await settle(10000)
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(sockets).toHaveLength(1)
    expect(sockets[0].url).toContain('/ws?token=synthetic-local-token')
  })

  it('retries after a network error too', async () => {
    const fetchMock = vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValue(reply(200, 'synthetic-local-token'))
    vi.stubGlobal('fetch', fetchMock)
    renderHook(() => useWebSocket(), { wrapper })
    await settle(5000)
    expect(sockets).toHaveLength(1)
  })

  it('does not keep asking once the session is refused', async () => {
    const fetchMock = vi.fn().mockResolvedValue(reply(401))
    vi.stubGlobal('fetch', fetchMock)
    renderHook(() => useWebSocket(), { wrapper })
    await settle(120000)
    expect(fetchMock).toHaveBeenCalledTimes(1); expect(sockets).toHaveLength(0)
  })

  it('stops retrying when the page goes away', async () => {
    const fetchMock = vi.fn().mockResolvedValue(reply(429))
    vi.stubGlobal('fetch', fetchMock)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await settle(0)
    unmount()
    await settle(120000)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not schedule a retry or request a token when the socket closes after unmount', async () => {
    const fetchMock = vi.fn().mockResolvedValue(reply(200, 'synthetic-local-token'))
    vi.stubGlobal('fetch', fetchMock)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await settle(0)
    expect(sockets).toHaveLength(1)
    const socket = sockets[0]
    act(() => socket.onopen())
    // Browsers deliver close asynchronously, after effect cleanup has cleared its timer.
    socket.close = vi.fn(() => setTimeout(() => socket.onclose(), 0))
    unmount()
    expect(socket.close).toHaveBeenCalledTimes(1)
    await settle(0)
    const pendingTimers = vi.getTimerCount()
    await settle(120000)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(sockets).toHaveLength(1)
    expect(pendingTimers).toBe(0)
  })

  it('a later failure starts the backoff again from 5 seconds after a good connection', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(reply(429)).mockResolvedValueOnce(reply(200, 'synthetic-local-token')).mockResolvedValueOnce(reply(429)).mockResolvedValue(reply(200, 'synthetic-local-token'))
    vi.stubGlobal('fetch', fetchMock)
    renderHook(() => useWebSocket(), { wrapper })
    await settle(5000); expect(sockets).toHaveLength(1)
    await act(async () => { sockets[0].onclose() })      // server restarts: the hook reconnects in 5 s
    await settle(5000); expect(fetchMock).toHaveBeenCalledTimes(3); expect(sockets).toHaveLength(1)
    await settle(5000); expect(sockets).toHaveLength(2)   // 5 s again, not 10
  })
})
