import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { LicenseProvider, useLicense } from './LicenseContext'
import UpgradeBanner from './components/shared/UpgradeBanner'
// Actual /api/license bodies captured by tests/test_contracts/test_trial_license.py::test_trial_wire_capture.
import wire from './test-fixtures/trial-license-wire.json'

const api = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('./api', () => ({ license: { get: api.get } }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

function Probe() {
  const l = useLicense()
  return <span data-testid="flags">{JSON.stringify([l.loading, l.isPro, l.isEducation, l.isEnterprise, l.tier])}</span>
}

test('O5: an ended trial (tier pro, valid false) is not Pro in the UI and shows the upgrade banner', async () => {
  expect(wire.ended_license.tier).toBe('pro')
  expect(wire.ended_license.valid).toBe(false)
  api.get.mockResolvedValue(wire.ended_license)
  render(<LicenseProvider><Probe /><UpgradeBanner /></LicenseProvider>)
  await waitFor(() => expect(JSON.parse(screen.getByTestId('flags').textContent!)[0]).toBe(false))
  expect(JSON.parse(screen.getByTestId('flags').textContent!)).toEqual([false, false, false, false, 'pro'])
  expect(screen.getByText(/Upgrade to Pro for unlimited/)).toBeInTheDocument()
})

test.each([
  ['education', true, true, false], ['enterprise', true, true, true],
])('O5: an expired %s license is not that tier in the UI', async (tier) => {
  api.get.mockResolvedValue({ ...wire.ended_license, tier, trial: false, trial_days_left: null })
  render(<LicenseProvider><Probe /></LicenseProvider>)
  await waitFor(() => expect(JSON.parse(screen.getByTestId('flags').textContent!)[0]).toBe(false))
  expect(JSON.parse(screen.getByTestId('flags').textContent!).slice(1, 4)).toEqual([false, false, false])
})

test('O5: a running trial is Pro and hides the upgrade banner', async () => {
  api.get.mockResolvedValue(wire.active_license)
  render(<LicenseProvider><Probe /><UpgradeBanner /></LicenseProvider>)
  await waitFor(() => expect(JSON.parse(screen.getByTestId('flags').textContent!)[1]).toBe(true))
  expect(screen.queryByText(/Upgrade to Pro for unlimited/)).not.toBeInTheDocument()
})
