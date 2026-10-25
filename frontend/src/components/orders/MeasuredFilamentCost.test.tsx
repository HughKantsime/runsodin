import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import MeasuredFilamentCost, { MeasuredFilamentView } from './MeasuredFilamentCost'
import { orders, type OrderMeasuredCosts } from '../../api/orders'

vi.mock('../../api/orders', () => ({ orders: { measuredCosts: vi.fn() } }))
const base: OrderMeasuredCosts = {
  measured_filament_known_subtotal: '0.00', currency: 'USD', filament_state: 'fully_measured',
  production_cogs_state: 'unavailable', known_attempts: 1, unresolved_attempts: 0, open_attempts: 0,
  reasons: [], successful_subtotal: '0.00', failed_cancelled_subtotal: null,
  attempts: [{ attempt_id: 1, outcome: 'completed', measurement_basis: 'printer_reported', amount: '0.00', currency: 'USD', reason: null }],
}

describe('Measured filament panel', () => {
  it('shows known zero, scope and attempt breakdown', () => {
    render(<MeasuredFilamentView value={base} />)
    expect(screen.getByText('Fully measured filament: USD 0.00')).toBeTruthy()
    expect(screen.getByText('Printer-reported consumption.')).toBeTruthy()
    expect(screen.getByText('Attempt breakdown')).toBeTruthy()
    expect(screen.getByText(/Production COGS and measured margin are unavailable/)).toBeTruthy()
  })
  it('shows partial subtotal, failed consumption and missing evidence', () => {
    render(<MeasuredFilamentView value={{ ...base, filament_state: 'partial', unresolved_attempts: 1, failed_cancelled_subtotal: '0.00', reasons: ['missing_measurement'] }} />)
    expect(screen.getByText('Known measured filament cost: USD 0.00')).toBeTruthy()
    expect(screen.getByText('Failed / cancelled attempts: USD 0.00')).toBeTruthy()
    expect(screen.getByText('Printer consumption was not reported')).toBeTruthy()
  })
  it('never turns null or mixed currency into zero', () => {
    render(<MeasuredFilamentView value={{ ...base, filament_state: 'partial', measured_filament_known_subtotal: null, currency: null, successful_subtotal: null, reasons: ['currency_mismatch'] }} />)
    expect(screen.getByText('Measured cost unavailable')).toBeTruthy()
    expect(screen.getByText('Currencies do not agree')).toBeTruthy()
    expect(screen.queryByText('Fully measured filament: USD 0.00')).toBeNull()
  })
  it('loads through the private API and handles denial', async () => {
    vi.mocked(orders.measuredCosts).mockRejectedValueOnce(new Error('denied'))
    render(<MeasuredFilamentCost orderId={9} />)
    await waitFor(() => expect(screen.getByText(/Financial authority or historical evidence could not be verified/)).toBeTruthy())
    expect(orders.measuredCosts).toHaveBeenCalledWith(9)
  })
  it('discards stale order responses', async () => {
    let finish: (value: OrderMeasuredCosts) => void = () => {}
    vi.mocked(orders.measuredCosts).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
      .mockResolvedValueOnce({ ...base, measured_filament_known_subtotal: '7.00' })
    const view = render(<MeasuredFilamentCost orderId={1} />)
    view.rerender(<MeasuredFilamentCost orderId={2} />)
    await screen.findByText('Fully measured filament: USD 7.00')
    finish(base)
    await waitFor(() => expect(screen.queryByText('Fully measured filament: USD 0.00')).toBeNull())
  })
})
