import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import Orders from './Orders'
import { MemoryRouter } from 'react-router-dom'
import { orders } from '../../api'
import toast from 'react-hot-toast'
vi.mock('../../permissions', () => ({canDo: () => true}))
vi.mock('../../api', () => ({orders: {list:vi.fn(),get:vi.fn(),create:vi.fn(),update:vi.fn(),updateItem:vi.fn()},products:{list:vi.fn()},orderInvoice:vi.fn()}))
import { products } from '../../api'
const order={id:1,order_number:'Synthetic zero order',status:'pending',revenue:0,platform_fees:0,payment_fees:0,shipping_charged:0,customer_email:'synthetic@example.test',notes:'Synthetic existing note',tracking_number:'SYNTHETIC-TRACKING',items:[{id:3,product_id:5,quantity:2,unit_price:0,subtotal:0,fulfilled_quantity:0,product_name:'Synthetic free product'}]}
beforeEach(()=>{
  vi.mocked(orders.list).mockResolvedValue([{id:order.id,order_number:order.order_number,status:order.status,revenue:order.revenue,customer_name:'Synthetic customer'}] as any)
  vi.mocked(orders.get).mockResolvedValue(order as any)
  vi.mocked(products.list).mockResolvedValue([{id:5,name:'Synthetic free product',price:0}] as any)
  vi.mocked(orders.update).mockResolvedValue(order as any)
  vi.mocked(orders.updateItem).mockResolvedValue(order.items[0] as any)
  vi.mocked(orders.create).mockResolvedValue(order as any)
})

it('shows recorded zero revenue in the order list',async()=>{
  render(<MemoryRouter initialEntries={['/orders']}><Orders /></MemoryRouter>)
  await waitFor(()=>expect(screen.getAllByText('$0.00').length).toBeGreaterThan(0))
})

it('creates a free catalog item with an explicit zero price',async()=>{
  render(<MemoryRouter initialEntries={['/orders']}><Orders /></MemoryRouter>)
  await waitFor(()=>expect(screen.getAllByTitle('Edit Order').length).toBeGreaterThan(0))
  fireEvent.click(screen.getAllByRole('button',{name:'New Order'})[0])
  fireEvent.click(screen.getByRole('button',{name:'Add Item'}))
  const select=screen.getByRole('option',{name:'Synthetic free product'}).closest('select')!
  fireEvent.change(select,{target:{value:'5'}})
  fireEvent.click(screen.getByRole('button',{name:'Create Order'}))
  await waitFor(()=>expect(orders.create).toHaveBeenCalledWith(expect.objectContaining({items:[{product_id:5,quantity:1,unit_price:0}]})))
})

it('clears the old catalog price when switching to an unpriced product',async()=>{
  vi.mocked(products.list).mockResolvedValue([{id:5,name:'Synthetic free product',price:0},{id:7,name:'Synthetic unpriced product',price:null}] as any)
  render(<MemoryRouter initialEntries={['/orders']}><Orders /></MemoryRouter>)
  await waitFor(()=>expect(screen.getAllByTitle('Edit Order').length).toBeGreaterThan(0))
  fireEvent.click(screen.getAllByRole('button',{name:'New Order'})[0])
  fireEvent.click(screen.getByRole('button',{name:'Add Item'}))
  const select=screen.getByRole('option',{name:'Synthetic free product'}).closest('select')!
  fireEvent.change(select,{target:{value:'5'}})
  fireEvent.change(select,{target:{value:'7'}})
  fireEvent.click(screen.getByRole('button',{name:'Create Order'}))
  await waitFor(()=>expect(orders.create).toHaveBeenCalledWith(expect.objectContaining({items:[{product_id:7,quantity:1,unit_price:null}]})))
})
afterEach(()=>{cleanup();vi.clearAllMocks()})
it('preserves recorded financial and item zeroes during an unchanged edit',async()=>{
  render(<MemoryRouter initialEntries={['/orders']}><Orders /></MemoryRouter>)
  await waitFor(()=>expect(screen.getAllByTitle('Edit Order').length).toBeGreaterThan(0))
  fireEvent.click(screen.getAllByTitle('Edit Order')[0])
  const save=await screen.findByRole('button',{name:'Save Changes'})
  fireEvent.click(save)
  await waitFor(()=>expect(orders.update).toHaveBeenCalled())
  expect(orders.update).toHaveBeenCalledWith(1,expect.objectContaining({revenue:0,platform_fees:0,payment_fees:0,shipping_charged:0,customer_email:'synthetic@example.test',notes:'Synthetic existing note',tracking_number:'SYNTHETIC-TRACKING'}))
  await waitFor(()=>expect(orders.updateItem).toHaveBeenCalledWith(1,3,{quantity:2,unit_price:0}))
})

