/* eslint-disable no-unused-vars */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

import { license as licenseApi } from '../../api'
import LicenseTab from './LicenseTab'

vi.mock('../../api', () => ({
  license: {
    get: vi.fn(),
    activate: vi.fn(),
    upload: vi.fn(),
    remove: vi.fn(),
    getActivationRequest: vi.fn(),
  },
  downloadBlob: vi.fn(),
}))

describe('LicenseTab managed sandbox state', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows externally managed state without mutation controls', async () => {
    licenseApi.get.mockResolvedValue({
      tier: 'education',
      valid: true,
      features: ['job_approval', 'user_groups', 'print_quotas'],
      max_printers: 20,
      max_users: 500,
      managed_externally: true,
    })
    render(<LicenseTab />)

    expect(await screen.findByText('Externally Managed License')).toBeInTheDocument()
    expect(screen.queryByPlaceholderText('ODIN-XXXX-XXXX-XXXX')).not.toBeInTheDocument()
    expect(screen.queryByText(/upload license file/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/remove license/i)).not.toBeInTheDocument()
  })
})
