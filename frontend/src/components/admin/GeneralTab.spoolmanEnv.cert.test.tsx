// GET /api/config reports spoolman_url_from_env when SPOOLMAN_URL is set in the server environment; the server
// then refuses to change the saved URL (409). Saving scheduling settings must not send the URL in that case.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import GeneralTab from './GeneralTab'
const api = vi.hoisted(() => ({ update: vi.fn(async (d: unknown) => d), config: {} as Record<string, unknown> }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ tier: 'pro', isPro: true, max_printers: null, max_users: null, hasFeature: () => false }) }))
vi.mock('../../api', async importOriginal => ({ ...await importOriginal(), pricingConfig: { get: async () => ({}) },
  config: { get: async () => api.config, update: api.update, getNetwork: async () => ({}), testSpoolman: vi.fn() } }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['stats'], { printers: { total: 1 } })
  return render(<QueryClientProvider client={client}><GeneralTab /></QueryClientProvider>)
}

it('environment-managed Spoolman URL is shown read-only and left out of the save', async () => {
  api.config = { spoolman_url: 'http://10.0.0.5:7912', spoolman_url_from_env: true, blackout_start: '21:00', blackout_end: '06:00' }
  mount()
  const input = await screen.findByDisplayValue('http://10.0.0.5:7912')
  expect(input).toBeDisabled()
  expect(screen.getByText(/SPOOLMAN_URL environment variable/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Save Scheduling Settings/ }))
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ blackout_start: '21:00', blackout_end: '06:00' }))
})

it('a saved Spoolman URL is editable and saved with the blackout hours', async () => {
  api.config = { spoolman_url: 'http://10.0.0.6:7912', spoolman_url_from_env: false, blackout_start: '22:30', blackout_end: '05:30' }
  mount()
  const input = await screen.findByDisplayValue('http://10.0.0.6:7912')
  expect(input).not.toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: /Save Scheduling Settings/ }))
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ spoolman_url: 'http://10.0.0.6:7912', blackout_start: '22:30', blackout_end: '05:30' }))
})
