import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SquareInventory from './SquareInventory'
import { square } from '../../api/square'
const identity = vi.hoisted(() => ({ role: 'admin' }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => identity }))
vi.mock('../../api/square', () => ({ square: { status: vi.fn(), configure: vi.fn(), disconnect: vi.fn(), catalog: vi.fn(), locations: vi.fn(), map: vi.fn(), unmap: vi.fn(), stock: vi.fn() } }))
const mapping = { product_id: 101, location_id: 'L1', variation_id: 'V1', item_name: 'Square part', variation_name: 'Regular' }
const connected = { connected: true, revision: 'r1', environment: 'sandbox' as const, merchant_id: 'M1', mappings: [mapping] }
const mount = async () => { vi.mocked(square.status).mockResolvedValue(connected); render(<SquareInventory products={[{ id: 101, name: 'Own part' }]} />); fireEvent.click(screen.getByRole('button', { name: 'Square inventory' })); await screen.findByText(/Identity verified/) }
beforeEach(() => { vi.resetAllMocks(); identity.role = 'admin' })
afterEach(cleanup)
it('closed panel makes no Square request', () => { render(<SquareInventory products={[]} />); expect(square.status).not.toHaveBeenCalled() })
it('configuration clears password and uses explicit environment and revision', async () => {
 await mount(); vi.mocked(square.configure).mockResolvedValue({ ...connected, revision: 'r2' })
 fireEvent.change(screen.getByLabelText('Square access token'), { target: { value: 'synthetic-form-value' } })
 fireEvent.click(screen.getByRole('button', { name: 'Verify and replace connection' }))
 expect(screen.getByLabelText('Square access token')).toHaveValue('')
 await waitFor(() => expect(square.configure).toHaveBeenCalledWith({ access_token: 'synthetic-form-value', environment: 'sandbox', expected_revision: 'r1' }))
})
it('explicit mapping uses chosen product variation and location', async () => {
 await mount()
 vi.mocked(square.locations).mockResolvedValue({ revision: 'r1', locations: [{ id: 'L1', name: 'Shop' }] })
 vi.mocked(square.catalog).mockResolvedValue({ revision: 'r1', cursor: null, items: [{ variation_id: 'V1', item_name: 'Square part', variation_name: 'Regular', sku: 'PART' }] })
 vi.mocked(square.map).mockResolvedValue({ ...connected, revision: 'r2' })
 fireEvent.click(screen.getByRole('button', { name: 'Choose Square item' })); await screen.findByLabelText('Square variation')
 for(const [label,value] of [['ODIN product','101'],['Square location','L1'],['Square variation','V1']]) fireEvent.change(screen.getByLabelText(label),{target:{value}})
 fireEvent.click(screen.getByRole('button', { name: 'Save explicit mapping' }))
 await waitFor(() => expect(square.map).toHaveBeenCalledWith(101,{revision:'r1',location_id:'L1',variation_id:'V1'}))
})
it('count precision preserved and failed refresh clears prior count', async () => {
 await mount(); vi.mocked(square.stock).mockResolvedValue({revision:'r1',quantity:'7.00000',state:'known',retrieved_at:'2026-10-20T12:00:00Z',calculated_at:null})
 fireEvent.click(screen.getByRole('button',{name:'Read current stock'})); await screen.findByText(/Square in stock: 7.00000/)
 vi.mocked(square.stock).mockRejectedValue(new Error('Square denied access'));fireEvent.click(screen.getByRole('button',{name:'Read current stock'}))
 await screen.findByRole('alert');expect(screen.queryByText(/Square in stock/)).not.toBeInTheDocument()
})
it('missing stock stays unknown',async()=>{
 await mount();vi.mocked(square.stock).mockResolvedValue({revision:'r1',quantity:null,state:'unknown',retrieved_at:'2026-10-20T12:00:00Z',calculated_at:null})
 fireEvent.click(screen.getByRole('button',{name:'Read current stock'}));await screen.findByText(/Stock is unknown/)
 expect(screen.queryByText(/Square in stock: 0/)).not.toBeInTheDocument()
})
it('viewer cannot configure or remap',async()=>{
 identity.role='viewer';await mount();expect(screen.getByRole('button',{name:'Read current stock'})).toBeInTheDocument()
 expect(screen.queryByLabelText('Square access token')).not.toBeInTheDocument();expect(screen.queryByRole('button',{name:'Remove mapping'})).not.toBeInTheDocument()
})
it('wrong revision stock response is discarded',async()=>{
 await mount();vi.mocked(square.stock).mockResolvedValue({revision:'old',quantity:'999',state:'known',retrieved_at:'2026-10-20T12:00:00Z',calculated_at:null})
 fireEvent.click(screen.getByRole('button',{name:'Read current stock'}));await waitFor(()=>expect(screen.queryByText('Working…')).not.toBeInTheDocument())
 expect(screen.queryByText(/999/)).not.toBeInTheDocument()
})
