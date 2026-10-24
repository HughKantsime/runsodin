import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import StockReliability from './StockReliability'
import { spools } from '../../api/inventory'
vi.mock('../../permissions', () => ({ canDo: () => true }))
vi.mock('../../api/inventory', () => ({
  spools: { reorder: vi.fn(), setReorderPolicy: vi.fn(), receive: vi.fn() },
  filaments: { list: vi.fn().mockResolvedValue([{ id: 'lib_199', brand: 'Test', name: 'Black', material: 'PLA' }]) },
}))
const row = { material: 'PLA', color: '000000', sealed_rolls: 2, sealed_g: 2000, open_g: 250, total_g: 2250, threshold_g: 3000, reorder: true, shortfall_g: 750 }
function mount(orgId = 1) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 1, group_id: 1, role: 'operator' })
  return render(<QueryClientProvider client={client}><StockReliability orgId={orgId} /></QueryClientProvider>)
}
beforeEach(() => { vi.clearAllMocks(); vi.mocked(spools.reorder).mockResolvedValue([row]); vi.mocked(spools.setReorderPolicy).mockResolvedValue({}); vi.mocked(spools.receive).mockResolvedValue({ spools: [{ id: 201, qr_code: 'SPL-TEST' }] }) })
describe('inventory stock', () => {
  it('shows aggregate stock and saves a material/color threshold', async () => {
    mount()
    expect(await screen.findByText('2 rolls / 2000 g')).toBeTruthy()
    expect(screen.getByText('Order 750 g')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Set threshold for PLA 000000'))
    fireEvent.change(screen.getByLabelText('Threshold (g)'), { target: { value: '4000' } })
    fireEvent.click(screen.getByText('Save threshold'))
    await waitFor(() => expect(spools.setReorderPolicy).toHaveBeenCalledWith({ material: 'PLA', color: '000000', threshold_g: 4000 }, expect.anything()))
  })
  it('receives into storage and provides printable QR labels', async () => {
    mount()
    fireEvent.click(screen.getByText('Receive rolls'))
    await screen.findByText('Test Black (PLA)')
    fireEvent.change(screen.getByLabelText('Receiving filament'), { target: { value: '199' } })
    fireEvent.change(screen.getByLabelText('Storage location'), { target: { value: 'Shelf A' } })
    fireEvent.click(screen.getByText('Receive into storage'))
    expect(await screen.findByText('Open printable labels 1')).toHaveAttribute('href', '/api/spools/labels/batch?spool_ids=201')
    expect(spools.receive).toHaveBeenCalledWith({ filament_id: 199, quantity: 1, initial_weight_g: 1000, storage_location: 'Shelf A', sealed: true }, expect.anything())
  })
  it('does not offer writes while viewing another org', async () => {
    mount(2)
    await screen.findByText('Order 750 g')
    expect(screen.queryByText('Receive rolls')).toBeNull()
    expect(screen.queryByLabelText('Set threshold for PLA 000000')).toBeNull()
  })
  it('shows load errors', async () => {
    vi.mocked(spools.reorder).mockRejectedValue(new Error('unavailable'))
    mount()
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load reorder stock')
  })
})
