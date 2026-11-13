import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import GeneralTab from '../../components/admin/GeneralTab'
import Calculator from './Calculator'

// Stand-in for the server rule: the pricing row also holds the interface mode, and only a save
// that carries pricing numbers marks pricing as set up.
const defaults = { spool_cost: 25, spool_weight: 1000, hourly_rate: 15, electricity_rate: 0.12, printer_wattage: 100, printer_cost: 300, printer_lifespan: 5000, packaging_cost: 0.45, failure_rate: 7, monthly_rent: 0, parts_per_month: 100, post_processing_min: 5, packing_min: 5, support_min: 5, default_margin: 50, other_costs: 0, ui_mode: 'advanced' }
const server = vi.hoisted(() => ({ row: null as Record<string, unknown> | null, saved: false, get: vi.fn(), update: vi.fn() }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ tier: 'community', isPro: false, max_printers: null, max_users: null, hasFeature: () => false }) }))
vi.mock('../../api', async importOriginal => ({ ...await importOriginal<object>(), pricingConfig: { get: server.get, update: server.update }, models: { list: async () => [] } }))

beforeEach(() => {
  server.row = null; server.saved = false
  server.get.mockImplementation(async () => ({ ...defaults, ...server.row, configured: server.saved }))
  server.update.mockImplementation(async (data: Record<string, unknown>) => {
    if (Object.keys(data).some(key => key !== 'ui_mode')) server.saved = true
    server.row = { ...defaults, ...server.row, ...data }
    return { ...server.row, configured: server.saved }
  })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('switching the interface mode on a fresh install sends the mode alone and leaves pricing not set up', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
  render(<QueryClientProvider client={client}><GeneralTab /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('button', { name: /^Simple / }))
  await waitFor(() => expect(server.update).toHaveBeenCalledTimes(1))
  expect(server.update).toHaveBeenCalledWith({ ui_mode: 'simple' })
  fireEvent.click(screen.getByRole('button', { name: /^Advanced / }))
  await waitFor(() => expect(server.update).toHaveBeenCalledTimes(2))
  expect(server.update).toHaveBeenLastCalledWith({ ui_mode: 'advanced' })
  expect(await server.get()).toMatchObject({ ui_mode: 'advanced', configured: false })
})

it('saving the pricing form sends the pricing numbers, marks pricing set up and refreshes the Models page copy', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  render(<QueryClientProvider client={client}><Calculator /></QueryClientProvider>)
  await screen.findByRole('heading', { name: 'Pricing Calculator' })
  expect(screen.getByRole('button', { name: 'Save Config' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Spool Cost'), { target: { value: '32' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save Config' }))
  await waitFor(() => expect(server.update).toHaveBeenCalledTimes(1))
  const sent = server.update.mock.calls[0][0]
  expect(sent).toMatchObject({ spool_cost: 32, hourly_rate: 15, default_margin: 50 })
  expect(sent).not.toHaveProperty('ui_mode')
  expect(await server.get()).toMatchObject({ spool_cost: 32, configured: true })
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['pricing-config'] }))
})
