import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import RemoteAccessStatus from './RemoteAccessStatus'
import wire from '../../test-fixtures/remote-access.json'
const getRemoteAccess = vi.hoisted(() => vi.fn())
vi.mock('../../api', () => ({ config: { getRemoteAccess } }))
afterEach(() => vi.unstubAllGlobals())
test('shows backend URL and verifies ODIN from this browser without cookies', async () => {
  getRemoteAccess.mockResolvedValue(wire)
  const fetcher = vi.fn(async () => ({ ok: true, json: async () => ({ status: 'ok', version: 'fixture' }) }))
  vi.stubGlobal('fetch', fetcher)
  render(<RemoteAccessStatus />)
  await screen.findByText('ODIN answers at this HTTPS URL.')
  expect(screen.getByRole('link')).toHaveAttribute('href', wire.url)
  expect(fetcher).toHaveBeenCalledWith(wire.check_url, expect.objectContaining({ credentials: 'omit', redirect: 'error' }))
})
test('unconfigured status does not try network checks', async () => {
  getRemoteAccess.mockResolvedValue({ ...wire, configured: false, url: null, check_url: null })
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
  render(<RemoteAccessStatus />)
  await screen.findByText('Remote access is not configured.')
  expect(fetcher).not.toHaveBeenCalled()
})
test('failed check gives useful diagnostics', async () => {
  getRemoteAccess.mockResolvedValue(wire)
  vi.stubGlobal('fetch', vi.fn(async () => { throw new Error() }))
  render(<RemoteAccessStatus />)
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Could not verify from this browser'))
})
