import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import LicenseTab from './LicenseTab'
// Actual /api/license bodies captured by tests/test_contracts/test_trial_license.py::test_trial_wire_capture.
import wire from '../../test-fixtures/trial-license-wire.json'
import publicWire from '../../test-fixtures/public-license-wire-owned.json'

const api = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../api', () => ({
  license: { get: api.get, activate: vi.fn(), upload: vi.fn(), remove: vi.fn(), getActivationRequest: vi.fn(), getInstallationId: vi.fn() },
  downloadBlob: vi.fn(),
}))
afterEach(() => { cleanup(); vi.clearAllMocks() })

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 1, role: 'admin', group_id: null })
  render(<QueryClientProvider client={client}><LicenseTab /></QueryClientProvider>)
}

test('a running trial shows "Pro trial — N days left" with a buy link', async () => {
  api.get.mockResolvedValue(wire.active_license)
  renderTab()
  const status = await screen.findByTestId('license-trial-status')
  expect(status).toHaveTextContent('Pro trial — 14 days left')
  expect(status).toHaveTextContent('Your data is kept when it ends.')
  expect(screen.getByRole('link', { name: 'Buy a license' })).toHaveAttribute('href', 'https://runsodin.com/pricing')
})

test.each([[3, 'Pro trial — 3 days left'], [1, 'Pro trial — 1 day left']])('days left %i reads "%s"', async (days, text) => {
  api.get.mockResolvedValue({ ...wire.active_license, trial_days_left: days })
  renderTab()
  expect(await screen.findByTestId('license-trial-status')).toHaveTextContent(text)
})

test('an ended trial shows "Trial ended", keeps data and offers a buy link', async () => {
  api.get.mockResolvedValue(wire.ended_license)
  renderTab()
  const status = await screen.findByTestId('license-trial-status')
  expect(status).toHaveTextContent('Trial ended')
  expect(status).not.toHaveTextContent('days left')
  expect(status).toHaveTextContent('Your data is kept: you can still view, export and delete it.')
  expect(screen.getByRole('link', { name: 'Buy a license' })).toHaveAttribute('href', 'https://runsodin.com/pricing')
})

test('a paid license shows no trial state', async () => {
  api.get.mockResolvedValue(publicWire.body)
  renderTab()
  await screen.findByText('Max printers:')
  expect(screen.queryByTestId('license-trial-status')).not.toBeInTheDocument()
})
