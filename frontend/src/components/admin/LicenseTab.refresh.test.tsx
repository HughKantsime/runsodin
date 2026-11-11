import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import LicenseTab from './LicenseTab'
import publicWire from '../../test-fixtures/public-license-wire-owned.json'
import { LICENSE_REFRESH_EVENT } from '../../api/licenseEvents'

const api = vi.hoisted(() => ({ get: vi.fn(), reactivate: vi.fn() }))
vi.mock('../../api', () => ({
  license: { get: api.get, activate: vi.fn(), upload: vi.fn(), remove: vi.fn(), getActivationRequest: vi.fn(), getInstallationId: vi.fn() },
  downloadBlob: vi.fn(),
}))
vi.mock('../../api/licenseRefresh', () => ({ licenseRefresh: { reactivate: api.reactivate } }))

const paid = {
  ...publicWire.body, tier: 'pro', tier_name: 'Pro', valid: true, expired: false, trial: false,
  managed_externally: false, license_sha256: 'a'.repeat(64), expires_at: '2026-11-30T00:00:00Z',
}
const answer = (refresh_status: string, over: Record<string, unknown> = {}) => ({
  status: 'reactivated', refresh_status, tier: 'pro', valid: true, expires_at: '2026-11-30T00:00:00Z',
  last_refresh: { status: refresh_status, at: '2026-11-11T12:00:00Z', expires_at: '2026-11-30T00:00:00Z' }, ...over,
})

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(Date.parse('2026-11-11T12:00:00Z'))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks() })

function renderTab(role = 'admin') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 1, role, group_id: null })
  render(<QueryClientProvider client={client}><LicenseTab /></QueryClientProvider>)
}

test('an administrator sees one Refresh license button; a click shows the portal answer in plain words', async () => {
  api.get.mockResolvedValue(paid)
  api.reactivate.mockResolvedValue(answer('renewed', { expires_at: '2026-12-14T00:00:00Z' }))
  const reloaded = vi.fn()
  window.addEventListener(LICENSE_REFRESH_EVENT, reloaded)
  renderTab()
  const button = await screen.findByRole('button', { name: 'Refresh license' })
  expect(screen.getAllByRole('button', { name: 'Refresh license' })).toHaveLength(1)
  expect(screen.queryByTestId('license-renewal-banner')).not.toBeInTheDocument()
  fireEvent.click(button)
  expect(await screen.findByTestId('license-refresh-result')).toHaveTextContent('License refreshed. Pro is active until December 14, 2026.')
  expect(api.reactivate).toHaveBeenCalledTimes(1)
  expect(reloaded).toHaveBeenCalled()
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
  window.removeEventListener(LICENSE_REFRESH_EVENT, reloaded)
})

test.each([
  ['a non-administrator', 'operator', paid],
  ['no license installed', 'admin', { ...paid, valid: false, license_sha256: '', tier: 'community', expires_at: null }],
  ['a trial', 'admin', { ...paid, trial: true }],
  ['an externally managed license', 'admin', { ...paid, managed_externally: true }],
])('no Refresh license button for %s', async (_name, role, lic) => {
  api.get.mockResolvedValue(lic)
  renderTab(role)
  await screen.findByText('Max printers:')
  expect(screen.queryByRole('button', { name: 'Refresh license' })).not.toBeInTheDocument()
  expect(screen.queryByTestId('license-renewal-banner')).not.toBeInTheDocument()
})

test('no banner 8 days before the end', async () => {
  api.get.mockResolvedValue({ ...paid, expires_at: '2026-11-20T00:00:00Z' })
  renderTab()
  await screen.findByRole('button', { name: 'Refresh license' })
  expect(screen.queryByTestId('license-renewal-banner')).not.toBeInTheDocument()
})

test('from 7 days before the end a Renewal due banner holds the only Refresh license button', async () => {
  api.get.mockResolvedValue({ ...paid, expires_at: '2026-11-17T00:00:00Z' })
  renderTab()
  const banner = await screen.findByTestId('license-renewal-banner')
  expect(banner).toHaveAttribute('data-state', 'due')
  expect(banner).toHaveTextContent('Renewal due')
  expect(banner).toHaveTextContent('Your Pro license file ends on November 17, 2026.')
  expect(within(banner).getByRole('button', { name: 'Refresh license' })).toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: 'Refresh license' })).toHaveLength(1)
})

test('the stored last answer shows on load, before any click', async () => {
  api.get.mockResolvedValue({ ...paid, last_refresh: answer('canceled').last_refresh })
  renderTab()
  const banner = await screen.findByTestId('license-renewal-banner')
  expect(banner).toHaveAttribute('data-state', 'canceled')
  expect(banner).toHaveTextContent('Pro features stop on November 30, 2026')
})

test.each([
  ['not_renewed_yet', 'has not renewed'],
  ['canceled', 'Pro features stop on November 30, 2026'],
  ['not_entitled', 'Pro has ended'],
])('%s: the answer is stored and the banner keeps showing it, with Refresh still available', async (status, text) => {
  const lastRefresh = answer(status).last_refresh
  api.get.mockResolvedValueOnce(paid).mockResolvedValue({ ...paid, valid: status !== 'not_entitled', last_refresh: lastRefresh })
  api.reactivate.mockResolvedValue(answer(status, { valid: status !== 'not_entitled' }))
  renderTab()
  fireEvent.click(await screen.findByRole('button', { name: 'Refresh license' }))
  const banner = await screen.findByTestId('license-renewal-banner')
  expect(banner).toHaveAttribute('data-state', status)
  expect(banner).toHaveTextContent(text)
  await waitFor(() => expect(within(banner).getByRole('button', { name: 'Refresh license' })).toBeEnabled())
})

test('not entitled links to support', async () => {
  api.get.mockResolvedValue({ ...paid, valid: false, expired: true, last_refresh: answer('not_entitled').last_refresh })
  renderTab()
  await screen.findByTestId('license-renewal-banner')
  expect(screen.getByRole('link', { name: 'support@runsodin.com' })).toHaveAttribute('href', 'mailto:support@runsodin.com')
})

test('an unreachable portal is reported without touching the license', async () => {
  api.get.mockResolvedValue(paid)
  api.reactivate.mockRejectedValue(new Error('Could not reach license server.'))
  renderTab()
  fireEvent.click(await screen.findByRole('button', { name: 'Refresh license' }))
  const result = await screen.findByTestId('license-refresh-result')
  expect(result).toHaveAttribute('role', 'alert')
  expect(result).toHaveTextContent('Could not reach the license portal. Try again later, or use offline activation. Your license keeps working until November 30, 2026.')
})

test('another failure shows the server message', async () => {
  api.get.mockResolvedValue(paid)
  api.reactivate.mockRejectedValue(new Error('This license file was issued before the one installed on this server, so it was not installed.'))
  renderTab()
  fireEvent.click(await screen.findByRole('button', { name: 'Refresh license' }))
  expect(await screen.findByTestId('license-refresh-result')).toHaveTextContent('Refresh failed: This license file was issued before the one installed')
})
