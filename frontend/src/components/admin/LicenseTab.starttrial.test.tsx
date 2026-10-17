import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import LicenseTab from './LicenseTab'
import publicWire from '../../test-fixtures/public-license-wire-owned.json'
import wire from '../../test-fixtures/trial-license-wire.json'

const api = vi.hoisted(() => ({ get: vi.fn(), requestTrial: vi.fn() }))
vi.mock('../../api', () => ({
  license: { get: api.get, requestTrial: api.requestTrial, activate: vi.fn(), upload: vi.fn(), remove: vi.fn(), getActivationRequest: vi.fn(), getInstallationId: vi.fn() },
  downloadBlob: vi.fn(),
}))
const COMMUNITY = { ...publicWire.body, valid: false, tier: 'community', tier_name: 'Community', expires_at: null, features: ['push_notifications'], max_printers: 5, max_users: 1 }
beforeEach(() => { api.get.mockResolvedValue(COMMUNITY) })
afterEach(() => { cleanup(); vi.clearAllMocks() })

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 1, role: 'admin', group_id: null })
  render(<QueryClientProvider client={client}><LicenseTab /></QueryClientProvider>)
}

test('Community: "Start 14-day Pro trial" sends a device-signed request for the email and says check your email', async () => {
  api.requestTrial.mockResolvedValue({ status: 'request_sent' })
  renderTab()
  const heading = await screen.findByRole('heading', { name: 'Start 14-day Pro trial' })
  expect(heading).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Trial email'), { target: { value: 'owner@example.com' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send trial request' }))
  await waitFor(() => expect(api.requestTrial).toHaveBeenCalledExactlyOnceWith('owner@example.com', false))
  expect(await screen.findByText(/Check owner@example.com for a confirmation link/)).toBeInTheDocument()
})

test('offline: "Get request code" shows the code to paste on the portal', async () => {
  api.requestTrial.mockResolvedValue({ request_code: 'ODINTRIAL1.eyJ2IjoxfQ', expires_in_minutes: 15 })
  renderTab()
  fireEvent.change(await screen.findByLabelText('Trial email'), { target: { value: 'school@example.org' } })
  fireEvent.click(screen.getByRole('button', { name: 'Get request code' }))
  await waitFor(() => expect(api.requestTrial).toHaveBeenCalledExactlyOnceWith('school@example.org', true))
  expect(await screen.findByLabelText('Trial request code')).toHaveValue('ODINTRIAL1.eyJ2IjoxfQ')
  expect(screen.getByText(/within 15 minutes/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'runsodin.com/portal/trial' })).toHaveAttribute('href', 'https://runsodin.com/portal/trial')
})

test('errors from the server are shown', async () => {
  api.requestTrial.mockRejectedValue(new Error('Could not reach the license server.'))
  renderTab()
  fireEvent.change(await screen.findByLabelText('Trial email'), { target: { value: 'owner@example.com' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send trial request' }))
  expect(await screen.findByText(/Could not reach the license server/)).toBeInTheDocument()
})

test.each([['a running trial', wire.active_license], ['a paid license', publicWire.body]])('no trial start with %s', async (_n, body) => {
  api.get.mockResolvedValue(body)
  renderTab()
  await screen.findByText('Max printers:')
  expect(screen.queryByRole('heading', { name: 'Start 14-day Pro trial' })).not.toBeInTheDocument()
})
