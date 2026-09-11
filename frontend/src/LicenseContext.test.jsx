/* eslint-disable no-unused-vars */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { fetchAPI } from './api'
import { LicenseProvider, featuresFromLicense, useLicense } from './LicenseContext'

vi.mock('./api', () => ({ fetchAPI: vi.fn() }))

function Probe() {
  const license = useLicense()
  return (
    <div>
      <span data-testid="tier">{license.tier}</span>
      <span data-testid="quota">{String(license.hasFeature('print_quotas'))}</span>
      <span data-testid="groups">{String(license.hasFeature('user_groups'))}</span>
      <span data-testid="class-sections">{String(license.hasFeature('class_sections'))}</span>
      <span data-testid="max-users">{String(license.maxUsers)}</span>
      <span data-testid="at-limit">{String(license.atUserLimit(1))}</span>
      <span data-testid="managed">{String(license.managed_externally)}</span>
      <span data-testid="loading">{String(license.loading)}</span>
      <button onClick={license.refresh}>Refresh license</button>
    </div>
  )
}

describe('LicenseProvider effective feature contract', () => {
  beforeEach(() => vi.clearAllMocks())

  it('uses API-provided Education entitlements and limits', async () => {
    fetchAPI.mockResolvedValue({
      tier: 'education',
      max_users: 500,
      max_printers: 20,
      features: ['job_approval', 'user_groups', 'print_quotas', 'usage_reports'],
      managed_externally: true,
    })
    render(<LicenseProvider><Probe /></LicenseProvider>)

    await waitFor(() => expect(screen.getByTestId('tier')).toHaveTextContent('education'))
    expect(screen.getByTestId('quota')).toHaveTextContent('true')
    expect(screen.getByTestId('groups')).toHaveTextContent('true')
    expect(screen.getByTestId('class-sections')).toHaveTextContent('false')
    expect(screen.getByTestId('max-users')).toHaveTextContent('500')
    expect(screen.getByTestId('managed')).toHaveTextContent('true')
  })

  it('honors the backend Community max_users value', async () => {
    fetchAPI.mockResolvedValue({
      tier: 'community', max_users: 1, max_printers: 5, features: [],
    })
    render(<LicenseProvider><Probe /></LicenseProvider>)
    await waitFor(() => expect(screen.getByTestId('at-limit')).toHaveTextContent('true'))
    expect(screen.getByTestId('max-users')).toHaveTextContent('1')
  })

  it('does not reconstruct features from the tier name', () => {
    expect(featuresFromLicense(undefined).size).toBe(0)
    expect(featuresFromLicense(['print_quotas']).has('print_quotas')).toBe(true)
    expect(featuresFromLicense([]).has('class_sections')).toBe(false)
  })

  it('clears stale entitlements when a refresh fails', async () => {
    fetchAPI
      .mockResolvedValueOnce({
        tier: 'education', max_users: 500, max_printers: 20,
        features: ['print_quotas'], managed_externally: true,
      })
      .mockRejectedValueOnce(new Error('offline'))

    render(<LicenseProvider><Probe /></LicenseProvider>)
    await waitFor(() => expect(screen.getByTestId('tier')).toHaveTextContent('education'))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh license' }))

    await waitFor(() => expect(screen.getByTestId('managed')).toHaveTextContent('false'))
    expect(screen.getByTestId('tier')).toHaveTextContent('community')
    expect(screen.getByTestId('quota')).toHaveTextContent('false')
    expect(screen.getByTestId('max-users')).toHaveTextContent('1')
    expect(screen.getByTestId('loading')).toHaveTextContent('false')
  })
})
