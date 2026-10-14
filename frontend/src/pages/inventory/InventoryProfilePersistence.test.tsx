import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Spools from './Spools'
const api=vi.hoisted(()=>({list:vi.fn(),update:vi.fn()}))
vi.mock('../../api',()=>({spools:{list:api.list,update:api.update},filaments:{list:vi.fn().mockResolvedValue([])},printers:{list:vi.fn().mockResolvedValue([])},bulkOps:{spools:vi.fn()}}))
vi.mock('../../permissions',()=>({canDo:()=>true}))
vi.mock('../../contexts/OrgContext',()=>({useOrg:()=>({orgId:1})}))
let client:QueryClient
const spool={id:1,qr_code:'SPL-OWNED',status:'active',filament_name:'Owned spool',filament_brand:'Owned',filament_material:'PLA',initial_weight_g:1000,remaining_weight_g:500,percent_remaining:50,pa_profile:'K0.042',low_stock_threshold_g:0}
function show(){client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});render(<QueryClientProvider client={client}><MemoryRouter><Spools/></MemoryRouter></QueryClientProvider>)}
beforeEach(()=>{vi.clearAllMocks();api.list.mockResolvedValue([spool]);api.update.mockResolvedValue({success:true,id:1})})
afterEach(()=>{cleanup();client?.clear()})
it('edit saves actual PA and explicit zero threshold through update API',async()=>{
 show();await screen.findByText('SPL-OWNED');fireEvent.click(screen.getByTitle('Edit spool'))
 expect(screen.getByLabelText('PA Profile')).toHaveValue('K0.042');expect(screen.getByLabelText('Low-stock threshold (g)')).toHaveValue(0)
 fireEvent.change(screen.getByLabelText('PA Profile'),{target:{value:'K0.055'}});fireEvent.click(screen.getByRole('button',{name:'Save',exact:true}))
 await waitFor(()=>expect(api.update).toHaveBeenCalledWith(expect.objectContaining({id:1,pa_profile:'K0.055',low_stock_threshold_g:0})))
})
it('selected batch action opens genuine sheet URLs covering selections larger than a page',async()=>{
 const rows=Array.from({length:41},(_,i)=>({...spool,id:i+1,qr_code:`SPL-OWNED-${i+1}`}));api.list.mockResolvedValue(rows)
 show();await screen.findByText('SPL-OWNED-1');fireEvent.click(screen.getByRole('checkbox',{name:'Select all'}));fireEvent.click(screen.getByRole('button',{name:'Print labels'}))
 const links=await screen.findAllByRole('link',{name:/Open label sheet/});expect(links).toHaveLength(2)
 const ids=links.flatMap(link=>new URL(link.getAttribute('href')!,'http://owned.invalid').searchParams.get('spool_ids')!.split(',').map(Number))
 expect(ids).toEqual(rows.map(r=>r.id));expect(links[0]).toHaveAttribute('href',`/api/spools/labels/batch?spool_ids=${rows.slice(0,40).map(r=>r.id).join(',')}&size=small`)
 expect(links[0]).toHaveAttribute('target','_blank');expect(links[0]).toHaveAttribute('rel','noopener noreferrer')
},15000) // 41-row render exceeds the default 5s budget on a loaded host

it('edit refuses a threshold above the database integer limit before a request',async()=>{
 show();await screen.findByText('SPL-OWNED');fireEvent.click(screen.getByTitle('Edit spool'))
 const input=screen.getByLabelText('Low-stock threshold (g)')
 expect(input).toHaveAttribute('max','2147483647')
 fireEvent.change(input,{target:{value:'2147483648'}})
 fireEvent.submit(screen.getByRole('button',{name:'Save',exact:true}).closest('form')!)
 expect(api.update).not.toHaveBeenCalled()
 expect(screen.getByText('Low-stock threshold must be a whole number from 0 to 2147483647.')).toBeInTheDocument()
})
