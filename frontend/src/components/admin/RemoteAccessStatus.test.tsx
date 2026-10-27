import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import RemoteAccessStatus from './RemoteAccessStatus'
import wire from '../../test-fixtures/remote-access.json'
const getRemoteAccess = vi.hoisted(() => vi.fn())
vi.mock('../../api', () => ({ config: { getRemoteAccess } }))
beforeEach(() => {
  getRemoteAccess.mockReset()
  vi.stubGlobal('fetch', vi.fn())
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})
test('shows a DNS URL without automatic cross-origin requests', async () => {
  getRemoteAccess.mockResolvedValue(wire)
  render(<RemoteAccessStatus />)
  expect(await screen.findByRole('link')).toHaveAttribute('href', wire.url)
  expect(screen.getByRole('status')).toHaveTextContent('Open the server URL to check connectivity.')
  expect(fetch).not.toHaveBeenCalled()
})
test('reports current-origin verification without a health probe', async () => {
  getRemoteAccess.mockResolvedValue({ ...wire, current_origin_verified: true })
  render(<RemoteAccessStatus />)
  await screen.findByText('Connected through the configured remote URL.')
  expect(fetch).not.toHaveBeenCalled()
})
test('unconfigured status neither displays stale URL fields nor tries network checks', async () => {
  getRemoteAccess.mockResolvedValue({ ...wire, configured: false })
  render(<RemoteAccessStatus />)
  await screen.findByText('Remote access is not configured.')
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
  expect(fetch).not.toHaveBeenCalled()
})
test.each([
  'https://100.101.102.103', 'https://127.0.0.1', 'https://[::1]',
  'https://[fd7a:115c:a1e0::1]', 'https://2130706433', 'https://0x7f000001',
  'https://127.1', 'https://0177.0.0.1', '127.0.0.1,::1',
  'https://user:password@odin.example.ts.net', 'http://odin.example.ts.net',
  'https://odin.example.ts.net/path', 'https://odin.example.ts.net?proxy=::1',
  'https://odin.example.ts.net#::1', 'javascript:alert(1)', null, {},
])('does not display or probe an unsafe URL: %s', async url => {
  getRemoteAccess.mockResolvedValue({ ...wire, url, check_url: 'https://[::1]/health' })
  const { container } = render(<RemoteAccessStatus />)
  await screen.findByText('Remote access URL is unavailable. Use an HTTPS hostname.')
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
  if (typeof url === 'string') expect(container.textContent).not.toContain(url)
  expect(fetch).not.toHaveBeenCalled()
})
test.each([null, undefined, {}, { configured: 'yes' }])('handles malformed status silently: %s', async data => {
  getRemoteAccess.mockResolvedValue(data)
  const error = vi.spyOn(console, 'error')
  render(<RemoteAccessStatus />)
  await screen.findByText('Remote access status unavailable.')
  expect(error).not.toHaveBeenCalled()
})
test('does not disclose error details or log them', async () => {
  getRemoteAccess.mockRejectedValue(new Error('trusted proxies 127.0.0.1,::1'))
  const error = vi.spyOn(console, 'error')
  render(<RemoteAccessStatus />)
  await screen.findByText('Remote access status unavailable.')
  expect(screen.queryByText(/trusted proxies/)).not.toBeInTheDocument()
  expect(screen.queryByText(/Set ODIN_PUBLIC_URL/)).not.toBeInTheDocument()
  expect(error).not.toHaveBeenCalled()
})
test('bounds a stalled settings request and ignores a late response', async () => {
  vi.useFakeTimers()
  let resolve!: (value: typeof wire) => void
  getRemoteAccess.mockReturnValue(new Promise(done => { resolve = done }))
  render(<RemoteAccessStatus />)
  await act(async () => { vi.advanceTimersByTime(10000) })
  expect(getRemoteAccess.mock.calls[0][0].aborted).toBe(true)
  expect(screen.getByRole('status')).toHaveTextContent('Remote access status unavailable.')
  await act(async () => resolve(wire))
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})
test('cancels settings loading on unmount', () => {
  getRemoteAccess.mockReturnValue(new Promise(() => {}))
  const { unmount } = render(<RemoteAccessStatus />)
  const signal = getRemoteAccess.mock.calls[0][0]
  unmount()
  expect(signal.aborted).toBe(true)
})
