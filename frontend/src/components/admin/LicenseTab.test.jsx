/* eslint-disable no-unused-vars */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render as rtlRender, screen, fireEvent, waitFor, act } from '@testing-library/react'

import { license as licenseApi } from '../../api'
import LicenseTab from './LicenseTab'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import publicWire from '../../test-fixtures/public-license-wire-owned.json'

beforeEach(() => vi.clearAllMocks())
let currentClient
function render(node) {
  currentClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  currentClient.setQueryData(['auth', 'me'], { id: 3, role: 'admin', group_id: 1 })
  return rtlRender(<QueryClientProvider client={currentClient}>{node}</QueryClientProvider>)
}

vi.mock('../../api', () => ({
  license: {
    get: vi.fn(),
    activate: vi.fn(),
    upload: vi.fn(),
    remove: vi.fn(),
    getActivationRequest: vi.fn(),
    getInstallationId: vi.fn(),
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

it.each([null, 0, 2, 374, 9999])('renders actual resolved cap %s without sentinel conversion', async cap => {
  licenseApi.get.mockResolvedValue({ tier: 'pro', valid: true, features: [], max_printers: cap, max_users: cap })
  render(<LicenseTab />)
  const label = await screen.findByText('Max printers:')
  expect(label.parentElement).toHaveTextContent(`Max printers:${cap === null ? 'Unlimited' : cap}`)
  expect(screen.getByText('Max users:').parentElement).toHaveTextContent(`Max users:${cap === null ? 'Unlimited' : cap}`)
})

it('renders the expiry from the actual public HTTP wire without timezone conversion', async () => {
  licenseApi.get.mockResolvedValue(publicWire.body)
  render(<LicenseTab />)
  expect(await screen.findByText('2099-01-01')).toBeInTheDocument()
  expect(screen.getByText('Expires:').parentElement).toHaveTextContent('Expires:2099-01-01')
})

it('loads installation identity only through an explicit authenticated action', async () => {
  licenseApi.get.mockResolvedValue(publicWire.body)
  licenseApi.getInstallationId.mockResolvedValue({ installation_id: 'owned-synthetic-installation' })
  render(<LicenseTab />)
  await screen.findByText('2099-01-01')
  expect(licenseApi.getInstallationId).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  expect(await screen.findByText('owned-synthetic-installation')).toBeInTheDocument()
  expect(licenseApi.getInstallationId).toHaveBeenCalledOnce()
})

it('does not claim clipboard success before completion or after rejection', async () => {
  licenseApi.get.mockResolvedValue(publicWire.body)
  licenseApi.getInstallationId.mockResolvedValue({ installation_id: 'owned-synthetic-installation' })
  let rejectCopy
  const writeText = vi.fn(() => new Promise((resolve, reject) => { rejectCopy = reject }))
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  render(<LicenseTab />)
  await screen.findByText('2099-01-01')
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  await screen.findByText('owned-synthetic-installation')
  fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
  expect(writeText).toHaveBeenCalledExactlyOnceWith('owned-synthetic-installation')
  expect(screen.queryByText('Installation ID copied to clipboard')).not.toBeInTheDocument()
  rejectCopy(new Error('Synthetic clipboard refused'))
  await screen.findByText('Could not copy. Select the installation ID and copy it manually.')
  expect(screen.queryByText('Installation ID copied to clipboard')).not.toBeInTheDocument()
})

it.each([{ id:4, role:'viewer', group_id:1 }, { id:4, role:'admin', group_id:1 }, { id:3, role:'admin', group_id:2 }])('retires a late identity response after a cache-only principal switch %j', async nextPrincipal => {
  licenseApi.get.mockResolvedValue(publicWire.body)
  let resolveId
  licenseApi.getInstallationId.mockImplementation(() => new Promise(resolve => { resolveId = resolve }))
  render(<LicenseTab />)
  await screen.findByText('2099-01-01')
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  await act(async () => currentClient.setQueryData(['auth', 'me'], nextPrincipal))
  await act(async () => resolveId({ installation_id: 'retired-synthetic-installation' }))
  expect(screen.queryByText('retired-synthetic-installation')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
})

it('reports clipboard success only after the write resolves', async () => {
  licenseApi.get.mockResolvedValue(publicWire.body)
  licenseApi.getInstallationId.mockResolvedValue({ installation_id: 'owned-synthetic-installation' })
  let finishCopy
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn(() => new Promise(resolve => { finishCopy = resolve })) } })
  render(<LicenseTab />)
  await screen.findByText('2099-01-01')
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  await screen.findByText('owned-synthetic-installation')
  fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
  expect(screen.getByRole('button', { name: 'Copy' })).toBeDisabled()
  expect(screen.queryByText('Installation ID copied to clipboard')).not.toBeInTheDocument()
  await act(async () => finishCopy())
  expect(await screen.findByText('Installation ID copied to clipboard')).toBeInTheDocument()
})

it('supports manual copy when clipboard is unavailable', async () => {
  licenseApi.get.mockResolvedValue(publicWire.body)
  licenseApi.getInstallationId.mockResolvedValue({ installation_id: 'owned-synthetic-installation' })
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
  render(<LicenseTab />)
  await screen.findByText('2099-01-01')
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  await screen.findByText('owned-synthetic-installation')
  fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
  expect(await screen.findByText('Could not copy. Select the installation ID and copy it manually.')).toBeInTheDocument()
})

it('allows retry after identity request failure', async () => {
  licenseApi.get.mockResolvedValue(publicWire.body)
  licenseApi.getInstallationId.mockRejectedValueOnce(new Error('Synthetic denied')).mockResolvedValueOnce({ installation_id: 'owned-synthetic-installation' })
  render(<LicenseTab />)
  await screen.findByText('2099-01-01')
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  await screen.findByText('Could not load installation ID. Try again.')
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  expect(await screen.findByText('owned-synthetic-installation')).toBeInTheDocument()
  expect(licenseApi.getInstallationId).toHaveBeenCalledTimes(2)
})

it.each([false, true])('retires late clipboard result after cache-only role change, rejection=%s', async rejected => {
  licenseApi.get.mockResolvedValue(publicWire.body)
  licenseApi.getInstallationId.mockResolvedValue({ installation_id: 'owned-synthetic-installation' })
  let finishCopy, rejectCopy
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn(() => new Promise((resolve, reject) => { finishCopy = resolve; rejectCopy = reject })) } })
  render(<LicenseTab />)
  await screen.findByText('2099-01-01')
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  await screen.findByText('owned-synthetic-installation')
  fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
  await act(async () => currentClient.setQueryData(['auth', 'me'], { id:3, role:'viewer', group_id:1 }))
  await act(async () => rejected ? rejectCopy(new Error('Synthetic refused')) : finishCopy())
  await waitFor(() => expect(screen.queryByText('owned-synthetic-installation')).not.toBeInTheDocument())
  expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  expect(screen.queryByText('Installation ID copied to clipboard')).not.toBeInTheDocument()
  expect(screen.queryByText('Could not copy. Select the installation ID and copy it manually.')).not.toBeInTheDocument()
})

