import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { useFloorData } from './data'

const mocks = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../../api/client', () => ({ fetchAPI: (...args: unknown[]) => mocks.request(...args) }))
vi.mock('../../api', async () => ({
  alerts: (await import('../../api/notifications')).alerts,
  printers: { list: async () => [] },
  printJobs: { list: async () => [] },
}))
vi.mock('../../permissions', () => ({ canAccessPage: (page: string) => page === 'alerts' }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('fetches floor notices once per refresh with 2,500 alerts available and keeps every paused printer', async () => {
  const history = Array.from({ length: 2500 }, (_, index) => ({
    id: index + 1,
    // The first 100 printers have old alerts buried under printer 101's noise.
    printer_id: Math.min(index + 1, 101),
    alert_type: 'print_paused',
    is_dismissed: false,
  }))
  mocks.request.mockImplementation(async (path: string) => {
    const url = new URL(path, 'http://localhost')
    if (url.searchParams.get('needs_you') === 'true') {
      // Emulate the server contract; backend tests exercise the SQL reduction.
      return [...new Map(history.map(alert => [alert.printer_id, alert])).values()]
    }
    const offset = Number(url.searchParams.get('offset') || 0)
    const limit = Number(url.searchParams.get('limit') || 25)
    return history.slice(offset, offset + limit)
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const { result, unmount } = renderHook(() => useFloorData(), { wrapper })
  try {
    await waitFor(() => expect(result.current.notices.isSuccess).toBe(true))
    expect(mocks.request).toHaveBeenCalledTimes(1)
    expect(mocks.request).toHaveBeenCalledWith('/alerts?needs_you=true')
    expect(result.current.notices.data).toHaveLength(101)
    expect(result.current.notices.data?.map(alert => alert.printer_id)).toEqual(Array.from({ length: 101 }, (_, i) => i + 1))
    await act(async () => { await result.current.notices.refetch() })
    expect(mocks.request).toHaveBeenCalledTimes(2)
    expect(result.current.notices.data).toHaveLength(101)
  } finally {
    unmount()
    client.clear()
  }
})
