import React from 'react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {act,cleanup,fireEvent,render,within,screen,waitFor} from '@testing-library/react'
import {QueryClient,QueryClientProvider} from '@tanstack/react-query'
import Consumables from './Consumables'
import {consumables} from '../../api'
import {canDo} from '../../permissions'
vi.mock('../../api',()=>({consumables:{list:vi.fn(),adjust:vi.fn(),create:vi.fn(),update:vi.fn(),delete:vi.fn()}}))
vi.mock('../../permissions',()=>({canDo:vi.fn(()=>true)}))
const item={id:1,name:'Synthetic packaging',unit:'piece',current_stock:5,min_stock:1,cost_per_unit:0,status:'active'}
beforeEach(()=>{vi.mocked(canDo).mockReturnValue(true);vi.mocked(consumables.list).mockResolvedValue([item] as any);vi.mocked(consumables.adjust).mockResolvedValue({new_stock:205} as any)})
afterEach(()=>{cleanup();vi.clearAllMocks()})
function mount(){const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});render(<QueryClientProvider client={client}><Consumables /></QueryClientProvider>)}
it('requires a second confirmation before a large stock adjustment',async()=>{
 mount();fireEvent.click(await screen.findByTitle('Adjust Stock'))
 fireEvent.change(screen.getByRole('spinbutton'),{target:{value:'200'}})
 fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))
 expect(consumables.adjust).not.toHaveBeenCalled()
 expect(screen.getByText(/Click Adjust again to confirm/)).toBeTruthy()
 fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))
 await waitFor(()=>expect(consumables.adjust).toHaveBeenCalledWith(1,{quantity:200,type:'restock',notes:''}))
})
it('changing a confirmed large amount requires confirmation again',async()=>{
 mount();fireEvent.click(await screen.findByTitle('Adjust Stock'))
 fireEvent.change(screen.getByRole('spinbutton'),{target:{value:'200'}})
 fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))
 fireEvent.change(screen.getByRole('spinbutton'),{target:{value:'300'}})
 fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))
 expect(consumables.adjust).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))
 await waitFor(()=>expect(consumables.adjust).toHaveBeenCalledWith(1,{quantity:300,type:'restock',notes:''}))
})

it('requires renewed confirmation when changing restock to deduction',async()=>{
 mount();fireEvent.click(await screen.findByTitle('Adjust Stock'))
 fireEvent.change(screen.getByRole('spinbutton'),{target:{value:'200'}})
 fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))
 const selects=screen.getAllByRole('combobox')
 const direction=selects.find(element=>element.querySelector('option[value="deduct"]'))!
 fireEvent.change(direction,{target:{value:'deduct'}})
 await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))})
 expect(consumables.adjust).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))
 await waitFor(()=>expect(consumables.adjust).toHaveBeenCalledWith(1,{quantity:200,type:'deduct',notes:''}))
})
it('does not submit a second adjustment while the first is pending',async()=>{
 vi.mocked(consumables.adjust).mockReturnValue(new Promise(()=>{}) as any)
 mount();fireEvent.click(await screen.findByTitle('Adjust Stock'))
 fireEvent.change(screen.getByRole('spinbutton'),{target:{value:'2'}})
 fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))
 await waitFor(()=>expect(consumables.adjust).toHaveBeenCalledTimes(1))
 await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))})
 expect(consumables.adjust).toHaveBeenCalledTimes(1)
})

it('releases the pending guard after failure and permits one successful retry',async()=>{
 let reject:any
 vi.mocked(consumables.adjust).mockReturnValueOnce(new Promise((resolve,rejectPromise)=>{reject=rejectPromise}) as any)
 mount();fireEvent.click(await screen.findByTitle('Adjust Stock'))
 fireEvent.change(screen.getByRole('spinbutton'),{target:{value:'2'}})
 fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))
 await waitFor(()=>expect(consumables.adjust).toHaveBeenCalledTimes(1))
 await act(async()=>{reject(new Error('Synthetic adjustment failure'))})
 await waitFor(()=>expect((screen.getByRole('button',{name:'Adjust',exact:true}) as HTMLButtonElement).disabled).toBe(false))
 fireEvent.click(screen.getByRole('button',{name:'Adjust',exact:true}))
 await waitFor(()=>expect(consumables.adjust).toHaveBeenCalledTimes(2))
 await waitFor(()=>expect(screen.queryByRole('button',{name:'Adjust',exact:true})).toBeNull())
})

it('does not offer stock adjustment to a read-only user',async()=>{
 vi.mocked(canDo).mockReturnValue(false)
 mount();await screen.findByText('Synthetic packaging')
 expect(screen.queryByTitle('Adjust Stock')).toBeNull()
})

it('preserves an unknown unit cost during an unchanged edit',async()=>{
 vi.mocked(consumables.list).mockResolvedValue([{...item,cost_per_unit:null}] as any)
 vi.mocked(consumables.update).mockResolvedValue({...item,cost_per_unit:null} as any)
 mount();fireEvent.click(await screen.findByTitle('Edit'))
 fireEvent.click(screen.getByRole('button',{name:'Save',exact:true}))
 await waitFor(()=>expect(consumables.update).toHaveBeenCalledWith(1,expect.objectContaining({cost_per_unit:null})))
})
it('shows and preserves an explicit zero unit cost',async()=>{
 vi.mocked(consumables.update).mockResolvedValue(item as any)
 mount();await screen.findByText('Synthetic packaging')
 expect(within(screen.getByText('Synthetic packaging').closest('tr')!).getByText('$0.00')).toBeTruthy()
 fireEvent.click(await screen.findByTitle('Edit'))
 expect((screen.getAllByRole('spinbutton')[0] as HTMLInputElement).value).toBe('0')
 fireEvent.click(screen.getByRole('button',{name:'Save',exact:true}))
 await waitFor(()=>expect(consumables.update).toHaveBeenCalledWith(1,expect.objectContaining({cost_per_unit:0})))
})

it('uses the configured models.edit action for authorized inventory edits and adjustments',async()=>{
 vi.mocked(canDo).mockImplementation(action=>['models.create','models.edit','models.delete'].includes(action))
 mount()
 expect(await screen.findByTitle('Edit')).toBeTruthy()
 expect(screen.getByTitle('Adjust Stock')).toBeTruthy()
})