it('ignores late identity rejection after account change and refuses duplicate pending clicks', async () => {
  licenseApi.get.mockResolvedValue(publicWire.body)
  let rejectId
  licenseApi.getInstallationId.mockImplementation(() => new Promise((resolve, reject) => { rejectId = reject }))
  render(<LicenseTab />)
  await screen.findByText('2099-01-01')
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  expect(licenseApi.getInstallationId).toHaveBeenCalledOnce()
  await act(async () => currentClient.setQueryData(['auth', 'me'], { id:4, role:'admin', group_id:1 }))
  await act(async () => rejectId(new Error('Synthetic late denied')))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Show installation ID' })).not.toBeDisabled())
  expect(screen.queryByText('Could not load installation ID. Try again.')).not.toBeInTheDocument()
})

it('retires identity response on unmount', async () => {
  licenseApi.get.mockResolvedValue(publicWire.body)
  let resolveId
  licenseApi.getInstallationId.mockImplementation(() => new Promise(resolve => { resolveId = resolve }))
  const view = render(<LicenseTab />)
  await screen.findByText('2099-01-01')
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  view.unmount()
  render(<LicenseTab />)
  await act(async () => resolveId({ installation_id:'retired-synthetic-installation' }))
  expect(screen.queryByText('retired-synthetic-installation')).not.toBeInTheDocument()
})

it.each([undefined, 0, -1, 1.5])('does not admit malformed cached admin ID %s', async id => {
  licenseApi.get.mockResolvedValue(publicWire.body)
  render(<LicenseTab />)
  await act(async () => currentClient.setQueryData(['auth', 'me'], { id, role:'admin', group_id:1 }))
  await waitFor(() => expect(screen.queryByRole('button', { name:'Show installation ID' })).not.toBeInTheDocument())
  expect(licenseApi.getInstallationId).not.toHaveBeenCalled()
})
