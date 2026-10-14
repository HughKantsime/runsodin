import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CreateSpoolModal } from './SpoolModals'
vi.mock('../../api', () => ({ spools: {} }))
const create = vi.fn()
function mount() {
  const client = new QueryClient()
  client.setQueryData(['auth', 'me'], { id: 3, group_id: 1, role: 'admin' })
  render(<QueryClientProvider client={client}><CreateSpoolModal filaments={[{ id: 'lib_80', brand: 'Owned', name: 'Fixture', material: 'PLA' }]} onClose={() => {}} onCreate={create} /></QueryClientProvider>)
  fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: '80' } })
  return client
}
function submit() { fireEvent.submit(screen.getByRole('button', { name: 'Add Spool' }).closest('form')!) }
beforeEach(() => { cleanup(); vi.clearAllMocks() })
describe('explicit purchase opt-in', () => {
  it('defaults off and retains original operational defaults without purchase evidence', () => {
    mount(); expect(screen.getByRole('checkbox')).not.toBeChecked(); submit()
    expect(create).toHaveBeenCalledOnce()
    const payload = create.mock.calls[0][0]
    expect(payload.initial_weight_g).toBe(1000); expect(payload.spool_weight_g).toBe(250)
    expect(payload).not.toHaveProperty('purchase_evidence')
  })
  it('starts separate currency and purchased net blank and requires all explicit fields', () => {
    mount(); fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByLabelText('Purchase currency')).toHaveValue('')
    expect(screen.getByLabelText('Purchased net filament weight (g)')).toHaveValue('')
    submit(); expect(create).not.toHaveBeenCalled(); expect(screen.getByRole('alert')).toBeInTheDocument()
  })
  it('preserves zero and fractional purchase strings without float conversion', () => {
    mount(); fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(screen.getByLabelText('Purchase currency'), { target: { value: 'EUR' } })
    fireEvent.change(screen.getByLabelText('Total purchase price'), { target: { value: '000.000' } })
    fireEvent.change(screen.getByLabelText('Purchased net filament weight (g)'), { target: { value: '00950.12500' } })
    submit(); expect(create.mock.calls[0][0].purchase_evidence).toEqual({ currency: 'EUR', price: '000.000', net_weight_g: '00950.12500' })
    expect(create.mock.calls[0][0].initial_weight_g).toBe(1000)
  })
  it('cache-only audience switch retires prior private inputs and rejects stale form submit', async () => {
    const client = mount(); fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(screen.getByLabelText('Purchase currency'), { target: { value: 'EUR' } })
    fireEvent.change(screen.getByLabelText('Total purchase price'), { target: { value: '123.45' } })
    fireEvent.change(screen.getByLabelText('Purchased net filament weight (g)'), { target: { value: '950' } })
    const staleForm = screen.getByRole('button', { name: 'Add Spool' }).closest('form')!
    act(() => { client.setQueryData(['auth', 'me'], { id: 4, group_id: 2, role: 'operator' }); fireEvent.submit(staleForm) })
    expect(create).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByRole('checkbox')).not.toBeChecked())
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.getByLabelText('Purchase currency')).toHaveValue('')
    expect(screen.getByLabelText('Total purchase price')).toHaveValue('')
    expect(screen.getByLabelText('Purchased net filament weight (g)')).toHaveValue('')
    expect(screen.queryByDisplayValue('123.45')).toBeNull()
  })

})
