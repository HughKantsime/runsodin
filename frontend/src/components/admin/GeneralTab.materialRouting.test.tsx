// Filament routing (scheduler_material_mode): GET /api/config reports it, the select saves it on its own
// (PUT /api/config with only that key), and the blackout "Save Scheduling Settings" payload is unchanged.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import GeneralTab from './GeneralTab'
const api = vi.hoisted(() => ({ update: vi.fn(async (d: unknown) => d), config: {} as Record<string, unknown> }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ tier: 'pro', isPro: true, max_printers: null, max_users: null, hasFeature: () => false }) }))
vi.mock('../../api', async importOriginal => ({ ...await importOriginal(), pricingConfig: { get: async () => ({}) },
  config: { get: async () => api.config, update: api.update, getRemoteAccess: vi.fn().mockResolvedValue({configured:false,url:null,check_url:null}), getNetwork: async () => ({}), testSpoolman: vi.fn() } }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['stats'], { printers: { total: 1 } })
  return render(<QueryClientProvider client={client}><GeneralTab /></QueryClientProvider>)
}

it('shows Require when the server sends no mode, and describes every option', async () => {
  api.config = { spoolman_url: '', blackout_start: '22:30', blackout_end: '05:30' }
  mount()
  const select = await screen.findByLabelText('Filament routing')
  expect(select).toHaveValue('require')
  expect(screen.getByText(/stays queued/)).toBeInTheDocument()
  expect(screen.getByText(/ranked ahead of colour matches/)).toBeInTheDocument()
  expect(screen.getByText(/ignores material and scores colour only/)).toBeInTheDocument()
})

it('shows the saved mode', async () => {
  api.config = { spoolman_url: '', blackout_start: '22:30', blackout_end: '05:30', scheduler_material_mode: 'prefer' }
  mount()
  await waitFor(() => expect(screen.getByLabelText('Filament routing')).toHaveValue('prefer'))
})

it('a change saves by itself with only the mode', async () => {
  api.config = { spoolman_url: '', blackout_start: '22:30', blackout_end: '05:30', scheduler_material_mode: 'require' }
  mount()
  const select = await screen.findByLabelText('Filament routing')
  fireEvent.change(select, { target: { value: 'off' } })
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ scheduler_material_mode: 'off' }))
  expect(api.update).toHaveBeenCalledTimes(1)
})

it('Save Scheduling Settings does not resend the mode', async () => {
  api.config = { spoolman_url: 'http://10.0.0.6:7912', spoolman_url_from_env: false, blackout_start: '22:30', blackout_end: '05:30', scheduler_material_mode: 'off' }
  mount()
  await screen.findByDisplayValue('http://10.0.0.6:7912')
  fireEvent.click(screen.getByRole('button', { name: /Save Scheduling Settings/ }))
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ spoolman_url: 'http://10.0.0.6:7912', blackout_start: '22:30', blackout_end: '05:30' }))
})