it('does not open an editable summary when the full order cannot be loaded',async()=>{
  vi.mocked(orders.get).mockRejectedValue(new Error('Synthetic isolated failure'))
  const notify=vi.spyOn(toast,'error')
  render(<MemoryRouter initialEntries={['/orders']}><Orders /></MemoryRouter>)
  await waitFor(()=>expect(screen.getAllByTitle('Edit Order').length).toBeGreaterThan(0))
  fireEvent.click(screen.getAllByTitle('Edit Order')[0])
  await waitFor(()=>expect(notify).toHaveBeenCalledWith('Could not load order for editing'))
  expect(screen.queryByRole('button',{name:'Save Changes'})).toBeNull()
  expect(orders.update).not.toHaveBeenCalled()
  notify.mockRestore()
})

it('preserves nonzero financial charges loaded from the full order',async()=>{
  const priced={...order,revenue:19.5,platform_fees:2.25,payment_fees:0.75,shipping_charged:4}
  vi.mocked(orders.list).mockResolvedValue([{id:1,order_number:order.order_number,status:'pending',revenue:19.5}] as any)
  vi.mocked(orders.get).mockResolvedValue(priced as any)
  render(<MemoryRouter initialEntries={['/orders']}><Orders /></MemoryRouter>)
  await waitFor(()=>expect(screen.getAllByTitle('Edit Order').length).toBeGreaterThan(0))
  fireEvent.click(screen.getAllByTitle('Edit Order')[0])
  fireEvent.click(await screen.findByRole('button',{name:'Save Changes'}))
  await waitFor(()=>expect(orders.update).toHaveBeenCalledWith(1,expect.objectContaining({revenue:19.5,platform_fees:2.25,payment_fees:0.75,shipping_charged:4})))
})

it('ignores an older edit response after a newer order has opened',async()=>{
  let resolveFirst:any,resolveSecond:any
  const first=new Promise(resolve=>{resolveFirst=resolve})
  const second=new Promise(resolve=>{resolveSecond=resolve})
  vi.mocked(orders.list).mockResolvedValue([{id:1,order_number:'Synthetic first',status:'pending'},{id:2,order_number:'Synthetic second',status:'pending'}] as any)
  vi.mocked(orders.get).mockImplementation((id:any)=>id===1?first:second as any)
  render(<MemoryRouter initialEntries={['/orders']}><Orders /></MemoryRouter>)
  await waitFor(()=>expect(screen.getAllByTitle('Edit Order').length).toBeGreaterThan(1))
  const edits=screen.getAllByTitle('Edit Order')
  fireEvent.click(edits[0]);fireEvent.click(edits[1])
  await act(async()=>resolveSecond({...order,id:2,order_number:'Synthetic second'}))
  await screen.findByRole('button',{name:'Save Changes'})
  await act(async()=>resolveFirst({...order,id:1,order_number:'Synthetic first'}))
  fireEvent.click(screen.getByRole('button',{name:'Save Changes'}))
  await waitFor(()=>expect(orders.update).toHaveBeenCalledWith(2,expect.objectContaining({order_number:'Synthetic second'})))
})
