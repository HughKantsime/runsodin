import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { renderWithCurrency as render } from '../../test/renderWithCurrency'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const api = vi.hoisted(() => ({ get: vi.fn(), update: vi.fn(), list: vi.fn() }))
vi.mock('../../api', () => ({ models: { list: api.list }, pricingConfig: { get: api.get, update: api.update } }))
vi.mock('react-hot-toast', () => ({ default: { error: vi.fn(), success: vi.fn() } }))

import CalculatorPage from './Calculator'
import toast from 'react-hot-toast'

// The configuration every install has until someone saves one; no 2.3.0 keys in it.
const SAVED = { spool_cost: 25, spool_weight: 1000, hourly_rate: 15, electricity_rate: 0.12, printer_wattage: 100, printer_cost: 300,
  printer_lifespan: 5000, packaging_cost: 0.45, failure_rate: 7, monthly_rent: 0, parts_per_month: 100, post_processing_min: 5,
  packing_min: 5, support_min: 5, default_margin: 50, other_costs: 0, configured: true }

async function show(saved: Record<string, unknown> = SAVED) {
  api.get.mockResolvedValue(saved)
  api.list.mockResolvedValue([])
  api.update.mockResolvedValue(saved)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><CalculatorPage /></QueryClientProvider>)
  await screen.findByText('Final Pricing')
}
const price = () => screen.getByTestId('recommended-price').textContent
const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement

beforeEach(() => { vi.clearAllMocks() })
afterEach(cleanup)

describe('Pricing Calculator', () => {
  it('rounds computed cost per gram to three decimal places', async () => {
    await show({ ...SAVED, spool_cost: 20, spool_weight: 750 })
    expect(await screen.findByText('Cost per gram: EUR 0.027')).toBeInTheDocument()
  })

  it('a configuration saved before this change shows the price it always showed', async () => {
    await show()
    // 50 g of a $25/kg spool, 2 h, defaults: cost 5.99, +50% = 8.98 (unchanged from 2.2.0).
    for (const label of ['Hourly Rate', 'Fixed fee']) {
      expect(field(label).parentElement!.querySelector('svg')).toHaveClass('lucide-coins')
    }
    expect(price()).toBe('EUR 8.98')
    expect(screen.getByTestId('markup-percent').textContent).toBe('50%')
    expect(screen.getByTestId('margin-of-price').textContent).toContain('33.3%')
    expect(screen.queryByText('Selling fees:')).toBeNull()
  })

  it('calls the percentage a markup, never a margin, and shows the margin beside the price', async () => {
    await show()
    expect(field('Markup').value).toBe('50')
    expect(screen.getByText('added on top of cost')).toBeInTheDocument()
    expect(screen.queryByText('Manufacturing Margin')).toBeNull()
    expect(screen.queryByLabelText('Margin')).toBeNull()
    expect(field('Target margin').value).toBe('33.3')
  })

  it('target margin and markup are one setting: changing one moves the other and the price', async () => {
    await show()
    fireEvent.change(field('Target margin'), { target: { value: '50' } })
    expect(field('Markup').value).toBe('100')
    expect(price()).toBe('EUR 11.97')
    fireEvent.change(field('Markup'), { target: { value: '25' } })
    expect(field('Target margin').value).toBe('20')
  })

  it('refuses a target margin of 100% and leaves the markup alone', async () => {
    await show()
    fireEvent.change(field('Target margin'), { target: { value: '100' } })
    expect(toast.error).toHaveBeenCalledWith('Target margin must be 0 or more and under 100%')
    expect(field('Markup').value).toBe('50')
  })

  it('selling fees raise the price so the markup still applies to cost, and get their own line', async () => {
    await show()
    fireEvent.change(field('Selling fee'), { target: { value: '10' } })
    expect(price()).toBe('EUR 9.98')                       // 8.98 / 0.9
    expect(screen.getByText('Selling fees:')).toBeInTheDocument()
    expect(screen.getByTestId('margin-of-price').textContent).toContain('after selling fees')
    fireEvent.change(field('Fixed fee'), { target: { value: '0.3' } })
    expect(price()).toBe('EUR 10.31')                      // (8.98 + 0.30) / 0.9
  })

  it('a selling fee of 100% is refused: warning shown, nothing saved', async () => {
    await show()
    fireEvent.change(field('Selling fee'), { target: { value: '100' } })
    expect(screen.getByRole('alert').textContent).toBe('Selling fee must be under 100%.')
    fireEvent.click(screen.getByRole('button', { name: /Save Config/ }))
    expect(api.update).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledWith('Selling fee must be under 100%')
  })

  it('plate handling is shared between the units on a plate', async () => {
    await show()
    fireEvent.change(field('Plate handling'), { target: { value: '4' } })
    const one = price()                                   // 4 min at 15/h = 1.00 more cost
    expect(one).toBe('EUR 10.58')
    fireEvent.change(field('Units per Bed'), { target: { value: '4' } })
    fireEvent.change(field('Units per Bed'), { target: { value: '1' } })
    expect(price()).toBe(one)
  })

  it('saves the three new settings with the rest and keeps the stored name of the markup', async () => {
    await show()
    fireEvent.change(field('Selling fee'), { target: { value: '2.9' } })
    fireEvent.change(field('Fixed fee'), { target: { value: '0.3' } })
    fireEvent.change(field('Plate handling'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: /Save Config/ }))
    await waitFor(() => expect(api.update).toHaveBeenCalledTimes(1))
    expect(api.update.mock.calls[0][0]).toMatchObject({ default_margin: 50, selling_fee_percent: 2.9, selling_fee_fixed: 0.3, plate_handling_min: 3 })
  })

  it('loads saved fees and plate handling', async () => {
    await show({ ...SAVED, selling_fee_percent: 6.5, selling_fee_fixed: 0.2, plate_handling_min: 2 })
    expect(field('Selling fee').value).toBe('6.5')
    expect(field('Fixed fee').value).toBe('0.2')
    expect(field('Plate handling').value).toBe('2')
  })
})
