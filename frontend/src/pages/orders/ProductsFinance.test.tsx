import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import Products from './Products'
import { products, models, consumables } from '../../api'
import toast from 'react-hot-toast'
vi.mock('../../permissions', () => ({canDo: () => true}))
vi.mock('../../api', () => ({products: {list:vi.fn(),get:vi.fn(),update:vi.fn(),removeComponent:vi.fn(),addComponent:vi.fn()}, models:{list:vi.fn()},consumables:{list:vi.fn()}}))
const product={id:1,name:'Synthetic free product',sku:'FREE',price:0,description:'Synthetic details',components:[],component_count:0}
beforeEach(()=>{
  vi.mocked(products.list).mockResolvedValue([product] as any)
  vi.mocked(products.get).mockResolvedValue(product as any)
  vi.mocked(products.update).mockResolvedValue(product as any)
  vi.mocked(models.list).mockResolvedValue([])
  vi.mocked(consumables.list).mockResolvedValue([])
})
afterEach(()=>{cleanup();vi.clearAllMocks()})
it('renders recorded zero prices in both product presentations',async()=>{
  render(<Products />)
  await screen.findAllByText('Synthetic free product')
  expect(screen.getByText('Price: $0.00')).toBeTruthy()
  expect(screen.getAllByText('$0.00').length).toBeGreaterThan(0)
})
it('preserves an explicit zero during an unchanged edit',async()=>{
  render(<Products />)
  fireEvent.click((await screen.findAllByTitle('Edit'))[0])
  fireEvent.click(await screen.findByRole('button',{name:'Save Changes'}))
  await waitFor(()=>expect(products.update).toHaveBeenCalledWith(1,expect.objectContaining({price:0})))
})
it('does not offer editing after a failed full product load',async()=>{
  vi.mocked(products.get).mockRejectedValue(new Error('Synthetic isolated load failure'))
  const notify=vi.spyOn(toast,'error')
  render(<Products />)
  fireEvent.click((await screen.findAllByTitle('Edit'))[0])
  await waitFor(()=>expect(notify).toHaveBeenCalledWith('Could not load product for editing'))
  expect(screen.queryByRole('button',{name:'Save Changes'})).toBeNull()
  expect(products.update).not.toHaveBeenCalled()
  expect(products.removeComponent).not.toHaveBeenCalled()
  notify.mockRestore()
})
it('edits full product fields instead of stale list values',async()=>{
  vi.mocked(products.get).mockResolvedValue({...product,name:'Current product',sku:'CURRENT',price:12.5,description:'Current description'} as any)
  render(<Products />)
  fireEvent.click((await screen.findAllByTitle('Edit'))[0])
  fireEvent.click(await screen.findByRole('button',{name:'Save Changes'}))
  await waitFor(()=>expect(products.update).toHaveBeenCalledWith(1,{name:'Current product',sku:'CURRENT',price:12.5,description:'Current description'}))
})
it('preserves an unknown price as unknown during unchanged editing',async()=>{
  vi.mocked(products.get).mockResolvedValue({...product,price:null} as any)
  render(<Products />)
  fireEvent.click((await screen.findAllByTitle('Edit'))[0])
  fireEvent.click(await screen.findByRole('button',{name:'Save Changes'}))
  await waitFor(()=>expect(products.update).toHaveBeenCalledWith(1,expect.objectContaining({price:null})))
})

it('ignores a slow edit after a newer product editor opens',async()=>{
  let resolveFirst:any
  const slow=new Promise(resolve=>{resolveFirst=resolve})
  vi.mocked(products.list).mockResolvedValue([product,{...product,id:2,name:'Second product'}] as any)
  vi.mocked(products.get).mockImplementation((id:any)=>id===1?slow as any:Promise.resolve({...product,id:2,name:'Current second'}) as any)
  render(<Products />)
  const edits=await screen.findAllByTitle('Edit')
  fireEvent.click(edits[0]);fireEvent.click(edits[1])
  await screen.findByRole('button',{name:'Save Changes'})
  await act(async()=>resolveFirst({...product,name:'Stale first'}))
  fireEvent.click(screen.getByRole('button',{name:'Save Changes'}))
  await waitFor(()=>expect(products.update).toHaveBeenCalledWith(2,expect.objectContaining({name:'Current second'})))
})
it('does not replace a create form with a pending edit response',async()=>{
  let resolveEdit:any
  vi.mocked(products.get).mockImplementation(()=>new Promise(resolve=>{resolveEdit=resolve}) as any)
  render(<Products />)
  fireEvent.click((await screen.findAllByTitle('Edit'))[0])
  fireEvent.click(screen.getAllByRole('button',{name:'New Product'})[0])
  await act(async()=>resolveEdit(product))
  expect(screen.getByRole('button',{name:'Create Product'})).toBeTruthy()
  expect(screen.queryByRole('button',{name:'Save Changes'})).toBeNull()
})
